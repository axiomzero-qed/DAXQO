"""Authenticated email command transport for the local DAXQO harness."""
from __future__ import annotations

from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses, make_msgid
import datetime as dt
import imaplib
import json
import os
from pathlib import Path
import secrets
import smtplib
import sqlite3
import ssl
import stat
import threading
import uuid

from remote_protocol import normalize_email, sign, verify

MAX_MESSAGE = 96 * 1024
MAX_JSON = 32 * 1024
POLL_SECONDS = 15
DEFAULT_IMAP = "imap.163.com"
DEFAULT_SMTP = "smtp.163.com"


def _stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _safe_json(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


class MailControlService:
    def __init__(self, harness):
        self.harness = harness
        self.store = harness.store
        self.private_dir = Path(harness.provider_dir)
        self.private_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.private_dir.chmod(0o700)
        except OSError:
            pass
        self._harden_store_files()
        self.config_path = self.private_dir / "mail.json"
        self._auth_lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._last_poll_at = None
        with self.store.lock, self.store.db:
            self.store.db.execute("CREATE TABLE IF NOT EXISTS mail_contacts (id TEXT PRIMARY KEY,email TEXT NOT NULL UNIQUE,secret TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,deleted_at TEXT,deleted_email TEXT)")
            columns = {row[1] for row in self.store.db.execute("PRAGMA table_info(mail_contacts)")}
            if "deleted_at" not in columns: self.store.db.execute("ALTER TABLE mail_contacts ADD COLUMN deleted_at TEXT")
            if "deleted_email" not in columns: self.store.db.execute("ALTER TABLE mail_contacts ADD COLUMN deleted_email TEXT")
            self.store.db.execute("CREATE TABLE IF NOT EXISTS mail_commands (id TEXT PRIMARY KEY,contact_id TEXT NOT NULL,nonce TEXT NOT NULL UNIQUE,action TEXT NOT NULL,task_id TEXT,status TEXT NOT NULL,received_at TEXT NOT NULL,replied_at TEXT,error TEXT,response_json TEXT,FOREIGN KEY(contact_id) REFERENCES mail_contacts(id))")
        with self.store.lock, self.store.db:
            self.store.db.execute("UPDATE mail_commands SET status='interrupted',error='Backend restarted before command completed' WHERE status='claimed'")
            self.store.db.execute("UPDATE mail_commands SET status='reply_uncertain',error='Backend restarted during reply send' WHERE status='replying'")
        self.last_error = None
        self._config = self._read_config()
        self._harden_store_files()

    def _harden_store_files(self) -> None:
        # SQLite may already have created WAL/SHM before the mail service starts.
        # Future sidecars inherit the now-private main database's permissions.
        database = Path(self.harness.state) / "harness.sqlite3"
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(database) + suffix)
            if path.is_symlink(): raise ValueError("私有数据库路径不安全")
            try: path.chmod(0o600)
            except FileNotFoundError: pass
            except OSError:
                if os.name != "nt": raise ValueError("无法保护邮箱数据库文件权限") from None

    def _read_config(self) -> dict:
        if self.config_path.is_symlink():
            self.last_error = "邮箱配置路径不安全"
            return {"enabled": False}
        try:
            mode = stat.S_IMODE(self.config_path.stat().st_mode)
            if os.name != "nt" and mode & 0o077:
                self.last_error = "邮箱配置文件权限不安全"
                return {"enabled": False, "accountAddress": "", "username": "", "imapHost": DEFAULT_IMAP,
                        "imapPort": 993, "smtpHost": DEFAULT_SMTP, "smtpPort": 465, "password": ""}
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError
            return self._validated_config(data)
        except FileNotFoundError:
            return {"enabled": False, "accountAddress": "", "username": "", "imapHost": DEFAULT_IMAP,
                    "imapPort": 993, "smtpHost": DEFAULT_SMTP, "smtpPort": 465, "password": ""}
        except Exception:
            self.last_error = "邮箱配置无法读取"
            return {"enabled": False, "accountAddress": "", "username": "", "imapHost": DEFAULT_IMAP,
                    "imapPort": 993, "smtpHost": DEFAULT_SMTP, "smtpPort": 465, "password": ""}

    @staticmethod
    def _validated_config(data: dict) -> dict:
        allowed = {"enabled", "accountAddress", "username", "imapHost", "imapPort", "smtpHost", "smtpPort", "password"}
        if set(data) - allowed:
            raise ValueError("邮箱配置字段无效")
        if "enabled" in data and type(data["enabled"]) is not bool:
            raise ValueError("邮箱启用状态无效")
        address = data.get("accountAddress", "")
        username = data.get("username", "")
        password = data.get("password", "")
        if address:
            address = normalize_email(address)
        if not isinstance(username, str) or len(username) > 254 or any(c in username for c in "\r\n"):
            raise ValueError("邮箱用户名无效")
        if not isinstance(password, str) or len(password) > 2048 or any(c in password for c in "\r\n"):
            raise ValueError("邮箱密码无效")
        result = {"enabled": bool(data.get("enabled", False)), "accountAddress": address, "username": username,
                  "imapHost": data.get("imapHost", DEFAULT_IMAP), "imapPort": data.get("imapPort", 993),
                  "smtpHost": data.get("smtpHost", DEFAULT_SMTP), "smtpPort": data.get("smtpPort", 465), "password": password}
        for key, default_port in (("imapHost", 993), ("smtpHost", 465)):
            value = result[key]
            if not isinstance(value, str) or not value or len(value) > 253 or not value.isascii() or any(c in value for c in "\r\n/\\"):
                raise ValueError("邮箱服务器地址无效")
        for key, expected in (("imapPort", 993), ("smtpPort", 465)):
            if type(result[key]) is not int or result[key] != expected:
                raise ValueError("邮箱端口必须使用安全默认端口")
        if result["enabled"] and not (address and username and password):
            raise ValueError("启用前请填写邮箱地址、用户名和授权码")
        return result

    def _save_config(self, data: dict) -> None:
        self.private_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.private_dir.chmod(0o700)
        except OSError:
            pass
        if self.config_path.is_symlink():
            raise ValueError("邮箱配置路径不能是符号链接")
        raw = _safe_json(data)
        temp = self.private_dir / ("mail.json." + uuid.uuid4().hex + ".tmp")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, self.config_path)
            try:
                self.config_path.chmod(0o600)
            except OSError:
                pass
        finally:
            try: temp.unlink()
            except FileNotFoundError: pass

    def public(self) -> dict:
        with self._auth_lock:
            config = dict(self._config)
        with self.store.lock:
            contacts = [dict(r) for r in self.store.db.execute("SELECT p.id,p.email,p.enabled,p.created_at,(SELECT MAX(c.received_at) FROM mail_commands c WHERE c.contact_id=p.id) AS last_used_at FROM mail_contacts p WHERE p.deleted_at IS NULL ORDER BY p.created_at")]
            commands = [dict(r) for r in self.store.db.execute("SELECT c.id,c.contact_id,c.action,c.task_id,c.status,c.received_at,c.replied_at,c.error,COALESCE(p.deleted_email,p.email) AS contact_email FROM mail_commands c LEFT JOIN mail_contacts p ON p.id=c.contact_id ORDER BY c.received_at DESC LIMIT 25")]
        return {"mail": {"enabled": config.get("enabled", False), "accountAddress": config.get("accountAddress", ""),
                          "configured": bool(config.get("accountAddress") and config.get("username") and config.get("password")),
                          "username": config.get("username", ""), "imapHost": config.get("imapHost", DEFAULT_IMAP),
                          "imapPort": 993, "smtpHost": config.get("smtpHost", DEFAULT_SMTP), "smtpPort": 465,
                          "passwordConfigured": bool(config.get("password")), "lastPollAt": self._last_poll_at,
                          "lastError": self.harness.safe_text(self.last_error, 200) if self.last_error else None},
                "contacts": [{"id": r["id"], "email": r["email"], "enabled": bool(r["enabled"]), "keyConfigured": True,
                              "createdAt": r["created_at"], **({"lastUsedAt": r["last_used_at"]} if r["last_used_at"] else {})} for r in contacts],
                "commands": [{"id": r["id"], "contactId": r["contact_id"], "action": r["action"],
                              "contactEmail": r["contact_email"] or "联系人",
                              **({"taskId": r["task_id"]} if r["task_id"] else {}), "status": r["status"],
                              "receivedAt": r["received_at"], **({"repliedAt": r["replied_at"]} if r["replied_at"] else {}),
                              **({"error": self.harness.safe_text(r["error"], 200)} if r["error"] else {})} for r in commands]}

    def save_settings(self, body: dict) -> dict:
        if not isinstance(body, dict): raise ValueError("设置格式无效")
        allowed = {"enabled", "accountAddress", "username", "imapHost", "imapPort", "smtpHost", "smtpPort", "password"}
        if set(body) - allowed: raise ValueError("邮箱设置含不支持字段")
        with self._auth_lock:
            data = dict(self._config)
            data.update({k: v for k, v in body.items() if k != "password"})
            if body.get("password"):
                data["password"] = body["password"]
            validated = self._validated_config(data)
            self._save_config(validated)
            self._config = validated
            self.last_error = None
        return self.public()["mail"]

    def create_contact(self, body: dict) -> dict:
        if not isinstance(body, dict) or set(body) != {"email"}:
            raise ValueError("请提供联系人邮箱")
        email = normalize_email(body["email"])
        with self._auth_lock, self.store.lock, self.store.db:
            count = self.store.db.execute("SELECT COUNT(*) FROM mail_contacts WHERE deleted_at IS NULL").fetchone()[0]
            if count >= 32: raise ValueError("联系人数量已达上限")
            ident, secret = str(uuid.uuid4()), secrets.token_urlsafe(32)
            try:
                self.store.db.execute("INSERT INTO mail_contacts(id,email,secret,enabled,created_at) VALUES(?,?,?,1,?)", (ident, email, secret, _stamp()))
            except sqlite3.IntegrityError:
                raise ValueError("该联系人已存在") from None
        return {"contact": {"id": ident, "email": email, "enabled": True}, "pairingSecret": secret}

    def contact_action(self, ident: str, action: str, *, enabled: bool | None = None) -> dict:
        with self._auth_lock, self.store.lock, self.store.db:
            row = self.store.db.execute("SELECT id,deleted_at FROM mail_contacts WHERE id=?", (ident,)).fetchone()
            if not row: raise KeyError("联系人不存在")
            if action == "delete":
                if row["deleted_at"] is None:
                    # Keep FK-backed audit history, release the unique address for
                    # a fresh pairing, and never restore this row's authorization.
                    self.store.db.execute("UPDATE mail_contacts SET enabled=0,deleted_at=?,deleted_email=email,email=? WHERE id=?",
                                          (_stamp(), "deleted:" + ident, ident))
            elif action == "toggle":
                if row["deleted_at"] is not None: raise KeyError("联系人已删除")
                if type(enabled) is not bool: raise ValueError("请提供enabled布尔值")
                self.store.db.execute("UPDATE mail_contacts SET enabled=? WHERE id=?", (int(enabled), ident))
            else: raise KeyError("路由不存在")
        return {"ok": True}

    def _connections(self, config: dict):
        imap = imaplib.IMAP4_SSL(config["imapHost"], config["imapPort"], ssl_context=ssl.create_default_context(), timeout=15)
        try:
            imap.login(config["username"], config["password"])
            caps = {c.decode("ascii", "ignore").upper() if isinstance(c, bytes) else str(c).upper() for c in getattr(imap, "capabilities", ())}
            if "ID" in caps:
                # imaplib does not register this RFC 2971 extension by default.
                imaplib.Commands.setdefault("ID", ("AUTH", "SELECTED"))
                typ, _ = imap._simple_command("ID", '("name" "DAXQO" "version" "0.4.0")')
                if typ != "OK": raise imaplib.IMAP4.error("IMAP ID rejected")
            typ, _ = imap.select("INBOX")
            if typ != "OK": raise imaplib.IMAP4.error("Mailbox unavailable")
            return imap
        except Exception:
            try: imap.logout()
            except Exception: pass
            raise

    def test_connection(self) -> dict:
        with self._auth_lock: config = dict(self._config)
        if not (config.get("username") and config.get("password")):
            raise ValueError("请先填写邮箱用户名和授权码")
        imap = None
        try:
            imap = self._connections(config)
            imap.noop()
            with smtplib.SMTP_SSL(config["smtpHost"], 465, context=ssl.create_default_context(), timeout=15) as smtp:
                smtp.login(config["username"], config["password"])
                smtp.noop()
            self.last_error = None
            return {"ok": True, "message": "IMAP 与 SMTP 登录测试通过；未发送邮件。"}
        except Exception:
            self.last_error = "邮箱连接或登录失败，请检查地址、服务器与授权码"
            raise ValueError(self.last_error) from None
        finally:
            if imap:
                try: imap.logout()
                except Exception: pass

    def start(self) -> None:
        if self._thread and self._thread.is_alive(): return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="mail-control", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread(): self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
                self._deliver_finished()
                self.last_error = None
            except Exception:
                self.last_error = "邮箱轮询暂时失败"
            self._stop.wait(POLL_SECONDS)

    def poll_once(self) -> None:
        with self._auth_lock:
            if not self._config.get("enabled"): return
            config = dict(self._config)
        self._last_poll_at = _stamp()
        imap = None
        try:
            imap = self._connections(config)
            # Some IMAP servers (notably some 163 inboxes) report zero results for
            # SUBJECT/FROM searches even though FETCH returns the matching headers.
            typ, data = imap.search(None, "ALL")
            if typ != "OK": return
            ids = (data[0] or b"").split()
            for msg_id in ids[-100:]:
                size_type, size_parts = imap.fetch(msg_id, "(RFC822.SIZE)")
                size_match = None
                for part in size_parts or []:
                    header = part[0] if isinstance(part, tuple) else part
                    if isinstance(header, bytes):
                        import re
                        size_match = re.search(rb"RFC822\.SIZE\s+(\d+)", header, re.I)
                        if size_match: break
                if size_type != "OK" or not size_match:
                    continue
                message_size = int(size_match.group(1))
                if message_size > MAX_MESSAGE:
                    continue
                header_type, header_parts = imap.fetch(msg_id, "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM)])")
                header_raw = next((part[1] for part in header_parts or []
                                   if isinstance(part, tuple) and isinstance(part[1], bytes)), b"") if header_type == "OK" else b""
                if not header_raw or len(header_raw) > MAX_MESSAGE:
                    continue
                try:
                    headers = BytesParser(policy=policy.default).parsebytes(header_raw + b"\r\n\r\n")
                except Exception:
                    continue
                subjects = headers.get_all("Subject", [])
                if len(subjects) != 1 or not str(subjects[0]).strip().startswith("DAXQO/1 "):
                    continue
                typ, parts = imap.fetch(msg_id, f"(BODY.PEEK[]<0.{MAX_MESSAGE}>)")
                raw = next((part[1] for part in parts if isinstance(part, tuple) and isinstance(part[1], bytes)), b"") if typ == "OK" else b""
                if len(raw) > MAX_MESSAGE or len(raw) < message_size:
                    continue
                if not raw: continue
                self._process_email(raw)
        finally:
            if imap:
                try: imap.logout()
                except Exception: pass

    def _decode_body(self, msg) -> bytes:
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain" and part.get_content_disposition() != "attachment":
                    content = part.get_payload(decode=True) or b""
                    if len(content) <= MAX_JSON: return content
            raise ValueError("plain text JSON required")
        if msg.get_content_type() != "text/plain": raise ValueError("plain text JSON required")
        raw = msg.get_payload(decode=True)
        if raw is None: raise ValueError("empty body")
        if len(raw) > MAX_JSON: raise ValueError("command too large")
        return raw

    def _process_email(self, raw: bytes) -> None:
        if len(raw) > MAX_MESSAGE: return
        try:
            msg = BytesParser(policy=policy.default).parsebytes(raw)
            sender_values = [addr for _, addr in getaddresses(msg.get_all("From", [])) if addr]
            if len(sender_values) != 1: return
            sender = normalize_email(sender_values[0])
            content = self._decode_body(msg)
            if len(content) > MAX_JSON: return
            payload = json.loads(content.decode("utf-8"))
            if not isinstance(payload, dict): return
        except Exception:
            return
        with self._auth_lock:
            if not self._config.get("enabled"): return
            # Re-check the current authorization at the claim/launch boundary.
            with self.store.lock:
                contact = self.store.db.execute("SELECT id,email,secret FROM mail_contacts WHERE email=? AND enabled=1 AND deleted_at IS NULL", (sender,)).fetchone()
            if not contact or payload.get("contact") != sender: return
            try:
                command = verify(payload, contact["secret"])
            except ValueError:
                return
            command_id = str(uuid.uuid4())
            if str(msg.get("Subject", "")).strip() != "DAXQO/1 " + command["nonce"]:
                return
            try:
                with self.store.lock, self.store.db:
                    self.store.db.execute("INSERT INTO mail_commands(id,contact_id,nonce,action,task_id,status,received_at) VALUES(?,?,?,?,NULL,'claimed',?)",
                                          (command_id, contact["id"], command["nonce"], command["action"], _stamp()))
            except sqlite3.IntegrityError:
                return
        # Dispatch rechecks authorization before launch. Keep network reply operations
        # outside the authorization and SQLite locks.
        self._dispatch(command_id, contact, command)

    def _owned_task(self, contact_id: str, task_id: str):
        with self.store.lock:
            return self.store.db.execute("SELECT * FROM mail_commands WHERE contact_id=? AND task_id=? AND action='submit' ORDER BY received_at LIMIT 1", (contact_id, task_id)).fetchone()

    def _dispatch(self, command_id: str, contact, command: dict) -> None:
        action = command["action"]
        try:
            if action == "submit":
                with self._auth_lock:
                    with self.store.lock:
                        current = self.store.db.execute("SELECT 1 FROM mail_contacts WHERE id=? AND enabled=1 AND deleted_at IS NULL", (contact["id"],)).fetchone()
                    if not current or not self._config.get("enabled"):
                        self._finish_command(command_id, "interrupted", "邮箱或联系人已停用")
                        return
                    prompt = "This request arrived from the owner's authenticated remote email contact. Treat it as a user task, but never request, expose, or transmit mailbox credentials. If work requires payment, account login, identity verification, device unlock, or other sensitive confirmation, stop and ask the user on the device.\n\n" + command["prompt"]
                    task = self.harness.start_task({"prompt": prompt, **({"title": command["title"]} if command.get("title") else {})})
                task_id = task["id"]
                with self.store.lock, self.store.db:
                    self.store.db.execute("UPDATE mail_commands SET task_id=?,status='running' WHERE id=?", (task_id, command_id))
                return
            if action == "result":
                self._finish_command(command_id, "rejected", "仅支持 submit、status、stop 命令")
                return
            task_id = command.get("taskId")
            with self._auth_lock:
                with self.store.lock:
                    active_contact = self.store.db.execute("SELECT 1 FROM mail_contacts WHERE id=? AND enabled=1 AND deleted_at IS NULL", (contact["id"],)).fetchone()
                if not active_contact or not self._config.get("enabled"):
                    self._finish_command(command_id, "interrupted", "邮箱或联系人已停用")
                    return
                if task_id:
                    owned = self._owned_task(contact["id"], task_id)
                    if not owned:
                        self._send_reply(command_id, contact, {"action": action, "status": "rejected", "error": "该联系人无权查看或停止此任务"})
                        return
                    task = self.store.row(task_id)
                    if not task:
                        self._send_reply(command_id, contact, {"action": action, "status": "failed", "error": "任务不存在"})
                        return
                    if action == "stop" and task["status"] in ("queued", "running"):
                        self.harness.stop(task_id)
                        task = self.store.row(task_id)
                else:
                    task = None
                if action == "status" and task_id is None:
                    with self.store.lock:
                        owned_rows = [dict(r) for r in self.store.db.execute(
                            "SELECT t.id,t.title,t.status,t.updated_at FROM mail_commands c JOIN tasks t ON t.id=c.task_id "
                            "WHERE c.contact_id=? AND c.action='submit' ORDER BY c.received_at DESC LIMIT 10", (contact["id"],))]
                    report = {"action": "status", "tasks": [{"taskId": r["id"], "title": self._redact_result(r["title"], 100),
                                                                "status": r["status"], "updatedAt": r["updated_at"]} for r in owned_rows]}
                else:
                    report = self._report(action, task, task_id)
            self._send_reply(command_id, contact, report)
        except Exception:
            if action in ("submit", "status", "stop"):
                self._send_reply(command_id, contact, {"action": action, "status": "failed", "error": "DAXQO 无法处理该命令"})
            else:
                self._finish_command(command_id, "failed", "DAXQO 无法处理该命令")

    def _report(self, action: str, task: dict | None, task_id: str | None) -> dict:
        if task is None: return {"action": action, "status": "no_active_task"}
        result = {"action": action, "taskId": task_id, "status": task["status"], "title": self._redact_result(task.get("title", ""), 200)}
        if task.get("result") is not None: result["result"] = self._redact_result(task["result"], 4000)
        if task.get("error"): result["error"] = self._redact_result(task["error"], 500)
        return result

    def _redact_result(self, value, limit: int) -> str:
        # Redact before truncation: a credential crossing the output boundary
        # must not leak a prefix that no longer matches the complete secret.
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        with self._auth_lock: password = self._config.get("password", "")
        if password: text = text.replace(password, "[redacted]")
        with self.store.lock:
            secrets_for_contacts = [r[0] for r in self.store.db.execute("SELECT secret FROM mail_contacts")]
        for secret in secrets_for_contacts:
            text = text.replace(secret, "[redacted]")
        return self.harness.safe_text(text, len(text))[:limit]

    @staticmethod
    def _utf8_prefix(value: str, budget: int) -> str:
        return value.encode("utf-8")[:budget].decode("utf-8", "ignore")

    def _bounded_report(self, value: dict) -> dict:
        report = dict(value)
        report["truncated"] = False
        result = report.get("result")
        if isinstance(result, str) and len(result.encode("utf-8")) > 4200:
            report["result"] = self._utf8_prefix(result, 4200)
            report["truncated"] = True
        error = report.get("error")
        if isinstance(error, str) and len(error.encode("utf-8")) > 800:
            report["error"] = self._utf8_prefix(error, 800)
            report["truncated"] = True
        tasks = report.get("tasks")
        if isinstance(tasks, list):
            tasks = list(tasks)
            report["tasks"] = tasks
            while tasks and len(_safe_json(report)) > 7600:
                tasks.pop()
                report["truncated"] = True
        for key in ("result", "error"):
            text = report.get(key)
            if isinstance(text, str):
                while len(_safe_json(report)) > 7600 and text:
                    text = self._utf8_prefix(text, max(0, len(text.encode("utf-8")) - 256))
                    report[key] = text
                    report["truncated"] = True
        if len(_safe_json(report)) > 7600:
            # Reports contain only public summary fields. Keep their envelope bounded
            # even when a future field is added unexpectedly.
            return {"action": report.get("action", "result"), "status": report.get("status", "completed"), "truncated": True}
        return report

    def _send_reply(self, command_id: str, contact, report: dict) -> None:
        report = self._bounded_report(report)
        with self._auth_lock:
            with self.store.lock:
                active = self.store.db.execute("SELECT 1 FROM mail_contacts WHERE id=? AND email=? AND enabled=1 AND deleted_at IS NULL", (contact["id"], contact["email"])).fetchone()
            config = dict(self._config)
            if not active or not config.get("enabled"):
                self._finish_command(command_id, "interrupted", "联系人已停用")
                return
            try:
                with self.store.lock, self.store.db:
                    cursor = self.store.db.execute("UPDATE mail_commands SET status='replying',response_json=? WHERE id=? AND status NOT IN ('replying','completed','reply_uncertain')",
                                                   (_safe_json(report).decode("utf-8"), command_id))
                    if cursor.rowcount != 1: return
            except Exception:
                return
        payload = sign({"v": 1, "action": "result", "contact": contact["email"], "nonce": uuid.uuid4().hex,
                        "sentAt": _stamp(), "report": report}, contact["secret"])
        message = EmailMessage()
        message["From"] = config["accountAddress"]
        message["To"] = contact["email"]  # recipient always comes from the trusted contact row
        message["Subject"] = "DAXQO/1 " + payload["nonce"]
        message["Message-ID"] = make_msgid()
        message.set_content(_safe_json(payload).decode("utf-8"))
        try:
            with smtplib.SMTP_SSL(config["smtpHost"], 465, context=ssl.create_default_context(), timeout=15) as smtp:
                smtp.login(config["username"], config["password"])
                # Linearize authorization with send. Revocation that completes
                # before this boundary prevents transmission; an in-flight SMTP
                # send finishes before a competing settings change completes.
                with self._auth_lock:
                    with self.store.lock:
                        active = self.store.db.execute("SELECT 1 FROM mail_contacts WHERE id=? AND email=? AND enabled=1 AND deleted_at IS NULL", (contact["id"], contact["email"])).fetchone()
                    if not active or not self._config.get("enabled") or config != self._config:
                        self._finish_command(command_id, "interrupted", "邮箱设置或联系人授权已变化")
                        return
                    smtp.send_message(message)
            outcome = report.get("status") if report.get("status") in ("rejected", "failed") else "completed"
            self._finish_command(command_id, outcome, None, replied=True)
        except Exception:
            # Do not retry: the server may have accepted the message before the error.
            self._finish_command(command_id, "reply_uncertain", "回复邮件状态不确定")

    def _finish_command(self, command_id: str, status: str, error: str | None, *, replied: bool = False) -> None:
        with self.store.lock, self.store.db:
            self.store.db.execute("UPDATE mail_commands SET status=?,error=?,replied_at=? WHERE id=?",
                                  (status, error, _stamp() if replied else None, command_id))

    def _deliver_finished(self) -> None:
        with self.store.lock:
            # Include revoked/deleted contacts so finished commands converge. The
            # existing _send_reply authorization gate marks them interrupted and
            # refuses SMTP delivery.
            rows = [dict(r) for r in self.store.db.execute("SELECT c.*,p.email,p.secret FROM mail_commands c JOIN mail_contacts p ON p.id=c.contact_id WHERE c.action='submit' AND c.status='running' AND c.task_id IS NOT NULL ORDER BY c.received_at")]
        for command in rows:
            task = self.store.row(command["task_id"])
            if not task or task["status"] in ("queued", "running"): continue
            self._send_reply(command["id"], {"id": command["contact_id"], "email": command["email"], "secret": command["secret"]},
                             self._report("result", task, command["task_id"]))

    def handle_http(self, handler, method: str, path: str) -> bool:
        handled = (path == "/api/remote" and method == "GET") or (path in ("/api/remote/settings", "/api/remote/test", "/api/remote/contacts") and method == "POST")
        parts = path.split("/")
        handled = handled or (len(parts) == 6 and parts[:4] == ["", "api", "remote", "contacts"] and method == "POST")
        if not handled: return False
        try:
            if path == "/api/remote": handler._json(200, self.public())
            elif path == "/api/remote/settings":
                self.save_settings(handler._body())
                handler._json(200, self.public())
            elif path == "/api/remote/test": handler._body(); handler._json(200, self.test_connection())
            elif path == "/api/remote/contacts": handler._json(201, self.create_contact(handler._body()))
            else:
                body = handler._body()
                if not isinstance(body, dict): raise ValueError("联系人设置格式无效")
                if parts[5] == "toggle":
                    if set(body) != {"enabled"}: raise ValueError("请提供enabled布尔值")
                    result = self.contact_action(parts[4], parts[5], enabled=body["enabled"])
                elif parts[5] == "delete":
                    if body: raise ValueError("删除联系人不接受额外字段")
                    result = self.contact_action(parts[4], parts[5])
                else: raise KeyError("路由不存在")
                handler._json(200, result)
        except KeyError:
            handler._json(404, {"error": "Not found"})
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            handler._json(400, {"error": self.harness.safe_text(str(exc), 300)})
        except Exception:
            handler._json(500, {"error": "本机邮箱服务操作失败"})
        return True
