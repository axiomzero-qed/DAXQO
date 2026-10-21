"""Persistent local settings, bounded file imports, and at-most-once schedules."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import stat
import threading
import uuid
from urllib.parse import parse_qs, urlsplit

UTC = dt.timezone.utc
SHANGHAI = dt.timezone(dt.timedelta(hours=8))
DEFAULT_SETTINGS = {"maxConcurrentTasks": 2, "subagentsEnabled": True,
                    "maxSubagents": 2, "autoCompactTokens": 32000, "contextWindow": 65536}
MAX_UPLOAD = 20 * 1024 * 1024


def stamp(value: dt.datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds")


class TaskServices:
    def __init__(self, harness, *, clock=None):
        self.harness, self.store = harness, harness.store
        self._clock = clock or (lambda: dt.datetime.now(UTC))
        self._stop = threading.Event()
        self._thread = None
        self._launch_lock = threading.Lock()
        self._settings_lock = threading.Lock()
        self._last_tick = None
        self._scheduler_error = None
        with self.store.lock, self.store.db:
            self.store.db.execute("CREATE TABLE IF NOT EXISTS daxqo_settings (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL)")
            self.store.db.execute("CREATE TABLE IF NOT EXISTS uploads (id TEXT PRIMARY KEY,name TEXT,path TEXT UNIQUE,size INTEGER,mime TEXT,sha256 TEXT,created_at TEXT)")
            self.store.db.execute("CREATE TABLE IF NOT EXISTS schedules (id TEXT PRIMARY KEY,title TEXT,prompt TEXT,kind TEXT,params TEXT,enabled INTEGER,next_run_at TEXT,last_run_at TEXT,last_task_id TEXT,last_status TEXT,last_error TEXT,created_at TEXT)")
            self.store.db.execute("CREATE TABLE IF NOT EXISTS schedule_runs (id TEXT PRIMARY KEY,schedule_id TEXT,task_id TEXT,status TEXT,started_at TEXT,finished_at TEXT,error TEXT)")
            row = self.store.db.execute("SELECT value FROM daxqo_settings WHERE id=1").fetchone()
            self.settings = dict(DEFAULT_SETTINGS)
            if row:
                try: self.settings = self._validate_settings(json.loads(row[0]))
                except (ValueError, TypeError): pass
            # A crash after claiming a run must never silently repeat an external action.
            self.store.db.execute("UPDATE schedule_runs SET status='interrupted',finished_at=?,error=? WHERE status='claimed'",
                (stamp(self._clock()), "启动状态不确定，请检查已有任务后手动重试"))
        self.effective_settings = dict(self.settings)

    def _validate_settings(self, body):
        if not isinstance(body, dict) or set(body) - set(DEFAULT_SETTINGS):
            raise ValueError("Unknown runtime setting")
        result = dict(getattr(self, "settings", DEFAULT_SETTINGS))
        result.update(body)
        for key, lower, upper in (("maxConcurrentTasks", 1, 4), ("maxSubagents", 1, 4),
                                  ("autoCompactTokens", 8000, 131072), ("contextWindow", 16384, 1048576)):
            if type(result[key]) is not int or not lower <= result[key] <= upper:
                raise ValueError(f"{key} must be an integer between {lower} and {upper}")
        if type(result["subagentsEnabled"]) is not bool:
            raise ValueError("subagentsEnabled must be a boolean")
        if result["autoCompactTokens"] > result["contextWindow"] * .8:
            raise ValueError("自动压缩阈值须低于上下文窗口的 80%")
        return result

    def _persist_settings(self):
        with self.store.lock, self.store.db:
            self.store.db.execute("INSERT INTO daxqo_settings(id,value) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET value=excluded.value",
                                  (json.dumps(self.settings),))

    def save_settings(self, body):
        with self._settings_lock:
            with self.harness.lock:
                new = self._validate_settings(body)
                previous = dict(self.settings)
                runtime_changed = any(new[k] != previous[k] for k in new if k != "maxConcurrentTasks")
                if runtime_changed and self._active_tasks():
                    raise ValueError("Tasks are running; runtime settings can be changed when idle")
                self.settings = new
                self._persist_settings()
            # Reconnect closes a reader thread that also takes harness.lock.
            # Never hold that lock while joining or replacing the engine.
            if runtime_changed:
                try: self.harness.reconnect()
                except Exception:
                    with self.harness.lock:
                        self.settings = previous
                        self._persist_settings()
                    try: self.harness.reconnect()
                    except Exception: pass
                    raise ValueError("设置已恢复：模型运行服务未能重新连接，请检查服务状态") from None
            self.effective_settings = dict(new)
        return self.public_settings()

    def public_settings(self):
        return {"settings": dict(self.settings), "runtimeEffective": dict(self.effective_settings),
                "scheduler": {"running": bool(self._thread and self._thread.is_alive()),
                              "timezone": "Asia/Shanghai", "requiresLinuxOnline": True,
                              "lastTickAt": self._last_tick, "error": self._scheduler_error}}

    def _active_tasks(self):
        return [row for row in self.store.rows() if row["status"] in ("queued", "running")]

    @staticmethod
    def _filename(value):
        if (not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > 220
                or any(ch in value for ch in '/\\\0:') or any(ord(ch) < 32 for ch in value)
                or value in (".", "..")):
            raise ValueError("文件名无效，请使用普通文件名")
        return value.strip()

    def upload(self, handler):
        if handler.headers.get("Transfer-Encoding"):
            raise ValueError("Transfer-Encoding is not supported")
        try: size = int(handler.headers.get("Content-Length", "0"))
        except ValueError: raise ValueError("Invalid Content-Length") from None
        if size < 0 or size > MAX_UPLOAD: raise OverflowError("每个附件最多 20 MiB")
        name = self._filename(parse_qs(urlsplit(handler.path).query).get("name", [""])[0])
        workspace = self.harness.workspace.resolve(strict=True)
        folder = workspace / "imports"
        if folder.is_symlink(): raise ValueError("附件目录不能是链接")
        folder.mkdir(mode=0o700, exist_ok=True)
        if not folder.is_dir() or folder.resolve() != folder:
            raise ValueError("附件目录不可用")
        ident = str(uuid.uuid4())
        relative = "imports/" + ident + "_" + name
        target = workspace / relative
        digest = hashlib.sha256()
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(target, flags, 0o600)
        connection = getattr(handler, "connection", None)
        previous_timeout = connection.gettimeout() if connection else None
        try:
            if connection: connection.settimeout(30)
            with os.fdopen(fd, "wb") as stream:
                remaining = size
                while remaining:
                    data = handler.rfile.read(min(remaining, 65536))
                    if not data: raise ValueError("附件上传不完整，请重新选择文件")
                    stream.write(data)
                    digest.update(data)
                    remaining -= len(data)
                stream.flush()
                os.fsync(stream.fileno())
            mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
            with self.store.lock, self.store.db:
                self.store.db.execute("INSERT INTO uploads VALUES(?,?,?,?,?,?,?)",
                    (ident, name, relative, size, mime, digest.hexdigest(), stamp(self._clock())))
        except Exception:
            target.unlink(missing_ok=True)
            raise
        finally:
            if connection: connection.settimeout(previous_timeout)
        return {"attachment": {"id": ident, "name": name, "path": relative, "size": size,
                               "mime": mime, "isImage": mime.startswith("image/"),
                               "visionSupported": False}}

    def prepare_body(self, body):
        if not isinstance(body, dict): raise ValueError("JSON object required")
        attachments = body.get("attachments", [])
        if not isinstance(attachments, list) or len(attachments) > 6:
            raise ValueError("每次消息最多 6 个附件")
        if any(not isinstance(ident, str) for ident in attachments):
            raise ValueError("Invalid attachment identifier")
        if not attachments: return body
        lines, public_attachments = [], []
        for ident in dict.fromkeys(attachments):
            if not isinstance(ident, str): raise ValueError("Invalid attachment identifier")
            with self.store.lock:
                row = self.store.db.execute("SELECT * FROM uploads WHERE id=?", (ident,)).fetchone()
            if not row: raise ValueError("附件不存在，请重新导入")
            path, _ = self.harness.resolve_workspace_path(row["path"])
            if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode) or path.stat().st_size > MAX_UPLOAD:
                raise ValueError("附件不可用")
            lines.append(f"- {json.dumps(row['name'], ensure_ascii=False)}: {json.dumps(str(path), ensure_ascii=False)} ({row['mime']}, {row['size']} bytes)")
            public_attachments.append({"id": ident, "name": row["name"], "path": row["path"],
                "size": row["size"], "mime": row["mime"], "isImage": row["mime"].startswith("image/"),
                "visionSupported": False})
        result = dict(body)
        field = "text" if "text" in body else "prompt"
        text = result.get(field, "")
        if not isinstance(text, str): raise ValueError("Message text must be a string")
        result["_displayText"] = text
        result["_attachments"] = public_attachments
        result[field] = text + "\n\n本次导入的附件（位于本机工作区，可用本地工具读取；不要假称已看懂图片）：\n" + "\n".join(lines)
        return result

    def _next(self, kind, params, current):
        if kind == "once":
            try: value = dt.datetime.fromisoformat(params["runAt"].replace("Z", "+00:00"))
            except (KeyError, ValueError, TypeError, AttributeError): raise ValueError("请提供含时区的执行时间") from None
            if value.tzinfo is None: raise ValueError("执行时间必须包含时区")
            return stamp(value)
        if kind == "interval":
            minutes = params.get("intervalMinutes")
            if type(minutes) is not int or not 1 <= minutes <= 10080:
                raise ValueError("执行间隔须为 1 至 10080 分钟")
            return stamp(current + dt.timedelta(minutes=minutes))
        if kind == "daily":
            try:
                value = params["time"]
                if not re.fullmatch(r"\d\d:\d\d", value): raise ValueError()
                hour, minute = map(int, value.split(":"))
                local = current.astimezone(SHANGHAI).replace(hour=hour, minute=minute, second=0, microsecond=0)
            except (KeyError, ValueError, TypeError): raise ValueError("每日时间格式为 HH:MM，北京时间") from None
            if local <= current: local += dt.timedelta(days=1)
            return stamp(local)
        raise ValueError("Unknown schedule kind")

    def create_schedule(self, body):
        title, prompt = body.get("title", ""), body.get("prompt", "")
        if not isinstance(title, str) or not title.strip() or len(title) > 100:
            raise ValueError("请填写 100 字以内的定时任务名称")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 32000:
            raise ValueError("请填写定时任务内容，最多 32000 字")
        kind, current = body.get("kind", "daily"), self._clock()
        params = {key: body[key] for key in ("runAt", "time", "intervalMinutes") if key in body}
        next_run = self._next(kind, params, current)
        if kind == "once" and next_run <= stamp(current): raise ValueError("一次任务的执行时间须在将来")
        enabled = body.get("enabled", True)
        if type(enabled) is not bool: raise ValueError("enabled must be a boolean")
        ident = str(uuid.uuid4())
        with self.store.lock, self.store.db:
            self.store.db.execute("INSERT INTO schedules VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (ident, title.strip(), prompt.strip(), kind, json.dumps(params), int(enabled), next_run,
                 None, None, None, None, stamp(current)))
        return self.schedule(ident)

    def schedule(self, ident):
        with self.store.lock:
            row = self.store.db.execute("SELECT * FROM schedules WHERE id=?", (ident,)).fetchone()
            if not row: raise KeyError("Schedule not found")
            runs = self.store.db.execute("SELECT * FROM schedule_runs WHERE schedule_id=? ORDER BY rowid DESC LIMIT 20", (ident,)).fetchall()
        value = dict(row)
        return {"id": value["id"], "title": value["title"], "prompt": value["prompt"], "kind": value["kind"],
                **json.loads(value["params"]), "enabled": bool(value["enabled"]),
                "nextRunAt": value["next_run_at"] if value["enabled"] else None,
                "lastRunAt": value["last_run_at"], "lastTaskId": value["last_task_id"],
                "lastStatus": value["last_status"], "lastError": value["last_error"],
                "lastRuns": [{"id": r["id"], "taskId": r["task_id"], "status": r["status"],
                              "startedAt": r["started_at"], "finishedAt": r["finished_at"], "error": r["error"]} for r in runs]}

    def schedules(self):
        self._reconcile()
        with self.store.lock:
            ids = [row[0] for row in self.store.db.execute("SELECT id FROM schedules ORDER BY created_at DESC")]
        return [self.schedule(ident) for ident in ids]

    def toggle_schedule(self, ident, enabled):
        if type(enabled) is not bool: raise ValueError("enabled must be a boolean")
        row = self.schedule(ident)
        next_run = self._next(row["kind"], row, self._clock())
        if enabled and row["kind"] == "once" and next_run <= stamp(self._clock()):
            raise ValueError("一次任务时间已过，请新建任务或选择立即运行")
        with self.store.lock, self.store.db:
            self.store.db.execute("UPDATE schedules SET enabled=?,next_run_at=? WHERE id=?", (int(enabled), next_run, ident))
        return self.schedule(ident)

    def run_schedule(self, ident, *, automatic=False):
        with self._launch_lock:
            self._reconcile()
            row = self.schedule(ident)
            if automatic and (not row["enabled"] or not row["nextRunAt"] or row["nextRunAt"] > stamp(self._clock())):
                return None
            if any(run["status"] in ("running", "claimed") for run in row["lastRuns"]):
                raise ValueError("This schedule is already running")
            if len(self._active_tasks()) >= self.settings["maxConcurrentTasks"]:
                raise ValueError("Task concurrency limit reached; waiting for capacity")
            run_id, current = str(uuid.uuid4()), self._clock()
            next_run = self._next(row["kind"], row, current)
            with self.store.lock, self.store.db:
                self.store.db.execute("INSERT INTO schedule_runs VALUES(?,?,?,?,?,?,?)",
                    (run_id, ident, None, "claimed", stamp(current), None, None))
                if automatic or (row["enabled"] and row["nextRunAt"] <= stamp(current)):
                    self.store.db.execute("UPDATE schedules SET enabled=?,next_run_at=? WHERE id=?",
                        (int(row["kind"] != "once"), next_run, ident))
                self.store.db.execute("UPDATE schedules SET last_run_at=?,last_status='starting',last_error=NULL WHERE id=?",
                    (stamp(current), ident))
            try:
                task = self.harness.start_task({"title": row["title"], "prompt": row["prompt"]})
                with self.store.lock, self.store.db:
                    self.store.db.execute("UPDATE schedule_runs SET task_id=?,status='running' WHERE id=?", (task["id"], run_id))
                    self.store.db.execute("UPDATE schedules SET last_task_id=?,last_status='running' WHERE id=?", (task["id"], ident))
                return {"task": task, "schedule": self.schedule(ident)}
            except Exception as exc:
                error = self.harness.safe_text(str(exc), 300)
                with self.store.lock, self.store.db:
                    self.store.db.execute("UPDATE schedule_runs SET status='failed',finished_at=?,error=? WHERE id=?",
                        (stamp(self._clock()), error, run_id))
                    self.store.db.execute("UPDATE schedules SET last_status='failed',last_error=? WHERE id=?", (error, ident))
                raise

    def _reconcile(self):
        with self.store.lock, self.store.db:
            rows = self.store.db.execute("SELECT r.id,r.schedule_id,r.task_id,t.status,t.error FROM schedule_runs r LEFT JOIN tasks t ON t.id=r.task_id WHERE r.status='running'").fetchall()
            for row in rows:
                if row["status"] in ("queued", "running"): continue
                status_value = row["status"] or "interrupted"
                error = row["error"] or ("关联任务不存在，请人工检查" if row["status"] is None else None)
                self.store.db.execute("UPDATE schedule_runs SET status=?,finished_at=?,error=? WHERE id=?",
                    (status_value, stamp(self._clock()), error, row["id"]))
                self.store.db.execute("UPDATE schedules SET last_status=?,last_error=? WHERE id=? AND last_task_id=?",
                    (status_value, error, row["schedule_id"], row["task_id"]))

    def tick(self):
        self._last_tick = stamp(self._clock())
        self._reconcile()
        with self.store.lock:
            due = [row[0] for row in self.store.db.execute("SELECT id FROM schedules WHERE enabled=1 AND next_run_at<=? ORDER BY next_run_at LIMIT 10", (stamp(self._clock()),))]
        for ident in due:
            if self._stop.is_set(): return
            try: self.run_schedule(ident, automatic=True)
            except ValueError as exc:
                if "capacity" in str(exc).lower():
                    with self.store.lock, self.store.db:
                        self.store.db.execute("UPDATE schedules SET last_status='waiting',last_error=NULL WHERE id=?", (ident,))
            except KeyError: pass
            except Exception: pass  # Launch failures are stored in the run history.

    def start(self):
        if self._thread and self._thread.is_alive(): return
        self._stop.clear()
        def loop():
            while not self._stop.wait(1):
                try:
                    self.tick()
                    self._scheduler_error = None
                except Exception as exc:
                    self._scheduler_error = "定时检查暂不可用：" + type(exc).__name__
        self._thread = threading.Thread(target=loop, name="daxqo-schedules", daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread(): self._thread.join(timeout=2)

    def handle_http(self, handler, method, path):
        if path not in ("/api/settings", "/api/uploads", "/api/schedules") and not path.startswith("/api/schedules/"):
            return False
        try:
            if path == "/api/settings":
                result = self.public_settings() if method == "GET" else self.save_settings(handler._body())
                handler._json(200, result)
            elif path == "/api/uploads" and method == "POST":
                handler._json(201, self.upload(handler))
            elif path == "/api/schedules":
                result = {"schedules": self.schedules()} if method == "GET" else {"schedule": self.create_schedule(handler._body())}
                handler._json(200 if method == "GET" else 201, result)
            else:
                parts = path.split("/")
                ident = parts[3] if len(parts) > 3 else ""
                if method == "GET" and len(parts) == 4:
                    handler._json(200, {"schedule": self.schedule(ident)})
                elif method == "POST" and len(parts) == 5:
                    action, body = parts[4], handler._body()
                    if action == "toggle": handler._json(200, {"schedule": self.toggle_schedule(ident, body.get("enabled"))})
                    elif action == "run": handler._json(202, self.run_schedule(ident))
                    elif action == "delete":
                        self.schedule(ident)
                        with self.store.lock, self.store.db:
                            self.store.db.execute("DELETE FROM schedules WHERE id=?", (ident,))
                        handler._json(200, {"deleted": True})
                    else: raise KeyError("Schedule action not found")
                else: raise KeyError("Not found")
        except KeyError:
            handler._json(404, {"error": "Not found"})
        except OverflowError as exc:
            handler._json(413, {"error": str(exc)})
        except (ValueError, TypeError) as exc:
            message = str(exc)
            code = 409 if any(word in message.lower() for word in ("running", "limit reached", "capacity")) else 400
            handler._json(code, {"error": self.harness.safe_text(message, 300)})
        except Exception:
            handler._json(500, {"error": "本机服务操作失败，请查看运行状态后重试"})
        return True
