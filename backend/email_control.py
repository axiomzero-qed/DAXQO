"""Authenticated email command transport for the local DAXQO harness."""
from __future__ import annotations

from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import format_datetime, getaddresses, make_msgid, parsedate_to_datetime
from html.parser import HTMLParser
import datetime as dt
import hashlib
import imaplib
import json
import os
from pathlib import Path
import re
import secrets
import smtplib
import sqlite3
import ssl
import stat
import sys
import threading
import time
import uuid

from remote_protocol import mail_payload_text, normalize_email, sign, verify

MAX_MESSAGE = 96 * 1024
MAX_JSON = 64 * 1024
POLL_SECONDS = 15
DEFAULT_IMAP = "imap.163.com"
DEFAULT_SMTP = "smtp.163.com"
PLAIN_MAX_AGE = 24 * 60 * 60
PLAIN_MAX_PROMPT = 8000

# Optional, app-local dependencies; absence disables ordinary mail only.
_mail_libs = Path(__file__).resolve().parent / "runtime" / "mail-libs"
if _mail_libs.is_dir() and not _mail_libs.is_symlink():
    sys.path.insert(0, str(_mail_libs))
try:
    import dkim
except ImportError:
    dkim = None


class _MailHTMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"): self.hidden += 1
        if not self.hidden and tag in ("br", "p", "div", "li", "h1", "h2", "h3", "tr"): self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head") and self.hidden: self.hidden -= 1
        if not self.hidden and tag in ("p", "div", "li", "tr"): self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden: self.parts.append(data)


class _AuthenticatedMailRejected(ValueError):
    """A verified sender may receive a bounded explanation of invalid content."""
    def __init__(self, reason, command):
        super().__init__(reason)
        self.command = command


def _plain_date(message, since: str, now=None):
    """Untrusted dates may exclude candidates, but never authorize them."""
    try:
        values = message.get_all("Date", [])
        if len(values) != 1: raise ValueError
        date = parsedate_to_datetime(str(values[0]))
        if date.tzinfo is None: raise ValueError
        date = date.astimezone(dt.timezone.utc)
        start = dt.datetime.fromisoformat(since.replace("Z", "+00:00"))
        current = now or dt.datetime.now(dt.timezone.utc)
        age = (current - date).total_seconds()
        if date < start or age > PLAIN_MAX_AGE or age < -120: raise ValueError
        return date
    except (ValueError, TypeError, OverflowError, AttributeError):
        raise ValueError("仅处理启用后的新邮件，且邮件日期须在24小时内") from None


def _plain_approval_command(text: str):
    """Recognize a decision only on the first non-empty authenticated body line."""
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    match = re.fullmatch(r"(确认|拒绝)\s+([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})", first)
    if match:
        try:
            approval_id = str(uuid.UUID(match.group(2)))
        except ValueError:
            approval_id = None
        if approval_id:
            return ("approve" if match.group(1) == "确认" else "decline", approval_id)
    terminal = first.rstrip("。！？.!?").strip()
    if terminal in {"确认", "同意", "批准", "允许", "确认安装", "同意安装", "我确认需要安装，你来点确认"}:
        return ("approve_unambiguous", None)
    # Ambiguous confirmation prose must never fall through and become a new task.
    if first.startswith(("确认", "拒绝", "批准", "继续", "你来点确认", "我确认需要安装")):
        return ("approval_help", None)
    return None


def _approval_like_subject(subject: str) -> bool:
    candidate = subject.strip()
    while True:
        match = re.match(r"^(?:re|fw|fwd)\s*:\s*", candidate, flags=re.IGNORECASE)
        if not match:
            break
        candidate = candidate[match.end():]
    return _plain_approval_command(candidate) is not None


