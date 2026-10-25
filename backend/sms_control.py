"""Opt-in, number-allowlisted SMS task intake over the authenticated Android bridge."""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
import time
import uuid

from android_mcp_server import AndroidBridge, BridgeError

MAX_BODY_CHARS = 2000
MAX_QUERY = 50
MAX_ENABLED_CONTACTS = 50
MAX_REPLY_UTF16 = 70
POLL_SECONDS = 4
REPLY_CONFIRM_TIMEOUT_MS = 5 * 60 * 1000
RESULT_PREFIX = "DAXQO/RESULT "


def _stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _millis(value: str | None) -> int:
    if not value: return 0
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None: return 0
        return int(parsed.timestamp() * 1000)
    except (ValueError, TypeError, OverflowError):
        return 0


def normalize_china_mobile(value: str) -> str:
    if not isinstance(value, str): raise ValueError("手机号必须是中国大陆11位手机号码")
    value = value.strip()
    if re.fullmatch(r"1[3-9][0-9]{9}", value): digits = value
    elif re.fullmatch(r"(?:\+?86|0086)(1[3-9][0-9]{9})", value): digits = re.fullmatch(r"(?:\+?86|0086)(1[3-9][0-9]{9})", value).group(1)
    else: raise ValueError("仅支持+86中国大陆11位手机号码")
    return "+86" + digits


