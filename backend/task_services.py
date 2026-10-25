"""Persistent local settings, bounded file imports, and at-most-once schedules."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import mimetypes
import os
from pathlib import Path, PurePosixPath
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

    def _task_workspace(self, task_workspace: Path) -> tuple[Path, str]:
        configured_root = Path(os.path.abspath(self.harness.workspace))
        root = configured_root.resolve(strict=True)
        supplied = Path(task_workspace)
        if ".." in supplied.parts:
            raise ValueError("任务工作区路径无效")
        candidate = Path(os.path.abspath(supplied if supplied.is_absolute() else configured_root / supplied))
        relative = None
        for lexical_root in (configured_root, root):
            try:
                relative = candidate.relative_to(lexical_root)
                break
            except ValueError:
                continue
        if relative is None:
            raise ValueError("任务工作区必须位于本机工作区内") from None
        if not relative.parts or any(part in (".", "..") for part in relative.parts):
            raise ValueError("任务工作区路径无效")
        current = root
        for part in relative.parts:
            current = current / part
            try:
                info = current.lstat()
            except OSError as exc:
                raise ValueError("任务工作区不可用") from exc
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise ValueError("任务工作区不能包含链接或非目录")
        if current.resolve(strict=True) != current:
            raise ValueError("任务工作区不能包含链接")
        return current, relative.as_posix()

    @staticmethod
    def _hash_regular_file(path: Path, expected_size: int, expected_sha256: str) -> None:
        try:
            before = path.lstat()
            if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
                raise ValueError("附件必须是普通文件")
            if before.st_size != expected_size or not 0 <= expected_size <= MAX_UPLOAD:
                raise ValueError("附件大小与上传记录不符")
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) |
                         getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        except OSError as exc:
            raise ValueError("附件不可用") from exc
        digest = hashlib.sha256()
        size = 0
        try:
            with os.fdopen(fd, "rb") as source:
                opened = os.fstat(source.fileno())
                if (not stat.S_ISREG(opened.st_mode)
                        or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)):
                    raise ValueError("附件在读取前已更改")
                for chunk in iter(lambda: source.read(64 * 1024), b""):
                    size += len(chunk)
                    if size > expected_size:
                        raise ValueError("附件大小与上传记录不符")
                    digest.update(chunk)
                after = path.lstat()
                final = os.fstat(source.fileno())
                if (size != expected_size or digest.hexdigest() != expected_sha256
                        or stat.S_ISLNK(after.st_mode) or not stat.S_ISREG(after.st_mode)
                        or (final.st_dev, final.st_ino) != (before.st_dev, before.st_ino)
                        or (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino)
                        or final.st_size != expected_size or after.st_size != expected_size):
                    raise ValueError("附件内容与上传记录不符")
        except OSError as exc:
            raise ValueError("附件读取失败") from exc

    def _copy_upload_to_task(self, row, task_workspace: Path, task_workspace_rel: str) -> tuple[Path, str]:
        ident = row["id"]
        name = self._filename(row["name"])
        try:
            parsed_id = uuid.UUID(ident)
        except (ValueError, TypeError, AttributeError):
            raise ValueError("附件标识无效") from None
        if str(parsed_id) != ident:
            raise ValueError("附件标识无效")
        stored_relative = row["path"]
        expected_relative = f"imports/{ident}_{name}"
        if (not isinstance(stored_relative, str) or "\\" in stored_relative
                or PurePosixPath(stored_relative).is_absolute() or stored_relative != expected_relative):
            raise ValueError("附件路径无效")

        root = self.harness.workspace.resolve(strict=True)
        source_path = root / "imports" / f"{ident}_{name}"
        imports_dir = root / "imports"
        try:
            dir_info = imports_dir.lstat()
        except OSError as exc:
            raise ValueError("上传附件目录不可用") from exc
        if stat.S_ISLNK(dir_info.st_mode) or not stat.S_ISDIR(dir_info.st_mode) or imports_dir.resolve(strict=True) != imports_dir:
            raise ValueError("上传附件目录不能是链接")

        size, digest = row["size"], row["sha256"]
        if type(size) is not int or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("附件上传记录无效")
        # Verify the immutable upload before either reusing or creating a task copy.
        self._hash_regular_file(source_path, size, digest)

        input_dir = task_workspace / "inputs"
        try:
            if input_dir.exists() or input_dir.is_symlink():
                info = input_dir.lstat()
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                    raise ValueError("任务附件目录不能是链接或文件")
            else:
                input_dir.mkdir(mode=0o700)
            if input_dir.resolve(strict=True) != input_dir or task_workspace not in input_dir.resolve(strict=True).parents:
                raise ValueError("任务附件目录路径无效")
        except OSError as exc:
            raise ValueError("任务附件目录不可用") from exc

        filename = f"{ident}_{name}"
        target = input_dir / filename
        relative = f"{task_workspace_rel}/inputs/{filename}"
        try:
            existing = target.lstat()
        except FileNotFoundError:
            existing = None
        except OSError as exc:
            raise ValueError("任务附件副本不可用") from exc
        if existing is not None:
            if stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode):
                raise ValueError("任务附件副本必须是普通文件")
            try:
                self._hash_regular_file(target, size, digest)
            except ValueError:
                raise ValueError("任务附件副本已被修改，拒绝覆盖") from None
            return target, relative

        temporary = input_dir / f".{ident}.{uuid.uuid4().hex}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(temporary, flags, 0o600)
        except OSError as exc:
            raise ValueError("无法创建任务附件临时副本") from exc

        copied_hash = hashlib.sha256()
        copied_size = 0
        try:
            source_fd = os.open(source_path, os.O_RDONLY | getattr(os, "O_BINARY", 0) |
                                getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        except OSError as exc:
            os.close(fd)
            temporary.unlink(missing_ok=True)
            raise ValueError("附件读取失败") from exc
        try:
            with os.fdopen(source_fd, "rb") as source, os.fdopen(fd, "wb") as output:
                source_info = os.fstat(source.fileno())
                target_info = os.fstat(output.fileno())
                if (not stat.S_ISREG(source_info.st_mode) or not stat.S_ISREG(target_info.st_mode)
                        or target_info.st_size != 0):
                    raise ValueError("附件副本文件类型无效")
                while True:
                    chunk = source.read(64 * 1024)
                    if not chunk:
                        break
                    copied_size += len(chunk)
                    if copied_size > size:
                        raise ValueError("附件大小与上传记录不符")
                    copied_hash.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
                source_after = os.fstat(source.fileno())
                source_path_after = source_path.lstat()
                if (copied_size != size or copied_hash.hexdigest() != digest
                        or (source_after.st_dev, source_after.st_ino) != (source_info.st_dev, source_info.st_ino)
                        or (source_path_after.st_dev, source_path_after.st_ino) != (source_info.st_dev, source_info.st_ino)
                        or source_after.st_size != size or source_path_after.st_size != size):
                    raise ValueError("附件内容与上传记录不符")
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        try:
            self._hash_regular_file(temporary, size, digest)
            # Hard-link publication is atomic and cannot replace a file that
            # appeared during a concurrent retry.
            os.link(temporary, target, follow_symlinks=False)
        except FileExistsError:
            try:
                self._hash_regular_file(target, size, digest)
            except ValueError:
                raise ValueError("任务附件副本已被修改，拒绝覆盖") from None
        except (OSError, ValueError) as exc:
            raise ValueError("无法安全发布任务附件副本") from exc
        finally:
            temporary.unlink(missing_ok=True)
        self._hash_regular_file(target, size, digest)
        return target, relative

    def prepare_body(self, body, *, task_workspace: Path | None = None):
        if not isinstance(body, dict): raise ValueError("JSON object required")
        attachments = body.get("attachments", [])
        if not isinstance(attachments, list) or len(attachments) > 6:
            raise ValueError("每次消息最多 6 个附件")
        if any(not isinstance(ident, str) for ident in attachments):
            raise ValueError("Invalid attachment identifier")
        if not attachments: return body
        task_root, task_root_rel = self._task_workspace(task_workspace) if task_workspace is not None else (None, None)
        lines, public_attachments = [], []
        for ident in dict.fromkeys(attachments):
            if not isinstance(ident, str): raise ValueError("Invalid attachment identifier")
            with self.store.lock:
                row = self.store.db.execute("SELECT * FROM uploads WHERE id=?", (ident,)).fetchone()
            if not row: raise ValueError("附件不存在，请重新导入")
            if task_root is not None:
                path, relative_path = self._copy_upload_to_task(row, task_root, task_root_rel)
            else:
                path, relative_path = self.harness.resolve_workspace_path(row["path"])
                if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode) or path.stat().st_size > MAX_UPLOAD:
                    raise ValueError("附件不可用")
            lines.append(f"- {json.dumps(row['name'], ensure_ascii=False)}: {json.dumps(str(path), ensure_ascii=False)} ({row['mime']}, {row['size']} bytes)")
            public_attachments.append({"id": ident, "name": row["name"], "path": relative_path,
                "size": row["size"], "mime": row["mime"], "isImage": row["mime"].startswith("image/"),
                "visionSupported": False})
        result = dict(body)
        field = "text" if "text" in body else "prompt"
        text = result.get("_displayText", result.get(field, ""))
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