def _verify_plain_email(raw: bytes, sender: str, receiver: str, since: str, *, now=None, dnsfunc=None) -> dict:
    """Authenticate the complete received bytes before interpreting instructions.

    Initial provider scope is personal Gmail. Never trust Authentication-Results
    or a From header alone. DKIM verification is delegated to dkimpy.
    """
    if not raw or len(raw) > MAX_MESSAGE: raise ValueError("邮件超过96KiB限制")
    if dkim is None: raise ValueError("普通邮件身份验证组件尚未安装")
    if normalize_email(sender).rsplit("@", 1)[1] != "gmail.com":
        raise ValueError("普通邮件自动处理目前仅支持已登记的 Gmail 联系人")
    try:
        msg = BytesParser(policy=policy.default).parsebytes(raw)
    except Exception:
        raise ValueError("邮件格式无效，未创建任务") from None
    required = {"from", "to", "subject", "date", "message-id", "content-type", "mime-version"}
    if any(len(msg.get_all(name, [])) != 1 for name in required):
        raise ValueError("邮件关键头缺失或重复，无法确认身份")
    for name in ("content-transfer-encoding", "sender", "reply-to"):
        if len(msg.get_all(name, [])) > 1: raise ValueError("邮件头重复，无法确认身份")
    addresses = [a for _, a in getaddresses(msg.get_all("From", [])) if a]
    if addresses != [sender] or any(msg.get_all(name, []) for name in ("Resent-From", "Resent-To", "Resent-Date")):
        raise ValueError("发件人与可信联系人不一致")
    recipients = [a.lower() for _, a in getaddresses(msg.get_all("To", [])) if a]
    if receiver not in recipients: raise ValueError("邮件须直接发送到本机邮箱")
    _plain_date(msg, since, now)
    message_id = str(msg["Message-ID"]).strip()
    if not re.fullmatch(r"<[^<>\s\x00-\x20\x7f]{1,240}>", message_id):
        raise ValueError("邮件唯一标识无效")
    # An unsigned MIME interpretation header can change the meaning of a signed body.
    if msg.get_all("Content-Transfer-Encoding", []): required.add("content-transfer-encoding")
    try:
        verifier = dkim.DKIM(raw, minkey=2048, timeout=5)
    except Exception:
        raise ValueError("邮件签名格式无效，未创建任务") from None
    authenticated = False
    signatures = [value for name, value in verifier.headers if name.lower() == b"dkim-signature"]
    for index, value in enumerate(signatures[:5]):
        try:
            fields = dkim.parse_tag_value(value)
            signed = {name.strip().lower().decode("ascii") for name in fields.get(b"h", b"").split(b":")}
            if (fields.get(b"d", b"").lower() != b"gmail.com" or fields.get(b"a") != b"rsa-sha256"
                    or b"l" in fields or not required.issubset(signed)):
                continue
            if verifier.verify(index, **({"dnsfunc": dnsfunc} if dnsfunc is not None else {})):
                authenticated = True
                break
        except Exception:
            continue
    if not authenticated: raise ValueError("Gmail 发件签名未通过验证，未执行；请检查网络或使用 Agent/MCP")
    if msg.get("Auto-Submitted", "no").lower() != "no" or msg.get("List-Id"):
        raise ValueError("自动通知或邮件列表不作为任务执行")
    # Preserve the complete decoded subject for task interpretation. Receipt
    # and UI presentation call _receipt_subject with its default short limit.
    subject = MailControlService._receipt_subject(msg["Subject"], limit=None)
    nonce = "plain:" + hashlib.sha256((sender + "\n" + message_id).encode("utf-8")).hexdigest()
    command = {"action": "submit", "title": subject[:100] or "邮箱任务", "nonce": nonce,
               "messageId": message_id, "subject": subject}
    if any(part.get_content_disposition() == "attachment" or part.get_filename() for part in msg.walk()):
        raise _AuthenticatedMailRejected("普通邮件任务暂不支持附件，请直接在正文说明任务", command)
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        # An empty MIME container has no decodable body to prefer. Treat only
        # that genuinely empty structure as a title-only request; unsupported
        # or malformed body parts still fail closed.
        payload = msg.get_payload()
        if payload not in (None, "", []):
            raise _AuthenticatedMailRejected("邮件正文无法解码", command)
        text = ""
    else:
        try:
            text = part.get_content()
            if not isinstance(text, str): raise ValueError
            if part.get_content_type() == "text/html":
                parser = _MailHTMLText()
                parser.feed(text)
                text = "".join(parser.parts)
        except Exception:
            raise _AuthenticatedMailRejected("邮件正文无法解码", command) from None
    text = text.strip()
    if not text:
        # A subject may itself be a simple task. Never turn a subject line into
        # an approval decision, including common reply/forward subject prefixes.
        if _approval_like_subject(subject):
            return {**command, "action": "approval_help", "title": subject[:100] or "审批决定"}
        if not subject.strip():
            raise _AuthenticatedMailRejected("请在主题或正文中提供任务内容", command)
        prompt = ("邮件主题：" + subject).strip()
        if len(prompt) > PLAIN_MAX_PROMPT:
            raise _AuthenticatedMailRejected("主题与正文合计不超过8000字", command)
        return {**command, "title": subject[:100], "prompt": prompt}
    prompt = ("邮件主题：" + subject + "\n\n" + text).strip()
    if len(prompt) > PLAIN_MAX_PROMPT:
        raise _AuthenticatedMailRejected("主题与正文合计不超过8000字", command)
    decision = _plain_approval_command(text)
    if decision:
        action, approval_id = decision
        return {**command, "action": action, "approvalId": approval_id, "title": subject[:100] or "审批决定"}
    return {**command, "prompt": prompt}


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
        self._auth_generation = 0
        self._stop = threading.Event()
        self._thread = None
        self._last_poll_at = None
        self._plain_retry_after = {}
        self._plain_cursor = 0
        with self.store.lock, self.store.db:
            self.store.db.execute("CREATE TABLE IF NOT EXISTS mail_contacts (id TEXT PRIMARY KEY,email TEXT NOT NULL UNIQUE,secret TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,deleted_at TEXT,deleted_email TEXT)")
            columns = {row[1] for row in self.store.db.execute("PRAGMA table_info(mail_contacts)")}
            if "deleted_at" not in columns: self.store.db.execute("ALTER TABLE mail_contacts ADD COLUMN deleted_at TEXT")
            if "deleted_email" not in columns: self.store.db.execute("ALTER TABLE mail_contacts ADD COLUMN deleted_email TEXT")
            if "plain_since" not in columns: self.store.db.execute("ALTER TABLE mail_contacts ADD COLUMN plain_since TEXT")
            self.store.db.execute("CREATE TABLE IF NOT EXISTS mail_commands (id TEXT PRIMARY KEY,contact_id TEXT NOT NULL,nonce TEXT NOT NULL UNIQUE,action TEXT NOT NULL,task_id TEXT,status TEXT NOT NULL,received_at TEXT NOT NULL,replied_at TEXT,error TEXT,response_json TEXT,FOREIGN KEY(contact_id) REFERENCES mail_contacts(id))")
            command_columns = {row[1] for row in self.store.db.execute("PRAGMA table_info(mail_commands)")}
            for name, definition in (("transport", "TEXT NOT NULL DEFAULT 'signed'"), ("subject", "TEXT"),
                                     ("message_id", "TEXT"), ("ack_status", "TEXT")):
                if name not in command_columns: self.store.db.execute(f"ALTER TABLE mail_commands ADD COLUMN {name} {definition}")
            self.store.db.execute("CREATE TABLE IF NOT EXISTS mail_receipts (id TEXT PRIMARY KEY,contact_id TEXT NOT NULL,contact_email TEXT NOT NULL,subject TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL,received_at TEXT NOT NULL,header_hash TEXT NOT NULL UNIQUE)")
            self.store.db.execute("CREATE TABLE IF NOT EXISTS mail_approval_notices (approval_id TEXT PRIMARY KEY,task_id TEXT NOT NULL,contact_id TEXT NOT NULL,command_id TEXT NOT NULL UNIQUE,status TEXT NOT NULL,created_at TEXT NOT NULL)")
            receipt_columns = {row[1] for row in self.store.db.execute("PRAGMA table_info(mail_receipts)")}
            if "task_id" not in receipt_columns: self.store.db.execute("ALTER TABLE mail_receipts ADD COLUMN task_id TEXT")
        with self.store.lock, self.store.db:
            self.store.db.execute("UPDATE mail_commands SET status='interrupted',error='Backend restarted before command completed' WHERE status='claimed'")
            self.store.db.execute("UPDATE mail_commands SET status='reply_uncertain',error='Backend restarted during reply send' WHERE status='replying'")
            self.store.db.execute("UPDATE mail_approval_notices SET status='reply_uncertain' WHERE status='sending'")
            self.store.db.execute("UPDATE mail_commands SET ack_status='uncertain' WHERE ack_status='sending'")
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
        allowed = {"enabled", "accountAddress", "username", "imapHost", "imapPort", "smtpHost", "smtpPort", "password", "plainEmailEnabled", "plainEmailSince"}
        if set(data) - allowed:
            raise ValueError("邮箱配置字段无效")
        if "enabled" in data and type(data["enabled"]) is not bool:
            raise ValueError("邮箱启用状态无效")
        if "plainEmailEnabled" in data and type(data["plainEmailEnabled"]) is not bool:
            raise ValueError("普通邮件开关无效")
        since = data.get("plainEmailSince", "")
        if since:
            try:
                if not isinstance(since, str) or len(since) > 64 or dt.datetime.fromisoformat(since.replace("Z", "+00:00")).tzinfo is None: raise ValueError
            except (ValueError, TypeError): raise ValueError("普通邮件生效时间无效") from None
        if data.get("plainEmailEnabled") and not since: raise ValueError("普通邮件生效时间缺失")
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
                  "smtpHost": data.get("smtpHost", DEFAULT_SMTP), "smtpPort": data.get("smtpPort", 465), "password": password,
                  "plainEmailEnabled": data.get("plainEmailEnabled", False), "plainEmailSince": since}
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
            receipts = [dict(r) for r in self.store.db.execute("SELECT r.id,r.contact_email,r.subject,r.status,r.reason,r.received_at,r.task_id FROM mail_receipts r JOIN mail_contacts p ON p.id=r.contact_id WHERE p.enabled=1 AND p.deleted_at IS NULL ORDER BY r.received_at DESC LIMIT 12")]
        return {"mail": {"enabled": config.get("enabled", False), "accountAddress": config.get("accountAddress", ""),
                          "configured": bool(config.get("accountAddress") and config.get("username") and config.get("password")),
                          "username": config.get("username", ""), "imapHost": config.get("imapHost", DEFAULT_IMAP),
                          "imapPort": 993, "smtpHost": config.get("smtpHost", DEFAULT_SMTP), "smtpPort": 465,
                          "passwordConfigured": bool(config.get("password")), "lastPollAt": self._last_poll_at,
                          "plainEmailEnabled": config.get("plainEmailEnabled", False), "plainEmailSince": config.get("plainEmailSince", ""),
                          "plainEmailReady": dkim is not None,
                          "plainEmailReason": "Gmail 发件签名验证已就绪" if dkim is not None else "请安装 DAXQO 邮件身份验证组件",
                          "lastError": self.harness.safe_text(self.last_error, 200) if self.last_error else None},
                "contacts": [{"id": r["id"], "email": r["email"], "enabled": bool(r["enabled"]), "keyConfigured": True,
                              "createdAt": r["created_at"], **({"lastUsedAt": r["last_used_at"]} if r["last_used_at"] else {})} for r in contacts],
                "receipts": [{"id": r["id"], "contactEmail": r["contact_email"], "subject": r["subject"],
                              "status": r["status"], "reason": r["reason"], "receivedAt": r["received_at"],
                              **({"taskId": r["task_id"]} if r["task_id"] else {})} for r in receipts],
                "commands": [{"id": r["id"], "contactId": r["contact_id"], "action": r["action"],
                              "contactEmail": r["contact_email"] or "联系人",
                              **({"taskId": r["task_id"]} if r["task_id"] else {}), "status": r["status"],
                              "receivedAt": r["received_at"], **({"repliedAt": r["replied_at"]} if r["replied_at"] else {}),
                              **({"error": self.harness.safe_text(r["error"], 200)} if r["error"] else {})} for r in commands]}

    def save_settings(self, body: dict) -> dict:
        if not isinstance(body, dict): raise ValueError("设置格式无效")
        allowed = {"enabled", "accountAddress", "username", "imapHost", "imapPort", "smtpHost", "smtpPort", "password", "plainEmailEnabled"}
        if set(body) - allowed: raise ValueError("邮箱设置含不支持字段")
        with self._auth_lock:
            data = dict(self._config)
            data.update({k: v for k, v in body.items() if k != "password"})
            if body.get("password"):
                data["password"] = body["password"]
            if data.get("plainEmailEnabled") and (not self._config.get("plainEmailEnabled")
                    or not self._config.get("enabled") and data.get("enabled")
                    or data.get("accountAddress") != self._config.get("accountAddress")):
                data["plainEmailSince"] = _stamp()
            validated = self._validated_config(data)
            self._save_config(validated)
            self._config = validated
            self._auth_generation += 1
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
                    self.store.db.execute("DELETE FROM mail_receipts WHERE contact_id=?", (ident,))
            elif action == "toggle":
                if row["deleted_at"] is not None: raise KeyError("联系人已删除")
                if type(enabled) is not bool: raise ValueError("请提供enabled布尔值")
                self.store.db.execute("UPDATE mail_contacts SET enabled=?,plain_since=CASE WHEN enabled=0 AND ?=1 THEN ? ELSE plain_since END WHERE id=?", (int(enabled), int(enabled), _stamp(), ident))
            else: raise KeyError("路由不存在")
            self._auth_generation += 1
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
            plain_candidates = []
            for msg_id in ids[-100:]:
                size_type, size_parts = imap.fetch(msg_id, "(RFC822.SIZE)")
                size_match = None
                for part in size_parts or []:
                    size_header = part[0] if isinstance(part, tuple) else part
                    if isinstance(size_header, bytes):
                        import re
                        size_match = re.search(rb"RFC822\.SIZE\s+(\d+)", size_header, re.I)
                        if size_match: break
                if size_type != "OK" or not size_match:
                    continue
                message_size = int(size_match.group(1))
                if message_size > MAX_MESSAGE:
                    continue
                header_type, header_parts = imap.fetch(msg_id, "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM MESSAGE-ID DATE)])")
                header_raw = next((part[1] for part in header_parts or []
                                   if isinstance(part, tuple) and isinstance(part[1], bytes)), b"") if header_type == "OK" else b""
                if not header_raw or len(header_raw) > 8192:
                    continue
                try:
                    headers = BytesParser(policy=policy.default).parsebytes(header_raw + b"\r\n\r\n")
                except Exception:
                    continue
                contact = self._trusted_header_contact(headers)
                if not contact:
                    continue
                subject_values = headers.get_all("Subject", [])
                subject = self._receipt_subject(subject_values[0] if len(subject_values) == 1 else "")
                header_hash = hashlib.sha256(header_raw).hexdigest()
                ordinary = len(subject_values) == 1 and not str(subject_values[0]).strip().startswith("DAXQO/1 ")
                if len(subject_values) != 1:
                    self._record_receipt(contact, subject, "邮件主题缺失或重复，未创建任务", header_hash)
                    continue
                if ordinary:
                    if self._plain_header_claimed(contact, headers): continue
                    if not config.get("plainEmailEnabled"):
                        self._record_receipt(contact, subject, "普通邮件未包含签名命令，未创建任务；可在本机开启本人 Gmail 普通邮件自动处理", header_hash)
                        continue
                    try:
                        if dkim is None: raise ValueError("普通邮件身份验证组件尚未安装")
                        if contact["email"].rsplit("@", 1)[1] != "gmail.com":
                            raise ValueError("普通邮件自动处理目前仅支持已登记的 Gmail 联系人")
                        _plain_date(headers, self._plain_since(config, contact))
                    except ValueError as error:
                        self._record_receipt(contact, subject, str(error), header_hash)
                        continue
                    if self._plain_retry_after.get(header_hash, 0) > time.monotonic(): continue
                    plain_candidates.append((msg_id, message_size, contact, subject, header_hash))
                    continue
                raw = self._fetch_body(imap, msg_id, message_size)
                if not raw:
                    self._record_receipt(contact, subject, "邮件无法完整读取，未创建任务", header_hash)
                    continue
                if not self._process_email(raw) and not self._has_claimed_nonce(contact, raw):
                    self._record_receipt(contact, subject, "签名命令无效或已处理，未创建新任务", header_hash)
            # Fair rotation prevents repeated invalid signatures from starving new
            # ordinary tasks. The cursor only schedules candidates; it never authenticates.
            plain_candidates.sort(key=lambda item: int(item[0]))
            ordered = [item for item in plain_candidates if int(item[0]) > self._plain_cursor]
            ordered += [item for item in plain_candidates if int(item[0]) <= self._plain_cursor]
            for msg_id, message_size, contact, subject, header_hash in ordered[:5]:
                self._plain_cursor = int(msg_id)
                raw = self._fetch_body(imap, msg_id, message_size)
                if not raw:
                    self._record_receipt(contact, subject, "邮件无法完整读取，未创建任务", header_hash)
                    continue
                if not self._process_plain_email(raw, contact, header_hash):
                    self._plain_retry_after[header_hash] = time.monotonic() + 60
                    if len(self._plain_retry_after) > 200:
                        self._plain_retry_after = {key: value for key, value in self._plain_retry_after.items() if value > time.monotonic()}
        finally:
            if imap:
                try: imap.logout()
                except Exception: pass

    @staticmethod
    def _fetch_body(imap, msg_id, message_size):
        typ, parts = imap.fetch(msg_id, f"(BODY.PEEK[]<0.{MAX_MESSAGE}>)")
        raw = next((part[1] for part in parts or [] if isinstance(part, tuple) and isinstance(part[1], bytes)), b"") if typ == "OK" else b""
        return raw if message_size <= len(raw) <= MAX_MESSAGE else b""

    @staticmethod
    def _receipt_subject(value, *, limit=200) -> str:
        text = " ".join(str(value or "").split())
        text = "".join(ch for ch in text if ch >= " " and ch != "\x7f")
        return text if limit is None else text[:limit]

    def _trusted_header_contact(self, headers):
        values = [addr for _, addr in getaddresses(headers.get_all("From", [])) if addr]
        if len(values) != 1:
            return None
        try:
            sender = normalize_email(values[0])
        except (TypeError, ValueError):
            return None
        with self._auth_lock:
            if not self._config.get("enabled"):
                return None
            with self.store.lock:
                row = self.store.db.execute("SELECT id,email,created_at,plain_since FROM mail_contacts WHERE email=? AND enabled=1 AND deleted_at IS NULL", (sender,)).fetchone()
        return dict(row) if row else None

    def _record_receipt(self, contact: dict, subject: str, reason: str, header_hash: str, *, status="not_started", task_id=None) -> None:
        with self._auth_lock:
            if not self._config.get("enabled"):
                return
            with self.store.lock, self.store.db:
                active = self.store.db.execute("SELECT 1 FROM mail_contacts WHERE id=? AND email=? AND enabled=1 AND deleted_at IS NULL", (contact["id"], contact["email"])).fetchone()
                if not active:
                    return
                self.store.db.execute("INSERT INTO mail_receipts(id,contact_id,contact_email,subject,status,reason,received_at,header_hash,task_id) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(header_hash) DO UPDATE SET status=excluded.status,reason=excluded.reason,task_id=excluded.task_id",
                                      (str(uuid.uuid4()), contact["id"], contact["email"], subject[:200], status, reason[:200], _stamp(), header_hash, task_id))
                self.store.db.execute("DELETE FROM mail_receipts WHERE id NOT IN (SELECT id FROM mail_receipts ORDER BY received_at DESC LIMIT 100)")

    @staticmethod
    def _plain_since(config, contact):
        return max(config.get("plainEmailSince", ""), contact.get("plain_since") or contact.get("created_at", ""))

    def _plain_header_claimed(self, contact, headers):
        """Untrusted metadata can suppress a duplicate, never authorize an action."""
        values = headers.get_all("Message-ID", [])
        if len(values) != 1: return False
        nonce = "plain:" + hashlib.sha256((contact["email"] + "\n" + str(values[0]).strip()).encode("utf-8")).hexdigest()
        with self.store.lock:
            return self.store.db.execute("SELECT 1 FROM mail_commands WHERE nonce=?", (nonce,)).fetchone() is not None

    def _process_plain_email(self, raw, contact, header_hash):
        subject = ""
        try:
            with self._auth_lock:
                config = dict(self._config)
                generation = self._auth_generation
            if not config.get("enabled") or not config.get("plainEmailEnabled"): return False
            rejection = None
            try:
                command = _verify_plain_email(raw, contact["email"], config["accountAddress"], self._plain_since(config, contact))
            except _AuthenticatedMailRejected as error:
                command, rejection = error.command, str(error)
            subject = command["subject"]
            with self._auth_lock, self.store.lock, self.store.db:
                current = self.store.db.execute("SELECT * FROM mail_contacts WHERE id=? AND email=? AND enabled=1 AND deleted_at IS NULL", (contact["id"], contact["email"])).fetchone()
                # Revocation, re-enabling, and settings changes invalidate in-flight verification.
                if (not current or config != self._config or generation != self._auth_generation or current["plain_since"] != contact.get("plain_since")
                        or not self._config.get("plainEmailEnabled")): return False
                command_id = str(uuid.uuid4())
                try:
                    self.store.db.execute("INSERT INTO mail_commands(id,contact_id,nonce,action,status,received_at,transport,subject,message_id,ack_status) VALUES(?,?,?,?,'claimed',?,'plain',?,?,?)",
                                          (command_id, current["id"], command["nonce"], command["action"], _stamp(), subject, command["messageId"], "pending" if command["action"] == "submit" else None))
                except sqlite3.IntegrityError: return False
                authenticated_contact = dict(current)
            if rejection:
                self._send_reply(command_id, authenticated_contact, {"action": "result", "status": "rejected", "title": command["title"], "error": rejection})
                self._record_receipt(contact, subject, rejection, header_hash, status="failed")
                return False
            command["_plainConfig"] = config
            command["_plainGeneration"] = generation
            self._dispatch(command_id, authenticated_contact, command)
            with self.store.lock:
                row = self.store.db.execute("SELECT status,task_id FROM mail_commands WHERE id=?", (command_id,)).fetchone()
            if row and row["task_id"] and command["action"] == "submit":
                self._record_receipt(contact, subject, "已验证本人 Gmail，已创建任务", header_hash, status="accepted", task_id=row["task_id"])
                self._send_plain_ack(command_id, authenticated_contact, row["task_id"])
            elif row and command["action"] != "submit":
                self._record_receipt(contact, subject, "已验证本人 Gmail，已处理审批指令", header_hash, status="accepted", task_id=row["task_id"])
            else:
                self._record_receipt(contact, subject, "身份验证已通过，但任务未能启动，请查看最近命令", header_hash, status="failed")
            return bool(row and row["task_id"])
        except ValueError as error:
            if not subject:
                try: subject = self._receipt_subject(BytesParser(policy=policy.default).parsebytes(raw)["Subject"])
                except Exception: pass
            self._record_receipt(contact, subject, str(error), header_hash)
            return False

    def _has_claimed_nonce(self, contact: dict, raw: bytes) -> bool:
        """Suppress diagnostics for known historical commands; never authorizes execution."""
        try:
            message = BytesParser(policy=policy.default).parsebytes(raw)
            payload = json.loads(self._decode_body(message).decode("utf-8"))
            nonce = payload.get("nonce") if isinstance(payload, dict) else None
            if not isinstance(nonce, str) or not nonce:
                return False
        except Exception:
            return False
        with self.store.lock:
            return self.store.db.execute("SELECT 1 FROM mail_commands WHERE contact_id=? AND nonce=? LIMIT 1", (contact["id"], nonce)).fetchone() is not None

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

    def _process_email(self, raw: bytes) -> bool:
        if len(raw) > MAX_MESSAGE: return False
        try:
            msg = BytesParser(policy=policy.default).parsebytes(raw)
            sender_values = [addr for _, addr in getaddresses(msg.get_all("From", [])) if addr]
            if len(sender_values) != 1: return False
            sender = normalize_email(sender_values[0])
            content = self._decode_body(msg)
            if len(content) > MAX_JSON: return False
            payload = json.loads(content.decode("utf-8"))
            if not isinstance(payload, dict): return False
        except Exception:
            return False
        with self._auth_lock:
            if not self._config.get("enabled"): return False
            # Re-check the current authorization at the claim/launch boundary.
            with self.store.lock:
                contact = self.store.db.execute("SELECT id,email,secret FROM mail_contacts WHERE email=? AND enabled=1 AND deleted_at IS NULL", (sender,)).fetchone()
            if not contact or payload.get("contact") != sender: return False
            try:
                command = verify(payload, contact["secret"])
            except ValueError:
                return False
            command_id = str(uuid.uuid4())
            if str(msg.get("Subject", "")).strip() != "DAXQO/1 " + command["nonce"]:
                return False
            try:
                with self.store.lock, self.store.db:
                    self.store.db.execute("INSERT INTO mail_commands(id,contact_id,nonce,action,task_id,status,received_at) VALUES(?,?,?,?,NULL,'claimed',?)",
                                          (command_id, contact["id"], command["nonce"], command["action"], _stamp()))
            except sqlite3.IntegrityError:
                return False
        # Dispatch rechecks authorization before launch. Keep network reply operations
        # outside the authorization and SQLite locks.
        self._dispatch(command_id, contact, command)
        return True

    def _owned_task(self, contact_id: str, task_id: str):
        with self.store.lock:
            return self.store.db.execute("SELECT * FROM mail_commands WHERE contact_id=? AND task_id=? AND action='submit' ORDER BY received_at LIMIT 1", (contact_id, task_id)).fetchone()

    def _dispatch_approval(self, command_id, contact, command):
        action = command["action"]
        with self._auth_lock, self.store.lock:
            active = (self._config.get("enabled") and self.store.db.execute(
                "SELECT 1 FROM mail_contacts WHERE id=? AND enabled=1 AND deleted_at IS NULL", (contact["id"],)).fetchone())
        if not active:
            self._finish_command(command_id, "interrupted", "邮箱或联系人已停用")
            return
        if action == "approval_help":
            self._send_reply(command_id, contact, {"action": "result", "status": "instructions",
                "error": "如需处理权限审批，请在邮件首个非空行完整写：确认 <审批ID> 或 拒绝 <审批ID>。审批ID见待审批通知；没有ID时不会创建新任务。"})
            return

        requested_task_id = command.get("taskId")
        if action == "approve_unambiguous":
            with self.store.lock:
                owned_task_ids = [r[0] for r in self.store.db.execute(
                    "SELECT DISTINCT task_id FROM mail_commands WHERE contact_id=? AND task_id IS NOT NULL AND action='submit'",
                    (contact["id"],))]
            candidates = []
            now_ms = int(time.time() * 1000)
            for owned_id in owned_task_ids:
                for item in self.harness.android_permission_approvals(task_id=owned_id):
                    try: expires = int(item.get("expiresAt"))
                    except (TypeError, ValueError, OverflowError): continue
                    if item.get("taskId") == owned_id and item.get("status") == "pending" and expires > now_ms:
                        candidates.append(item)
            if len(candidates) != 1:
                self._send_reply(command_id, contact, {"action": "result", "status": "instructions",
                    "error": "当前联系人名下没有唯一一项有效待审批操作。请回复首行“确认 <审批ID>”或“拒绝 <审批ID>”，不会猜测审批目标。"})
                return
            approval = candidates[0]
            approval_id = str(approval["id"])
            action = "approve"
        else:
            try:
                approval_id = str(uuid.UUID(command.get("approvalId", "")))
            except (TypeError, ValueError, AttributeError):
                self._send_reply(command_id, contact, {"action": action, "status": "rejected", "error": "审批编号格式无效"})
                return
            approvals = (self.harness.android_permission_approvals(task_id=requested_task_id)
                         if requested_task_id else self.harness.android_permission_approvals())
            approval = next((item for item in approvals if str(item.get("id", "")).lower() == approval_id
                             and (not requested_task_id or item.get("taskId") == requested_task_id)), None)
        if not approval:
            self._send_reply(command_id, contact, {"action": action, "status": "rejected", "error": "未找到属于该任务的待处理审批"})
            return
        task_id = approval.get("taskId")
        if not isinstance(task_id, str) or not self._owned_task(contact["id"], task_id):
            self._send_reply(command_id, contact, {"action": action, "status": "rejected", "error": "该联系人无权处理此审批"})
            return
        if approval.get("status") != "pending":
            self._send_reply(command_id, contact, {"action": action, "taskId": task_id,
                "approvalId": approval["id"], "status": "rejected", "error": "此审批已过期、已处理或已失效"})
            return
        try:
            expires_at = int(approval.get("expiresAt"))
        except (TypeError, ValueError, OverflowError):
            expires_at = 0
        if expires_at <= int(time.time() * 1000):
            self._send_reply(command_id, contact, {"action": action, "taskId": task_id,
                "approvalId": approval["id"], "status": "rejected", "error": "此审批已过期"})
            return

        decision = "accept" if action == "approve" else "decline"
        # Serialize the final ownership/authorization check with contact revocation.
        decision_error = None
        result = None
        authorized = False
        with self._auth_lock:
            with self.store.lock:
                active = (self._config.get("enabled") and self.store.db.execute(
                    "SELECT 1 FROM mail_contacts WHERE id=? AND enabled=1 AND deleted_at IS NULL", (contact["id"],)).fetchone())
            if active and self._owned_task(contact["id"], task_id):
                authorized = True
                try:
                    # The harness acquires its Android-policy lock before the
                    # store lock; keep only _auth_lock across this call so
                    # revocation stays serialized without reversing lock order.
                    result = self.harness.decide_android_permission(approval_id, decision, task_id=task_id)
                except ValueError as error:
                    decision_error = self._redact_result(str(error), 300)
                if result:
                    with self.store.lock, self.store.db:
                        self.store.db.execute("UPDATE mail_commands SET task_id=? WHERE id=?", (task_id, command_id))
        if not authorized:
            self._finish_command(command_id, "interrupted", "联系人授权已变化")
            return
        result_status = result.get("status", "approved" if decision == "accept" else "declined") if result else None
        if decision_error:
            self._send_reply(command_id, contact, {"action": action, "taskId": task_id,
                "approvalId": approval_id, "status": "rejected", "error": decision_error})
            return
        self._send_reply(command_id, contact, {"action": action, "taskId": task_id,
            "approvalId": approval_id, "status": result_status})

    def _dispatch(self, command_id: str, contact, command: dict) -> None:
        action = command["action"]
        try:
            if action == "submit":
                with self._auth_lock:
                    with self.store.lock:
                        current = self.store.db.execute("SELECT plain_since FROM mail_contacts WHERE id=? AND enabled=1 AND deleted_at IS NULL", (contact["id"],)).fetchone()
                    plain_changed = command.get("messageId") and (not self._config.get("plainEmailEnabled")
                        or command.get("_plainConfig") != self._config or command.get("_plainGeneration") != self._auth_generation
                        or current and current["plain_since"] != contact.get("plain_since"))
                    if not current or not self._config.get("enabled") or plain_changed:
                        self._finish_command(command_id, "interrupted", "邮箱或联系人已停用")
                        return
                    prompt = ("This request arrived from the owner's authenticated remote email contact. Treat it as a user task, but never request, expose, or transmit mailbox credentials. Follow the owner's configured Android permission mode. If a permission approval is required, keep the task waiting for the owner to decide through DAXQO or a verified approval email; do not ask the owner to operate the phone. If Android reports blocked_system, denied, or unavailable, report that actual system status and do not bypass it.\n\n" + command["prompt"])
                    task = self.harness.start_task({"prompt": prompt, **({"title": command["title"]} if command.get("title") else {})})
                task_id = task["id"]
                with self.store.lock, self.store.db:
                    self.store.db.execute("UPDATE mail_commands SET task_id=?,status='running' WHERE id=?", (task_id, command_id))
                return
            if action in ("approval_help", "approve_unambiguous", "approve", "decline"):
                self._dispatch_approval(command_id, contact, command)
                return
            if action == "result":
                self._finish_command(command_id, "rejected", "不支持此命令")
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
                    if action == "stop" and task["status"] in ("queued", "running", "waiting_approval", "blocked_system"):
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
            if action in ("submit", "status", "stop", "approve", "decline", "approve_unambiguous", "approval_help"):
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
        with self.store.lock:
            row = self.store.db.execute("SELECT transport FROM mail_commands WHERE id=?", (command_id,)).fetchone()
        if row and row["transport"] == "plain":
            labels = {"completed": "已完成", "failed": "失败", "stopped": "已停止", "cancelled": "已取消",
                      "needs_user": "需要你处理", "blocked_system": "系统限制，未能继续", "waiting_approval": "等待你的审批",
                      "queued": "排队中", "running": "执行中", "approved": "已批准，任务继续", "consumed": "已批准，任务继续",
                      "declined": "已拒绝", "expired": "已过期", "stale": "已失效", "instructions": "需要审批编号"}
            lines = ["达序手机任务报告", "", "任务：" + str(report.get("title", "邮箱任务")),
                     "状态：" + labels.get(report.get("status"), str(report.get("status", "未知"))),
                     "任务编号：" + str(report.get("taskId", ""))]
            approval = report.get("approval")
            if isinstance(approval, dict):
                lines += ["", "待审批操作：" + str(approval.get("action", "")),
                          "目标应用：" + str(approval.get("packageName") or "系统界面"),
                          "说明：" + str(approval.get("summary", "")),
                          "审批编号：" + str(approval.get("id", "")),
                          "到期时间：" + str(approval.get("expiresAtText", "")),
                          "回复本邮件首行：确认 " + str(approval.get("id", "")) + "；或拒绝 " + str(approval.get("id", ""))]
            if report.get("result"): lines += ["", str(report["result"])]
            if report.get("error"): lines += ["", "处理说明：" + str(report["error"])]
            if report.get("truncated"): lines += ["", "报告已节选，完整内容请在手机 DAXQO 对话中查看。"]
            message = self._plain_message(command_id, config, contact, "达序任务报告：", "\n".join(lines))
        else:
            payload = sign({"v": 1, "action": "result", "contact": contact["email"], "nonce": uuid.uuid4().hex,
                            "sentAt": _stamp(), "report": report}, contact["secret"])
            message = EmailMessage()
            message["From"] = config["accountAddress"]
            message["To"] = contact["email"]  # recipient always comes from the trusted contact row
            message["Subject"] = "DAXQO/1 " + payload["nonce"]
            message["Message-ID"] = make_msgid()
            message.set_content(mail_payload_text(payload), subtype="plain", charset="utf-8", cte="base64")
        try:
            self._transmit(config, contact, message)
            outcome = report.get("status") if report.get("status") in ("rejected", "failed") else "completed"
            self._finish_command(command_id, outcome, None, replied=True)
        except PermissionError:
            self._finish_command(command_id, "interrupted", "邮箱设置或联系人授权已变化")
        except Exception:
            # Do not retry: the server may have accepted the message before the error.
            self._finish_command(command_id, "reply_uncertain", "回复邮件状态不确定")

    def _plain_message(self, command_id, config, contact, prefix, text):
        with self.store.lock:
            row = self.store.db.execute("SELECT subject,message_id FROM mail_commands WHERE id=?", (command_id,)).fetchone()
        message = EmailMessage()
        message["From"], message["To"] = config["accountAddress"], contact["email"]
        message["Subject"] = prefix + self._receipt_subject(row["subject"] if row else "邮箱任务")
        message["Message-ID"] = make_msgid()
        message["Date"] = format_datetime(dt.datetime.now(dt.timezone.utc))
        if row and row["message_id"]:
            message["In-Reply-To"] = row["message_id"]
            message["References"] = row["message_id"]
        message["Auto-Submitted"] = "auto-replied"
        message.set_content(text, subtype="plain", charset="utf-8", cte="base64")
        return message

    def _transmit(self, config, contact, message):
        with smtplib.SMTP_SSL(config["smtpHost"], 465, context=ssl.create_default_context(), timeout=15) as smtp:
            smtp.login(config["username"], config["password"])
            # The fixed recipient and authorization are rechecked at the send boundary.
            with self._auth_lock:
                with self.store.lock:
                    active = self.store.db.execute("SELECT 1 FROM mail_contacts WHERE id=? AND email=? AND enabled=1 AND deleted_at IS NULL", (contact["id"], contact["email"])).fetchone()
                if not active or not self._config.get("enabled") or config != self._config:
                    raise PermissionError("邮箱设置或联系人授权已变化")
                smtp.send_message(message)

    def _send_plain_ack(self, command_id, contact, task_id):
        with self._auth_lock:
            config = dict(self._config)
            with self.store.lock, self.store.db:
                cursor = self.store.db.execute("UPDATE mail_commands SET ack_status='sending' WHERE id=? AND transport='plain' AND ack_status='pending'", (command_id,))
                if cursor.rowcount != 1: return
        message = self._plain_message(command_id, config, contact, "达序已接单：",
            "已验证你的 Gmail 身份，已在手机创建任务，正在排队或执行。\n\n任务编号：" + task_id
            + "\n\n完成或遇到系统限制后会通过本邮箱回报，也可在手机 DAXQO 查看进度。若需要权限审批，可通过 DAXQO 或待审批邮件作决定；不要求你代为操作手机。")
        try:
            self._transmit(config, contact, message)
            outcome = "sent"
        except PermissionError:
            outcome = "disabled"
        except Exception:
            outcome = "uncertain"  # Never retry an ambiguously accepted SMTP send.
        with self.store.lock, self.store.db:
            self.store.db.execute("UPDATE mail_commands SET ack_status=? WHERE id=? AND ack_status='sending'", (outcome, command_id))

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
            if task["status"] == "waiting_approval":
                with self._auth_lock, self.store.lock:
                    mail_enabled = bool(self._config.get("enabled"))
                    contact_active = self.store.db.execute(
                        "SELECT 1 FROM mail_contacts WHERE id=? AND enabled=1 AND deleted_at IS NULL",
                        (command["contact_id"],)).fetchone()
                if not mail_enabled:
                    continue
                if not contact_active:
                    self._finish_command(command["id"], "interrupted", "联系人已停用")
                    continue
                self._deliver_approval_notices(command, task)
                continue
            self._send_reply(command["id"], {"id": command["contact_id"], "email": command["email"], "secret": command["secret"]},
                             self._report("result", task, command["task_id"]))

    def _deliver_approval_notices(self, command, task):
        approvals = self.harness.android_permission_approvals(task_id=command["task_id"])
        now_ms = int(time.time() * 1000)
        for item in approvals:
            if item.get("taskId") != command["task_id"] or item.get("status") != "pending":
                continue
            try:
                expires_ms = int(item.get("expiresAt"))
                expires_text = dt.datetime.fromtimestamp(expires_ms / 1000, dt.timezone.utc).astimezone(
                    dt.timezone(dt.timedelta(hours=8))).isoformat(timespec="seconds")
            except (TypeError, ValueError, OverflowError, OSError):
                continue
            if expires_ms <= now_ms:
                continue
            approval_id = item.get("id")
            try:
                canonical_id = str(uuid.UUID(approval_id))
            except (TypeError, ValueError, AttributeError):
                continue
            if canonical_id != approval_id.lower():
                continue
            contact = {"id": command["contact_id"], "email": command["email"], "secret": command["secret"]}
            if not self._owned_task(contact["id"], command["task_id"]):
                continue
            notice_id = str(uuid.uuid4())
            notice_nonce = "approval-notice:" + hashlib.sha256(approval_id.encode("ascii")).hexdigest()
            with self._auth_lock, self.store.lock, self.store.db:
                try:
                    self.store.db.execute("INSERT INTO mail_commands(id,contact_id,nonce,action,task_id,status,received_at,transport,subject) VALUES(?,?,?,'approval_notice',?,'claimed',?,?,?)",
                        (notice_id, contact["id"], notice_nonce, command["task_id"], _stamp(), command.get("transport", "signed"), "待处理的手机权限审批"))
                    self.store.db.execute("INSERT INTO mail_approval_notices(approval_id,task_id,contact_id,command_id,status,created_at) VALUES(?,?,?,?,'sending',?)",
                        (approval_id, command["task_id"], contact["id"], notice_id, _stamp()))
                except sqlite3.IntegrityError:
                    continue
            approval = {
                "id": approval_id,
                "action": self._redact_result(item.get("action", ""), 200),
                "packageName": self._redact_result(item.get("packageName") or "系统界面", 200),
                "summary": self._redact_result(item.get("summary", ""), 500),
                "expiresAt": expires_ms,
                "expiresAtText": expires_text,
                "replyFormat": "确认 " + approval_id + " / 拒绝 " + approval_id,
            }
            notice_report = {"action": "result", "status": "waiting_approval", "taskId": command["task_id"],
                             "title": self._redact_result(task.get("title", "邮箱任务"), 200), "approval": approval}
            self._send_reply(notice_id, contact, notice_report)
            with self.store.lock, self.store.db:
                status = self.store.db.execute("SELECT status FROM mail_commands WHERE id=?", (notice_id,)).fetchone()
                self.store.db.execute("UPDATE mail_approval_notices SET status=? WHERE approval_id=?",
                                      (status["status"] if status else "reply_uncertain", approval_id))

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