class SmsControlService:
    """Persistent SMS polling and exactly-once task/reply attempts.

    Carrier sender numbers are caller-ID allowlists, not cryptographic identities.
    The service is constructed in tests but only polled after an explicit start().
    """

    def __init__(self, harness):
        self.harness = harness
        self.store = harness.store
        self.private_dir = Path(harness.provider_dir)
        self.private_dir.mkdir(parents=True, exist_ok=True)
        try: self.private_dir.chmod(0o700)
        except OSError: pass
        self.config_path = self.private_dir / "sms.json"
        self.bridge = AndroidBridge(self.harness.state)
        self._lock = threading.RLock()
        self._generation = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error: str | None = None
        with self.store.lock, self.store.db:
            self.store.db.execute("""CREATE TABLE IF NOT EXISTS sms_contacts (
                id TEXT PRIMARY KEY, number TEXT NOT NULL UNIQUE, label TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
                enabled_since TEXT NOT NULL)""")
            self.store.db.execute("""CREATE TABLE IF NOT EXISTS sms_commands (
                id TEXT PRIMARY KEY, subscription_id INTEGER NOT NULL, native_id TEXT NOT NULL,
                contact_id TEXT, sender TEXT NOT NULL, received_at TEXT NOT NULL,
                body TEXT, status TEXT NOT NULL, task_id TEXT, error TEXT,
                reply_status TEXT NOT NULL DEFAULT 'not_due', reply_id TEXT,
                replied_at TEXT, UNIQUE(subscription_id,native_id))""")
            self.store.db.execute("""CREATE TABLE IF NOT EXISTS sms_cursors (
                subscription_id INTEGER PRIMARY KEY, after_id INTEGER NOT NULL DEFAULT 0)""")
            # An RPC can have created a task/SMS before the process died. Never replay it.
            self.store.db.execute("UPDATE sms_commands SET status='uncertain',error='Backend restarted during task dispatch' WHERE status='dispatching'")
            self.store.db.execute("UPDATE sms_commands SET reply_status='unknown' WHERE reply_status='sending'")
        self._config = self._read_config()

    @staticmethod
    def _default_config() -> dict:
        return {"enabled": False, "replyEnabled": False, "enabledSince": None, "subscriptionId": None}

    def _read_config(self) -> dict:
        default = self._default_config()
        if self.config_path.is_symlink():
            self.last_error = "短信配置路径不安全"
            return default
        try:
            info = self.config_path.stat()
            if os.name != "nt" and stat.S_IMODE(info.st_mode) & 0o077:
                self.last_error = "短信配置文件权限不安全"
                return default
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or set(raw) != set(default): raise ValueError
            if type(raw["enabled"]) is not bool or type(raw["replyEnabled"]) is not bool: raise ValueError
            sub = raw["subscriptionId"]
            if sub is not None and (isinstance(sub, bool) or not isinstance(sub, int) or sub < 0): raise ValueError
            since = raw["enabledSince"]
            if since is not None and (not isinstance(since, str) or not _millis(since)): raise ValueError
            if raw["enabled"] and (sub is None or since is None): raise ValueError
            return raw
        except FileNotFoundError:
            return default
        except Exception:
            self.last_error = "短信配置无法读取"
            return default

    def _save_config(self, config: dict) -> None:
        if self.config_path.is_symlink(): raise ValueError("短信配置路径不能是符号链接")
        raw = json.dumps(config, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        temp = self.private_dir / ("sms.json." + uuid.uuid4().hex + ".tmp")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(raw); handle.flush(); os.fsync(handle.fileno())
            os.replace(temp, self.config_path)
            try: self.config_path.chmod(0o600)
            except OSError: pass
        finally:
            try: temp.unlink()
            except FileNotFoundError: pass

    def _device_status(self) -> dict:
        result = self.bridge.request("GET", "/api/sms/status")
        if not isinstance(result, dict): raise BridgeError("invalid_response", "Android SMS status is invalid")
        return result

    @staticmethod
    def _eligible_subscription(status: dict, subscription_id: int) -> dict:
        entries = status.get("subscriptions")
        if not isinstance(entries, list): raise ValueError("手机未提供可验证的SIM卡列表")
        item = next((row for row in entries if isinstance(row, dict)
                     and type(row.get("subscriptionId")) is int
                     and row["subscriptionId"] == subscription_id), None)
        if not item: raise ValueError("所选SIM卡不可用")
        country = str(item.get("countryIso", "")).lower()
        if country in ("gb", "uk"): raise ValueError("英国SIM卡不支持短信任务")
        if country != "cn" or item.get("eligible") is not True or item.get("blockedReason"):
            raise ValueError("仅支持已识别为中国大陆且可用的SIM卡")
        return item

    def set_settings(self, body: dict) -> dict:
        if not isinstance(body, dict) or set(body) != {"enabled", "subscriptionId", "replyEnabled"}:
            raise ValueError("短信设置字段无效")
        if type(body["enabled"]) is not bool or type(body["replyEnabled"]) is not bool:
            raise ValueError("短信启用状态必须是布尔值")
        sub = body["subscriptionId"]
        if sub is not None and (isinstance(sub, bool) or not isinstance(sub, int) or sub < 0):
            raise ValueError("SIM卡编号无效")
        with self._lock:
            previous = dict(self._config)
            if body["enabled"]:
                if sub is None: raise ValueError("启用短信任务前必须选择SIM卡")
                status = self._device_status()
                self._eligible_subscription(status, sub)
                permissions = status.get("permissions") or {}
                required = ("readSms", "readPhoneState") + (("sendSms",) if body["replyEnabled"] else ())
                if (body["replyEnabled"] and status.get("ready") is not True) or any(
                        permissions.get(name) is not True for name in required):
                    raise ValueError("请先完成短信和SIM卡权限设置")
                changed = not previous["enabled"] or previous["subscriptionId"] != sub
                since = _stamp() if changed else previous["enabledSince"]
                self.bridge.request("POST", "/api/sms/config", {"enabled": True, "subscriptionId": sub})
                updated = {"enabled": True, "replyEnabled": body["replyEnabled"],
                           "enabledSince": since, "subscriptionId": sub}
            else:
                sub = sub if sub is not None else previous.get("subscriptionId")
                try:
                    payload = {"enabled": False}
                    if sub is not None: payload["subscriptionId"] = sub
                    self.bridge.request("POST", "/api/sms/config", payload)
                except BridgeError:
                    # Local disable takes effect even if the device is currently unreachable.
                    self.last_error = "手机暂不可达；本机已停止处理短信"
                updated = {"enabled": False, "replyEnabled": body["replyEnabled"],
                           "enabledSince": previous.get("enabledSince"), "subscriptionId": sub}
            self._save_config(updated)
            self._config = updated
            self._generation += 1
            if not updated["enabled"] or previous.get("subscriptionId") != updated.get("subscriptionId"):
                with self.store.lock, self.store.db:
                    self.store.db.execute("UPDATE sms_commands SET status='ignored',body=NULL,error='SMS control or selected SIM changed' WHERE status='pending'")
            if not updated["enabled"] or not updated["replyEnabled"]:
                with self.store.lock, self.store.db:
                    self.store.db.execute("UPDATE sms_commands SET reply_status='suppressed' WHERE reply_status='not_due'")
            elif previous.get("subscriptionId") != updated.get("subscriptionId"):
                with self.store.lock, self.store.db:
                    self.store.db.execute("UPDATE sms_commands SET reply_status='suppressed' WHERE reply_status='not_due' AND subscription_id<>?",
                                          (updated["subscriptionId"],))
        return self.public()

    def add_contact(self, body: dict) -> dict:
        if not isinstance(body, dict) or set(body) - {"number", "label"} or "number" not in body:
            raise ValueError("联系人字段无效")
        number = normalize_china_mobile(body["number"])
        label = body.get("label", "")
        if not isinstance(label, str) or len(label.strip()) > 80 or any(ord(c) < 32 for c in label):
            raise ValueError("联系人备注最多80字且不能含控制字符")
        label = label.strip()
        stamp = _stamp()
        ident = str(uuid.uuid4())
        try:
            with self._lock, self.store.lock, self.store.db:
                self.store.db.execute("INSERT INTO sms_contacts(id,number,label,enabled,created_at,enabled_since) VALUES(?,?,?,0,?,?)",
                                      (ident, number, label, stamp, stamp))
                self._generation += 1
        except sqlite3.IntegrityError:
            raise ValueError("该手机号码已登记") from None
        return {"ok": True, "contact": {"id": ident, "number": number, "label": label,
                                        "enabled": False, "createdAt": stamp}}

    def contact_action(self, ident: str, action: str, body: dict) -> dict:
        if not isinstance(body, dict): raise ValueError("联系人请求字段无效")
        if action == "toggle":
            if set(body) != {"enabled"} or type(body["enabled"]) is not bool:
                raise ValueError("请提供enabled布尔值")
            with self._lock, self.store.lock, self.store.db:
                row = self.store.db.execute("SELECT enabled FROM sms_contacts WHERE id=?", (ident,)).fetchone()
                if not row: raise KeyError("短信联系人不存在")
                enabled = body["enabled"]
                if enabled and not row["enabled"]:
                    count = self.store.db.execute("SELECT count(*) FROM sms_contacts WHERE enabled=1").fetchone()[0]
                    if count >= MAX_ENABLED_CONTACTS: raise ValueError("最多启用50个短信联系人")
                since = _stamp() if enabled and not row["enabled"] else None
                if since:
                    self.store.db.execute("UPDATE sms_contacts SET enabled=1,enabled_since=? WHERE id=?", (since, ident))
                else:
                    self.store.db.execute("UPDATE sms_contacts SET enabled=? WHERE id=?", (int(enabled), ident))
                    if not enabled:
                        self.store.db.execute("UPDATE sms_commands SET status='ignored',body=NULL,error='Contact disabled' WHERE contact_id=? AND status='pending'", (ident,))
                        self.store.db.execute("UPDATE sms_commands SET reply_status='suppressed' WHERE contact_id=? AND reply_status='not_due'", (ident,))
                self._generation += 1
            return {"ok": True}
        if action == "delete":
            if body: raise ValueError("删除联系人不接受请求字段")
            with self._lock, self.store.lock, self.store.db:
                cursor = self.store.db.execute("DELETE FROM sms_contacts WHERE id=?", (ident,))
                if not cursor.rowcount: raise KeyError("短信联系人不存在")
                self.store.db.execute("UPDATE sms_commands SET status='ignored',body=NULL,error='Contact deleted' WHERE contact_id=? AND status='pending'", (ident,))
                self.store.db.execute("UPDATE sms_commands SET reply_status='suppressed' WHERE contact_id=? AND reply_status='not_due'", (ident,))
                self._generation += 1
            return {"ok": True}
        raise KeyError("短信联系人路由不存在")

    def public(self) -> dict:
        with self._lock:
            config = dict(self._config)
        status, ready, error = {}, False, None
        try:
            status = self._device_status()
            sub = config.get("subscriptionId")
            if sub is not None:
                item = self._eligible_subscription(status, sub)
                permissions = status.get("permissions") or {}
                required = ("readSms", "readPhoneState") + (("sendSms",) if config["replyEnabled"] else ())
                ready = bool((status.get("ready") is True or not config["replyEnabled"])
                             and status.get("selectedSubscriptionId") == sub
                             and all(permissions.get(k) is True for k in required)
                             and item.get("eligible") is True)
        except (BridgeError, ValueError) as exc:
            error = str(exc)
        with self.store.lock:
            contacts = [dict(row) for row in self.store.db.execute(
                "SELECT id,number,label,enabled,created_at FROM sms_contacts ORDER BY created_at")]
            commands = [dict(row) for row in self.store.db.execute(
                "SELECT id,sender,task_id,status,received_at,replied_at,reply_status FROM sms_commands ORDER BY rowid DESC LIMIT 50")]
        return {"sms": {"enabled": config["enabled"], "replyEnabled": config["replyEnabled"],
                         "enabledSince": config["enabledSince"], "ready": ready,
                         "lastError": self.harness.safe_text(error or self.last_error, 200) if (error or self.last_error) else status.get("lastError"),
                         "selectedSubscriptionId": config["subscriptionId"],
                         "subscriptions": status.get("subscriptions", []),
                         "permissions": status.get("permissions", {})},
                "contacts": [{"id": r["id"], "number": r["number"], "label": r["label"],
                              "enabled": bool(r["enabled"]), "createdAt": r["created_at"]} for r in contacts],
                "commands": [{"id": r["id"], "sender": r["sender"], "taskId": r["task_id"],
                              "status": r["status"], "receivedAt": r["received_at"],
                              "repliedAt": r["replied_at"], "replyStatus": r["reply_status"]} for r in commands]}

    def start(self) -> None:
        if self._thread and self._thread.is_alive(): return
        self._stop.clear()
        self._thread = threading.Thread(target=self._poll_loop, name="sms-control", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread(): self._thread.join(timeout=2)

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try: self.poll_once()
            except Exception as exc: self.last_error = self.harness.safe_text(type(exc).__name__, 100)
            self._stop.wait(POLL_SECONDS)

    def poll_once(self) -> None:
        with self._lock:
            config = dict(self._config)
            generation = self._generation
        if not config["enabled"]: return
        status = self._device_status()
        sub = config.get("subscriptionId")
        item = self._eligible_subscription(status, sub)
        permissions = status.get("permissions") or {}
        required = ("readSms", "readPhoneState") + (("sendSms",) if config["replyEnabled"] else ())
        if ((config["replyEnabled"] and status.get("ready") is not True) or status.get("selectedSubscriptionId") != sub
                or any(permissions.get(key) is not True for key in required)
                or item.get("eligible") is not True):
            raise ValueError("短信服务未就绪或SIM卡已变化")
        self.last_error = None
        self._poll_reply_statuses(config, status, generation)
        self._dispatch_pending(config, generation)
        with self.store.lock:
            contacts = [dict(row) for row in self.store.db.execute(
                "SELECT id,number,enabled_since FROM sms_contacts WHERE enabled=1 ORDER BY created_at")]
            cursor_row = self.store.db.execute("SELECT after_id FROM sms_cursors WHERE subscription_id=?", (sub,)).fetchone()
        if not contacts: return
        after_id = int(cursor_row[0]) if cursor_row else 0
        since = _millis(config.get("enabledSince"))
        result = self.bridge.request("POST", "/api/sms/query", {"since": since, "afterId": after_id,
            "limit": MAX_QUERY, "senders": [row["number"] for row in contacts]})
        if not isinstance(result, dict) or result.get("subscriptionId") != sub or not isinstance(result.get("messages"), list):
            raise BridgeError("invalid_response", "Android SMS query response is invalid")
        messages = result["messages"]
        previous = after_id
        for message in messages:
            if not isinstance(message, dict): raise BridgeError("invalid_response", "Android SMS message is invalid")
            native_id = message.get("id")
            if not isinstance(native_id, str) or not native_id.isdigit():
                raise BridgeError("invalid_response", "Android SMS id is invalid")
            number_id = int(native_id)
            if number_id <= previous: raise BridgeError("invalid_response", "Android SMS ids are not strictly increasing")
            previous = number_id
            self._ingest(sub, message, contacts, config, generation)
            with self._lock:
                if generation != self._generation or not self._config.get("enabled"):
                    break
            with self.store.lock, self.store.db:
                self.store.db.execute("INSERT INTO sms_cursors(subscription_id,after_id) VALUES(?,?) ON CONFLICT(subscription_id) DO UPDATE SET after_id=MAX(after_id,excluded.after_id)", (sub, number_id))
        self._dispatch_pending(config, generation)

    def _ingest(self, sub: int, message: dict, contacts: list[dict], config: dict, generation: int) -> None:
        try: sender = normalize_china_mobile(message.get("sender"))
        except ValueError: return
        contact = next((row for row in contacts if row["number"] == sender), None)
        if not contact: return
        if message.get("subscriptionId") != sub: raise BridgeError("invalid_response", "SMS arrived from an unselected SIM")
        received = message.get("receivedAt")
        body = message.get("body")
        if isinstance(received, bool) or not isinstance(received, int) or received < 0: raise BridgeError("invalid_response", "SMS timestamp is invalid")
        if not isinstance(body, str) or len(body) > MAX_BODY_CHARS: body = ""
        now_ms = int(time.time() * 1000)
        with self._lock:
            if generation != self._generation or not self._config.get("enabled"): return
            if _millis(self._config.get("enabledSince")) > received or _millis(contact["enabled_since"]) > received:
                status = "ignored"
                safe_body = None
            elif body.strip().upper().startswith(RESULT_PREFIX):
                status = "ignored"
                safe_body = None
            elif received > now_ms + 120_000:
                status = "ignored"
                safe_body = None
            elif not body.strip():
                status = "ignored"
                safe_body = None
            else:
                status = "pending"
                safe_body = body
            command_id = str(uuid.uuid4())
            stamp = dt.datetime.fromtimestamp(received / 1000, tz=dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            with self.store.lock, self.store.db:
                try:
                    self.store.db.execute("""INSERT INTO sms_commands
                        (id,subscription_id,native_id,contact_id,sender,received_at,body,status,reply_status)
                        VALUES(?,?,?,?,?,?,?,?,?)""", (command_id, sub, message["id"], contact["id"], sender,
                        stamp, safe_body, status, "not_due"))
                except Exception as exc:
                    if "UNIQUE" in str(exc).upper(): return
                    raise

    def _dispatch_pending(self, config: dict, generation: int) -> None:
        with self.store.lock:
            rows = [dict(row) for row in self.store.db.execute(
                "SELECT * FROM sms_commands WHERE status='pending' ORDER BY received_at,rowid LIMIT 20")]
        for row in rows:
            with self._lock:
                if generation != self._generation or not self._config.get("enabled"): return
                with self.store.lock, self.store.db:
                    contact = self.store.db.execute("SELECT number,enabled,enabled_since FROM sms_contacts WHERE id=?", (row["contact_id"],)).fetchone()
                    current = self.store.db.execute("SELECT * FROM sms_commands WHERE id=?", (row["id"],)).fetchone()
                    if not current or current["status"] != "pending": continue
                    received = _millis(row["received_at"])
                    if (row["subscription_id"] != self._config.get("subscriptionId")
                            or not contact or not contact["enabled"] or contact["number"] != row["sender"]
                            or received < max(_millis(self._config.get("enabledSince")), _millis(contact["enabled_since"]))):
                        self.store.db.execute("UPDATE sms_commands SET status='ignored',body=NULL,error='Contact or feature is no longer enabled' WHERE id=?", (row["id"],))
                        continue
                    if row["body"] and row["body"].strip().upper().startswith(RESULT_PREFIX):
                        self.store.db.execute("UPDATE sms_commands SET status='ignored',body=NULL WHERE id=?", (row["id"],))
                        continue
                    self.store.db.execute("UPDATE sms_commands SET status='dispatching' WHERE id=?", (row["id"],))
                try:
                    task = self.harness.start_task({"prompt": row["body"]})
                except ValueError as exc:
                    reason = str(exc)
                    with self.store.lock, self.store.db:
                        if "并发任务数已达上限" in reason:
                            self.store.db.execute("UPDATE sms_commands SET status='pending' WHERE id=?", (row["id"],))
                            return
                        self.store.db.execute("UPDATE sms_commands SET status='failed',body=NULL,error=? WHERE id=?",
                                              (self.harness.safe_text(reason, 300), row["id"]))
                    continue
                except Exception as exc:
                    with self.store.lock, self.store.db:
                        self.store.db.execute("UPDATE sms_commands SET status='uncertain',body=NULL,error=? WHERE id=?",
                                              (type(exc).__name__, row["id"]))
                    continue
                with self.store.lock, self.store.db:
                    self.store.db.execute("UPDATE sms_commands SET status='started',task_id=?,body=NULL WHERE id=?",
                                          (task["id"], row["id"]))

    def _poll_reply_statuses(self, config: dict, status: dict, generation: int) -> None:
        with self.store.lock:
            rows = [dict(row) for row in self.store.db.execute(
                "SELECT * FROM sms_commands WHERE status='started' AND reply_status IN ('not_due','queued') ORDER BY rowid LIMIT 50")]
        for row in rows:
            if row["reply_status"] == "queued":
                requested_at = _millis(row["replied_at"])
                if not requested_at or int(time.time() * 1000) - requested_at >= REPLY_CONFIRM_TIMEOUT_MS:
                    # A vendor confirmation dialog or carrier may never report back.
                    # The SMS can still arrive later; do not turn this into a resend.
                    self._mark_reply(row["id"], "unknown")
                    continue
                if not row["reply_id"]: self._mark_reply(row["id"], "unknown", replied=True); continue
                try:
                    result = self.bridge.request("GET", "/api/sms/sends/" + row["reply_id"])
                    returned_id = result.get("id") if isinstance(result, dict) else None
                    if isinstance(returned_id, int) and not isinstance(returned_id, bool): returned_id = str(returned_id)
                    if returned_id != row["reply_id"]:
                        self._mark_reply(row["id"], "unknown", replied=True)
                        continue
                    self._save_reply_result(row["id"], result)
                except BridgeError as exc:
                    if exc.code not in ("timeout", "unavailable"): self.last_error = "短信发送状态暂不可查"
                continue
            if row["subscription_id"] != config.get("subscriptionId"):
                self._mark_reply(row["id"], "suppressed")
                continue
            task = self.harness.store.row(row["task_id"])
            if not task or task["status"] not in ("completed", "failed", "stopped", "interrupted", "cancelled"):
                continue
            result_text = task.get("result") or task.get("error") or "任务已结束"
            text = _short_result(result_text)
            request_id = "daxqo-sms-result-" + row["id"]
            payload = {"subscriptionId": config["subscriptionId"], "to": row["sender"],
                       "text": text, "clientRequestId": request_id}
            # Keep the service lock across the one-shot send so a disable/contact
            # revocation linearizes before or after, never after an unapproved send.
            with self._lock:
                if (generation != self._generation or not self._config.get("enabled")
                        or not self._config.get("replyEnabled")
                        or self._config.get("subscriptionId") != config.get("subscriptionId")
                        or row["subscription_id"] != self._config.get("subscriptionId")):
                    self._mark_reply(row["id"], "suppressed")
                    continue
                with self.store.lock:
                    contact = self.store.db.execute("SELECT number,enabled,enabled_since FROM sms_contacts WHERE id=?", (row["contact_id"],)).fetchone()
                if (not contact or not contact["enabled"] or contact["number"] != row["sender"]
                        or _millis(row["received_at"]) < max(_millis(self._config.get("enabledSince")),
                                                               _millis(contact["enabled_since"]))):
                    self._mark_reply(row["id"], "suppressed")
                    continue
                if row["reply_status"] != "not_due": continue
                # Persist claim before crossing the device boundary. Never resend an uncertain send.
                self._mark_reply(row["id"], "sending")
                try:
                    response = self.bridge.request("POST", "/api/sms/send", payload)
                except BridgeError:
                    self._mark_reply(row["id"], "unknown", replied=True)
                    continue
                self._save_reply_result(row["id"], response, replied=True)

    def _mark_reply(self, command_id: str, status: str, *, replied: bool = False) -> None:
        with self.store.lock, self.store.db:
            self.store.db.execute("UPDATE sms_commands SET reply_status=?,replied_at=CASE WHEN ? THEN ? ELSE replied_at END WHERE id=? AND reply_status IN ('not_due','queued','sending')",
                                  (status, int(replied), _stamp(), command_id))

    def _save_reply_result(self, command_id: str, result: dict, *, replied: bool = False) -> None:
        if not isinstance(result, dict):
            self._mark_reply(command_id, "unknown", replied=True); return
        status = result.get("status")
        if status not in ("queued", "sent", "delivered", "failed", "unknown"):
            self._mark_reply(command_id, "unknown", replied=True); return
        ident = result.get("id")
        if isinstance(ident, int) and not isinstance(ident, bool): ident = str(ident)
        if ident is not None and (not isinstance(ident, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", ident)):
            self._mark_reply(command_id, "unknown", replied=True); return
        if ident is None:
            self._mark_reply(command_id, "unknown", replied=True); return
        if status == "queued" and ident is None:
            self._mark_reply(command_id, "unknown", replied=True); return
        with self.store.lock, self.store.db:
            self.store.db.execute("UPDATE sms_commands SET reply_status=?,reply_id=COALESCE(?,reply_id),replied_at=CASE WHEN ? THEN ? WHEN ? IN ('sent','delivered','failed','unknown') THEN ? ELSE replied_at END WHERE id=? AND reply_status IN ('not_due','queued','sending')",
                (status, ident, int(replied), _stamp(), status, _stamp(), command_id))


def _short_result(value: str) -> str:
    text = " ".join(str(value).split())
    prefix = RESULT_PREFIX
    budget = MAX_REPLY_UTF16 - len(prefix.encode("utf-16-le")) // 2
    output = []
    units = 0
    for char in text:
        count = len(char.encode("utf-16-le", errors="ignore")) // 2
        if units + count > budget: break
        output.append(char); units += count
    return prefix + "".join(output)
