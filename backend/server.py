#!/usr/bin/env python3
"""Local, durable HTTP harness for the Codex app-server JSON-RPC interface."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import secrets
import shutil
import signal
import socket
import sqlite3
import ssl
import stat
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PureWindowsPath
from typing import Any, Callable
from task_services import TaskServices

RPC_TIMEOUT = 30.0
MAX_BODY = 1024 * 1024
MAX_EVENT_TEXT = 4000
DEEPSEEK_BASE_URL = "https://api.deepseek.com/"
DEEPSEEK_MODELS = (("deepseek-flash", "DeepSeek Flash"), ("deepseek-v4-pro", "DeepSeek V4 Pro"))
API_KEY_ENV = "PHONE_HARNESS_MODEL_KEY"
MAX_TEXT_FILE = 1024 * 1024
MAX_DOWNLOAD_FILE = 20 * 1024 * 1024
MAX_DIRECTORY_ITEMS = 200


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def ensure_runtime_control(app: Path, state: Path, workspace: Path, backend_port: int) -> Any | None:
    """Start the installed control bridge if it is not already serving this app."""
    if not sys.platform.startswith("linux"):
        return None
    app, state, workspace = Path(app).resolve(), Path(state).resolve(), Path(workspace).resolve()
    if state != (app / "state").resolve() or workspace != (app / "workspace").resolve():
        return None
    bridge = app / "runtime_control.py"
    if not bridge.is_file():
        return None
    try:
        capability = (state / "capability").read_text(encoding="ascii").strip()
        if len(capability) < 32:
            return None
        request = urllib.request.Request("http://127.0.0.1:18882/api/runtime/status", headers={
            "Host": "127.0.0.1:18882", "Authorization": "Bearer " + capability})
        opener = urllib.request.build_opener(NoRedirectHandler(), urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=0.5) as response:
            status = json.loads(response.read(64 * 1024)) if response.status == 200 else {}
            if (response.headers.get("Server", "").startswith("DAXQORuntime/")
                    and status.get("app", {}).get("name") == "DAXQO"):
                return None
    except Exception:
        pass

    stage = os.environ.get("DAXQO_STAGE") or "/sdcard/Download/daxqo-stage/backend"
    log_path = state / "runtime-control-launcher.log"
    try:
        log = log_path.open("ab", buffering=0)
        try:
            os.chmod(log_path, 0o600)
        except OSError:
            pass
        try:
            return subprocess.Popen([sys.executable, str(bridge), "--app", str(app), "--stage", stage,
                "--port", "18882", "--backend-port", str(backend_port)], cwd=str(app), stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT, close_fds=True, start_new_session=True)
        finally:
            log.close()
    except Exception:
        return None


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def short_text(value: Any, limit: int = MAX_EVENT_TEXT) -> str:
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    import re
    value = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*", "Bearer [redacted]", value)
    value = re.sub(r"(?i)([?&](?:token|access_token|refresh_token|code|state|secret)=)[^&#\s]+", r"\1[redacted]", value)
    return value[:limit]


class RpcError(RuntimeError):
    pass


def _linux_process_identity(pid: int) -> tuple[int, int, int, int] | None:
    """Return (pid, real uid, parent pid, start ticks), protecting against PID reuse."""
    if not sys.platform.startswith("linux"): return None
    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        fields = stat_text[stat_text.rfind(")") + 2:].split()
        parent_pid, start_ticks = int(fields[1]), int(fields[19])
        status = Path(f"/proc/{pid}/status").read_text(encoding="ascii")
        uid_line = next(line for line in status.splitlines() if line.startswith("Uid:"))
        real_uid = int(uid_line.split()[1])
        return pid, real_uid, parent_pid, start_ticks
    except (OSError, ValueError, IndexError, StopIteration):
        return None


def _linux_descendant_identities(root_pid: int) -> list[tuple[int, int, int]]:
    """Snapshot direct and detached descendants before their parents can exit."""
    root = _linux_process_identity(root_pid)
    if not root: return []
    try: pids = [int(entry.name) for entry in Path("/proc").iterdir() if entry.name.isdigit()]
    except OSError: return []
    children: dict[int, list[tuple[int, int, int, int]]] = {}
    for pid in pids:
        identity = _linux_process_identity(pid)
        if identity and identity[1] == root[1] and pid != root_pid:
            children.setdefault(identity[2], []).append(identity)
    result: list[tuple[int, int, int]] = []
    seen = {root_pid}
    stack = [root_pid]
    while stack:
        parent = stack.pop()
        for pid, uid, _ppid, start_ticks in children.get(parent, ()):
            if pid in seen: continue
            seen.add(pid)
            result.append((pid, uid, start_ticks))
            stack.append(pid)
    return result


def _linux_identity_matches(identity: tuple[int, int, int]) -> bool:
    pid, uid, start_ticks = identity
    current = _linux_process_identity(pid)
    return bool(current and current[1] == uid and current[3] == start_ticks)


def _linux_identity_active(identity: tuple[int, int, int]) -> bool:
    if not _linux_identity_matches(identity): return False
    try:
        text = Path(f"/proc/{identity[0]}/stat").read_text(encoding="ascii")
        state = text[text.rfind(")") + 2:].split()[0]
        return state != "Z"
    except (OSError, IndexError):
        return False


def _linux_signal_identity(identity: tuple[int, int, int], sig: int) -> bool:
    """Signal only a verified process identity, using pidfd where available."""
    pid, uid, start_ticks = identity
    pidfd = None
    try:
        if hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal"):
            pidfd = os.pidfd_open(pid, 0)
            current = _linux_process_identity(pid)
            if not current or current[1] != uid or current[3] != start_ticks: return False
            signal.pidfd_send_signal(pidfd, sig)
        else:
            if not _linux_identity_matches(identity): return False
            os.kill(pid, sig)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False
    finally:
        if pidfd is not None:
            try: os.close(pidfd)
            except OSError: pass


class AppServer:
    """One app-server subprocess; responses and notifications are demultiplexed safely."""

    def __init__(self, command: list[str], codex_home: Path, on_event: Callable[[dict], None],
                 on_request: Callable[[dict], None] | None = None, on_disconnect: Callable[[], None] | None = None,
                 extra_env: dict[str, str] | None = None):
        self.command = command
        self.codex_home = codex_home
        self.on_event = on_event
        self.on_request = on_request or (lambda _: None)
        self.on_disconnect = on_disconnect or (lambda: None)
        self.extra_env = dict(extra_env or {})
        self._api_key = self.extra_env.get(API_KEY_ENV, "")
        self.process: subprocess.Popen[str] | None = None
        self._process_group_id: int | None = None
        self._descendants_lock = threading.Lock()
        self._tracked_descendants: dict[tuple[int, int], tuple[int, int, int]] = {}
        self._descendants_stop = threading.Event()
        self._descendants_thread: threading.Thread | None = None
        self._write_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: dict[str, tuple[threading.Event, dict[str, Any]]] = {}
        self._counter = 0
        self._reader: threading.Thread | None = None
        self._stderr: threading.Thread | None = None
        self._stderr_tail: list[str] = []
        self._ready = False
        self.version: str | None = None
        self.account: dict[str, Any] = {"authenticated": False, "type": None}
        self.account_error: str | None = None
        self.models: list[dict[str, Any]] = []
        self.login: dict[str, Any] | None = None
        self._login_event = threading.Event()
        self.approvals: dict[str, dict[str, Any]] = {}
        self._approvals_lock = threading.Lock()

    @property
    def connected(self) -> bool:
        return bool(self.process and self.process.poll() is None and self._ready)

    def start(self) -> None:
        env = os.environ.copy()
        # The app-server is an isolated, direct-to-provider child. Never let a
        # vendor proxy or OPENAI_* setting silently redirect its requests.
        for name in tuple(env):
            upper = name.upper()
            if upper in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"} or upper.startswith("OPENAI_"):
                env.pop(name, None)
        env["CODEX_HOME"] = str(self.codex_home)
        env.pop(API_KEY_ENV, None)
        env.update(self.extra_env)
        self.codex_home.mkdir(parents=True, exist_ok=True)
        try:
            version = subprocess.run([self.command[0], "--version"], env=env, capture_output=True,
                                     text=True, timeout=5, check=False)
            output = (version.stdout or version.stderr).strip().splitlines()
            self.version = short_text(output[0], 100) if output else None
        except (OSError, subprocess.TimeoutExpired):
            self.version = None
        self.process = subprocess.Popen(
            self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", bufsize=1, env=env,
            **({"start_new_session": True} if os.name == "posix" else {}),
        )
        if os.name == "posix": self._process_group_id = self.process.pid
        if sys.platform.startswith("linux"):
            self._descendants_thread = threading.Thread(target=self._track_descendants, name="app-server-descendants", daemon=True)
            self._descendants_thread.start()
        self._reader = threading.Thread(target=self._read_stdout, name="app-server-rpc", daemon=True)
        self._reader.start()
        self._stderr = threading.Thread(target=self._read_stderr, name="app-server-stderr", daemon=True)
        self._stderr.start()
        self.request("initialize", {"clientInfo": {"name": "phone-harness", "version": "1.0.0"}}, timeout=RPC_TIMEOUT)
        self.notify("initialized", {})
        self._ready = True
        self.refresh_account()

    def _read_stdout(self) -> None:
        assert self.process and self.process.stdout
        for line in self.process.stdout:
            try:
                msg = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(msg, dict):
                continue
            if "id" in msg and isinstance(msg.get("method"), str):
                self._handle_server_request(msg)
            elif "id" in msg:
                with self._pending_lock:
                    slot = self._pending.get(str(msg["id"]))
                if slot:
                    slot[1]["message"] = msg
                    slot[0].set()
            elif isinstance(msg.get("method"), str):
                self._handle_event(msg)
        self._ready = False
        with self._pending_lock:
            pending = list(self._pending.values())
        for event, result in pending:
            result["message"] = {"error": {"message": "Codex app-server disconnected"}}
            event.set()
        self.on_disconnect()

    def _handle_server_request(self, msg: dict[str, Any]) -> None:
        method = msg.get("method", "")
        if "requestapproval" not in method.lower():
            self.respond(msg.get("id"), error={"code": -32601, "message": "Unsupported app-server request"})
            return
        request_id = msg.get("id")
        key = str(request_id)
        params = msg.get("params") or {}
        try:
            self.on_request({"id": request_id, "key": key, "method": method, "params": params})
        except Exception:
            self.respond(request_id, error={"code": -32603, "message": "Unable to queue approval request"})

    def respond(self, request_id: Any, result: dict[str, Any] | None = None,
                error: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
        if error is not None: message["error"] = error
        else: message["result"] = result or {}
        self._write(message)

    def _read_stderr(self) -> None:
        assert self.process and self.process.stderr
        for line in self.process.stderr:
            # Keep only a short diagnostic tail; never return it through HTTP.
            self._stderr_tail.append(line.strip()[:500])
            del self._stderr_tail[:-20]

    def _track_descendants(self) -> None:
        while not self._descendants_stop.is_set():
            process = self.process
            if process:
                for pid, uid, start_ticks in _linux_descendant_identities(process.pid):
                    with self._descendants_lock:
                        self._tracked_descendants[(pid, start_ticks)] = (pid, uid, start_ticks)
            self._descendants_stop.wait(0.25)

    def _write(self, msg: dict[str, Any]) -> None:
        if not self.process or not self.process.stdin or self.process.poll() is not None:
            raise RpcError("Codex app-server is not running")
        wire = json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._write_lock:
            self.process.stdin.write(wire)
            self.process.stdin.flush()

    def request(self, method: str, params: dict[str, Any] | None = None, timeout: float = RPC_TIMEOUT) -> Any:
        with self._pending_lock:
            self._counter += 1
            request_id = str(self._counter)
            gate, holder = threading.Event(), {}
            self._pending[request_id] = (gate, holder)
        try:
            self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
            if not gate.wait(timeout):
                raise RpcError(f"Codex request timed out: {method}")
            msg = holder.get("message", {})
            if "error" in msg:
                error = msg["error"]
                message = short_text(error.get("message", error), 500) if isinstance(error, dict) else short_text(error, 500)
                if self._api_key: message = message.replace(self._api_key, "[redacted]")
                failure = RpcError(message)
                failure.rpc_rejected = True
                raise failure
            return msg.get("result", {})
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def _handle_event(self, msg: dict[str, Any]) -> None:
        method, params = msg.get("method", ""), msg.get("params") or {}
        if method == "account/login/completed":
            self.login = {k: params.get(k) for k in ("loginId", "success", "error") if k in params}
            self._login_event.set()
            threading.Thread(target=self.refresh_account, name="account-refresh", daemon=True).start()
        self.on_event(msg)

    def refresh_account(self) -> dict[str, Any]:
        try:
            value = self.request("account/read", {"refreshToken": False})
            value = value if isinstance(value, dict) else {}
            nested_account = "account" in value
            account = value.get("account") if nested_account else None
            account = account if isinstance(account, dict) else None
            # The nested account object is the current protocol shape. Never copy email or
            # other account fields into the HTTP status response.
            if account is not None:
                self.account = {"authenticated": True, "type": account.get("type", account.get("accountType"))}
            else:
                self.account = {
                    "authenticated": bool(value.get("authenticated", value.get("isAuthenticated", False))),
                    "type": value.get("type", value.get("accountType")),
                }
            self.account_error = None
        except Exception as exc:
            self.account_error = short_text(str(exc).replace(self._api_key, "[redacted]") if self._api_key else str(exc), 300)
        return self.account

    def refresh_models(self) -> list[dict[str, Any]]:
        try:
            result = self.request("model/list", {})
            rows = result.get("data", result.get("models", [])) if isinstance(result, dict) else []
            self.models = [{"id": m.get("id"), "displayName": m.get("displayName", m.get("name", m.get("id"))),
                           **({"isDefault": True} if m.get("isDefault") else {})}
                          for m in rows if isinstance(m, dict) and m.get("id")]
        except Exception:
            self.models = []
        return self.models

    def begin_login(self) -> dict[str, Any]:
        self._login_event.clear()
        self.login = None
        result = self.request("account/login/start", {"type": "chatgpt"})
        self.login = {k: result.get(k) for k in ("loginId", "authUrl") if result.get(k)}
        return {**self.login, **({"message": "在浏览器完成 ChatGPT 登录后，此页面会更新状态。"} if self.login.get("authUrl") else {})}

    def close(self) -> None:
        process = self.process
        pgid = self._process_group_id
        self._process_group_id = None
        if os.name == "posix" and pgid and pgid > 1 and pgid not in (os.getpid(), os.getpgrp()):
            # start_new_session makes this group exclusively owned by this app-server.
            # Signal it even if its leader already exited; descendants may still run.
            descendants = _linux_descendant_identities(process.pid) if process and sys.platform.startswith("linux") else []
            self._descendants_stop.set()
            if self._descendants_thread and self._descendants_thread is not threading.current_thread():
                self._descendants_thread.join(timeout=0.25)
            with self._descendants_lock:
                seen = {(pid, start) for pid, _uid, start in descendants}
                descendants.extend(identity for key, identity in self._tracked_descendants.items() if key not in seen)
            for identity in reversed(descendants): _linux_signal_identity(identity, signal.SIGTERM)
            try: os.killpg(pgid, signal.SIGTERM)
            except ProcessLookupError: pass
            except OSError: pass
            if process and process.stdin:
                try: process.stdin.close()
                except OSError: pass
            if process:
                try: process.wait(timeout=3)
                except subprocess.TimeoutExpired: pass
            # Give graceful shutdown a short window, then kill any surviving members.
            deadline = time.monotonic() + 0.5
            while time.monotonic() < deadline:
                try:
                    os.killpg(pgid, 0)
                    group_alive = True
                except ProcessLookupError: group_alive = False
                except OSError: group_alive = False
                if not group_alive and not any(_linux_identity_active(identity) for identity in descendants): break
                time.sleep(0.025)
            # Some Codex tools detach into new sessions. Their PID, UID and
            # kernel start time were captured while still below this child.
            for identity in reversed(descendants): _linux_signal_identity(identity, signal.SIGKILL)
            try: os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError: pass
            except OSError: pass
            if process and process.poll() is None:
                try: process.wait(timeout=2)
                except subprocess.TimeoutExpired: pass
        elif process and process.poll() is None:
            self._descendants_stop.set()
            if self._descendants_thread and self._descendants_thread is not threading.current_thread():
                self._descendants_thread.join(timeout=0.25)
            if process.stdin:
                try: process.stdin.close()
                except OSError: pass
            process.terminate()
            try: process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        else:
            self._descendants_stop.set()
        for worker in (self._reader, self._stderr):
            if worker and worker is not threading.current_thread(): worker.join(timeout=1)
        if process:
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream:
                    try: stream.close()
                    except OSError: pass


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.db:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, title TEXT NOT NULL, prompt TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, thread_id TEXT, model TEXT, result TEXT, error TEXT, stop_requested INTEGER NOT NULL DEFAULT 0)")
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(tasks)")}
            if "turn_id" not in columns: self.db.execute("ALTER TABLE tasks ADD COLUMN turn_id TEXT")
            self.db.execute("CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, type TEXT NOT NULL, text TEXT NOT NULL, created_at TEXT NOT NULL, FOREIGN KEY(task_id) REFERENCES tasks(id))")
            self.db.execute("CREATE TABLE IF NOT EXISTS progress (seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL, FOREIGN KEY(task_id) REFERENCES tasks(id))")
            self.db.execute("CREATE TABLE IF NOT EXISTS messages (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, client_message_id TEXT, role TEXT NOT NULL, text TEXT NOT NULL, created_at TEXT NOT NULL, status TEXT NOT NULL, turn_id TEXT, delivery TEXT, error TEXT, FOREIGN KEY(task_id) REFERENCES tasks(id), UNIQUE(task_id,client_message_id))")
            message_columns = {row[1] for row in self.db.execute("PRAGMA table_info(messages)")}
            if "retry_safe" not in message_columns: self.db.execute("ALTER TABLE messages ADD COLUMN retry_safe INTEGER NOT NULL DEFAULT 0")
            if "display_text" not in message_columns: self.db.execute("ALTER TABLE messages ADD COLUMN display_text TEXT")
            if "attachments_json" not in message_columns: self.db.execute("ALTER TABLE messages ADD COLUMN attachments_json TEXT")
            # Preserve v0.1 tasks as visible conversation history.
            for task in self.db.execute("SELECT id,prompt,result,created_at,status FROM tasks"):
                exists = self.db.execute("SELECT 1 FROM messages WHERE task_id=? LIMIT 1", (task["id"],)).fetchone()
                if not exists:
                    self.db.execute("INSERT INTO messages(id,task_id,role,text,created_at,status) VALUES(?,?,?,?,?,?)",
                                    (str(uuid.uuid4()), task["id"], "user", task["prompt"], task["created_at"], "started"))
                    if task["result"]:
                        self.db.execute("INSERT INTO messages(id,task_id,role,text,created_at,status) VALUES(?,?,?,?,?,?)",
                                        (str(uuid.uuid4()), task["id"], "assistant", task["result"], task["created_at"], "completed"))
            self.db.execute("UPDATE tasks SET status='interrupted',updated_at=?,error='Backend restarted while this task was active' WHERE status IN ('queued','running')", (now(),))
            self.db.execute("UPDATE messages SET status='failed',delivery='failed',error='Backend restarted before delivery was confirmed',retry_safe=0 WHERE status='sending'")

    def add_message(self, task_id: str, role: str, text: str, status: str, *, client_id: str | None = None,
                    turn_id: str | None = None, delivery: str | None = None, error: str | None = None,
                    display_text: str | None = None, attachments: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        ident, stamp = str(uuid.uuid4()), now()
        with self.lock, self.db:
            self.db.execute("INSERT INTO messages(id,task_id,client_message_id,role,text,created_at,status,turn_id,delivery,error,display_text,attachments_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                            (ident, task_id, client_id, role, text, stamp, status, turn_id, delivery, error, display_text,
                             json.dumps(attachments, ensure_ascii=False) if attachments else None))
        return {"id": ident, "role": role, "text": display_text if display_text is not None else text, "createdAt": stamp, "status": status,
                **({"turnId": turn_id} if turn_id else {}), **({"clientMessageId": client_id} if client_id else {}),
                **({"attachments": attachments} if attachments else {}), **({"retrySafe": False} if role == "user" else {})}

    def update_message(self, ident: str, **values: Any) -> None:
        if not values: return
        with self.lock, self.db:
            self.db.execute("UPDATE messages SET " + ",".join(f"{k}=?" for k in values) + " WHERE id=?", (*values.values(), ident))

    def message_by_client(self, task_id: str, client_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM messages WHERE task_id=? AND client_message_id=?", (task_id, client_id)).fetchone()
        return dict(row) if row else None

    def messages(self, task_id: str) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.db.execute("SELECT id,task_id,client_message_id,role,text,display_text,attachments_json,created_at,status,turn_id,retry_safe FROM messages WHERE task_id=? ORDER BY rowid", (task_id,)).fetchall()
        return [self.message_public(dict(r)) for r in rows]

    @staticmethod
    def message_public(row: dict[str, Any]) -> dict[str, Any]:
        turn = row.get("turn_id", row.get("turnId"))
        text = row.get("display_text") if row.get("display_text") is not None else row.get("text", "")
        try: attachments = json.loads(row.get("attachments_json") or "[]")
        except (ValueError, TypeError): attachments = []
        return {"id": row["id"], "role": row["role"], "text": text, "createdAt": row.get("created_at", row.get("createdAt")),
                "status": row["status"], **({"turnId": turn} if turn else {}),
                **({"clientMessageId": row["client_message_id"]} if row.get("client_message_id") else {}),
                **({"attachments": attachments} if attachments else {}),
                **({"retrySafe": bool(row.get("retry_safe"))} if row.get("role") == "user" else {})}

    def event(self, task_id: str, kind: str, text: Any) -> None:
        with self.lock, self.db:
            self.db.execute("INSERT INTO events(task_id,type,text,created_at) VALUES(?,?,?,?)", (task_id, kind, short_text(text), now()))

    def progress(self, task_id: str, kind: str, payload: dict[str, Any]) -> None:
        stamp = now()
        with self.lock, self.db:
            if kind == "liveMessage":
                self.db.execute("DELETE FROM progress WHERE task_id=? AND kind='liveMessage'", (task_id,))
            self.db.execute("INSERT INTO progress(task_id,kind,payload,created_at) VALUES(?,?,?,?)",
                            (task_id, kind, json.dumps(payload, ensure_ascii=False, separators=(",", ":")), stamp))

    def progress_rows(self, task_id: str) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.db.execute("SELECT kind,payload,created_at FROM progress WHERE task_id=? ORDER BY seq", (task_id,)).fetchall()
        result = []
        for row in rows:
            try: payload = json.loads(row["payload"])
            except (ValueError, TypeError): continue
            result.append({"kind": row["kind"], "createdAt": row["created_at"], **payload})
        return result

    def active_rows(self) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM tasks WHERE status IN ('queued','running') ORDER BY created_at").fetchall()
        return [dict(row) for row in rows]

    def update(self, task_id: str, **values: Any) -> None:
        if not values: return
        values["updated_at"] = now()
        with self.lock, self.db:
            self.db.execute("UPDATE tasks SET " + ",".join(f"{k}=?" for k in values) + " WHERE id=?", (*values.values(), task_id))

    def row(self, task_id: str) -> dict[str, Any] | None:
        with self.lock:
            r = self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return dict(r) if r else None

    def rows(self) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(r) for r in self.db.execute("SELECT * FROM tasks ORDER BY created_at DESC")]

    @staticmethod
    def public(row: dict[str, Any], detail: bool = False) -> dict[str, Any]:
        item = {"id": row["id"], "title": row["title"], "status": row["status"], "createdAt": row["created_at"], "updatedAt": row["updated_at"]}
        for source, target in (("thread_id", "threadId"), ("model", "model"), ("error", "error")):
            if row.get(source): item[target] = row[source]
        if detail:
            item["prompt"] = row["prompt"]
            if row.get("result") is not None: item["result"] = row["result"]
        return item

    def events(self, task_id: str, after: int) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.db.execute("SELECT seq,type,text,created_at FROM events WHERE task_id=? AND seq>? ORDER BY seq LIMIT 500", (task_id, after)).fetchall()
        return [{"seq": r["seq"], "type": r["type"], "text": r["text"], "createdAt": r["created_at"]} for r in rows]


class Harness:
    def __init__(self, codex: str, state: Path, workspace: Path, host: str, port: int,
                 test_engine: AppServer | None = None, sandbox: str = "workspace-write"):
        if sandbox not in ("read-only", "workspace-write", "danger-full-access"):
            raise ValueError("Invalid sandbox mode")
        self.state, self.workspace = state, workspace
        self.host, self.port = host, port
        self.sandbox = sandbox
        self.started_at = now()
        self.capability_file = state / "capability"
        self.provider_dir = state / "private"
        self.provider_file = self.provider_dir / "provider.json"
        self.codex_home = state / "codex"
        state.mkdir(parents=True, exist_ok=True)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.capability = self._load_capability()
        self.store = Store(state / "harness.sqlite3")
        self.provider = self._load_provider()
        self.provider_saved = self.provider_file.is_file()
        self.lock = threading.RLock()
        self.active_task_id: str | None = None
        self._task_events: dict[str, list[dict[str, Any]]] = {}
        self._task_buffers: dict[str, str] = {}
        self._agent_buffers: dict[tuple[str, str], str] = {}
        self._live_updates: dict[str, tuple[float, int]] = {}
        self._task_event_lock = threading.Lock()
        self._agent_threads: dict[str, str] = {}
        self._approval_lock = threading.Lock()
        self.pending_approvals: dict[str, dict[str, Any]] = {}
        self._loaded_threads: set[str] = set()
        self.services = TaskServices(self)
        self.engine_path = shutil.which(codex) if not Path(codex).is_absolute() else (codex if Path(codex).is_file() else None)
        self.engine = test_engine
        if self.engine:
            self.engine.on_event = self._engine_event
            self.engine.on_request = self._engine_request
            self.engine.on_disconnect = self._engine_disconnected
        self.engine_error = None
        self.reconnecting = False
        if not self.engine and self.engine_path:
            try:
                self.engine = self._new_engine()
                self.engine.start()
            except Exception as exc:
                self.engine_error = self.safe_text(str(exc), 300)
                if self.engine: self.engine.close()
                self.engine = None
        elif not self.engine_path and not test_engine:
            self.engine_error = "Codex app-server executable was not found"
        self.usage: dict[str, dict[str, Any]] = {}

    def _load_provider(self) -> dict[str, Any]:
        value = {"provider": "deepseek", "baseUrl": DEEPSEEK_BASE_URL, "model": "deepseek-flash",
                 "apiKey": "", "verified": False, "error": None}
        try:
            saved = json.loads(self.provider_file.read_text(encoding="utf-8"))
            if saved.get("provider") in ("deepseek", "custom"):
                value.update({k: saved.get(k, value[k]) for k in value})
                value["apiKey"] = saved.get("apiKey", "") if isinstance(saved.get("apiKey", ""), str) else ""
                if value["provider"] == "deepseek": value["baseUrl"] = DEEPSEEK_BASE_URL
        except FileNotFoundError:
            pass
        except Exception:
            value["error"] = "Saved provider settings could not be read"
        return value

    def provider_configured(self) -> bool:
        return bool(self.provider.get("provider") in ("deepseek", "custom")
                    and self.provider.get("baseUrl") and self.provider.get("model")
                    and isinstance(self.provider.get("apiKey"), str) and self.provider.get("apiKey"))

    def public_provider(self) -> dict[str, Any]:
        result = {"provider": self.provider["provider"], "baseUrl": self.provider["baseUrl"],
                 "model": self.provider["model"], "configured": self.provider_configured(),
                 "verified": bool(self.provider.get("verified"))}
        if self.provider.get("error"): result["error"] = self.safe_text(self.provider["error"], 300)
        return result

    def safe_text(self, value: Any, limit: int = MAX_EVENT_TEXT) -> str:
        text = short_text(value, limit)
        key = self.provider.get("apiKey") if getattr(self, "provider", None) else None
        if key: text = text.replace(key, "[redacted]")
        import re
        field_pattern = r"(?i)([\"']?\b(?:api[_-]?key|model[_-]?key|appsecret|secret|capability|authorization|access[_-]?token|refresh[_-]?token|key)[\"']?\s*[=:]\s*[\"']?)[^\s,;\"'}]+"
        text = re.sub(field_pattern, r"\1[redacted]", text)
        text = re.sub(r"(?i)([?&](?:api[_-]?key|model[_-]?key|appsecret|secret|capability|authorization|access[_-]?token|refresh[_-]?token|key)=)[^&#\s]+", r"\1[redacted]", text)
        text = re.sub(r"\b(?:sk|ds|rk)-[A-Za-z0-9_-]{12,}\b", "[redacted]", text)
        return text

    def max_concurrent_tasks(self) -> int:
        settings = getattr(self.services, "settings", {}) or {}
        try: value = int(settings.get("maxConcurrentTasks", 2))
        except (TypeError, ValueError): value = 2
        return max(1, min(4, value))

    def _sync_active_task_id(self) -> None:
        rows = self.store.active_rows()
        self.active_task_id = rows[0]["id"] if rows else None

    def _has_active_tasks(self) -> bool:
        return bool(self.store.active_rows())

    def task_detail(self, task_id: str) -> dict[str, Any]:
        events = self.store.progress_rows(task_id)
        row = self.store.row(task_id) or {}
        last = events[-1] if events else None
        created = last.get("createdAt") if last else row.get("updated_at")
        try: last_dt = dt.datetime.fromisoformat(created.replace("Z", "+00:00")) if created else dt.datetime.now(dt.timezone.utc)
        except (ValueError, AttributeError): last_dt = dt.datetime.now(dt.timezone.utc)
        started = next((e.get("createdAt") for e in reversed(events)
                        if e.get("kind") == "phase" and e.get("phase") == "running"), row.get("created_at"))
        finished = row.get("updated_at") if row.get("status") not in ("queued", "running") else None
        try:
            end = dt.datetime.fromisoformat(finished.replace("Z", "+00:00")) if finished else dt.datetime.now(dt.timezone.utc)
            elapsed = max(0, int((end - dt.datetime.fromisoformat(started.replace("Z", "+00:00"))).total_seconds())) if started else 0
        except (ValueError, AttributeError): elapsed = 0
        quiet = max(0, int((dt.datetime.now(dt.timezone.utc) - last_dt.astimezone(dt.timezone.utc)).total_seconds()))
        usage = self.usage.get(task_id, {})
        if not usage:
            usage_event = next((e for e in reversed(events) if e.get("kind") == "usage"), None)
            if usage_event: usage = {"context": usage_event.get("context", {})}
        context = dict(usage.get("context", {}))
        last_usage = context.get("last", {})
        total_usage = context.get("total", {})
        compact_items = [e for e in events if e.get("kind") == "compact" and not e.get("legacy")]
        compact_completed = sum(1 for e in compact_items if e.get("status") == "completed")
        legacy_compactions = sum(1 for e in events if e.get("kind") == "compact" and e.get("legacy") and e.get("status") == "completed")
        context.update({"contextWindow": context.get("windowTokens"),
                        "inputTokens": last_usage.get("inputTokens", total_usage.get("inputTokens")),
                        "outputTokens": last_usage.get("outputTokens", total_usage.get("outputTokens")),
                        "totalTokens": total_usage.get("totalTokens", last_usage.get("totalTokens")),
                        "compactThreshold": (getattr(self.services, "settings", {}) or {}).get("autoCompactTokens"),
                        "compactions": max(compact_completed, legacy_compactions),
                        "compactionAttempts": sum(1 for e in compact_items if e.get("status") == "started")})
        latest_live = next((e.get("text") for e in reversed(events) if e.get("kind") == "liveMessage"), "")
        agent_rows: dict[str, dict[str, Any]] = {}
        for event in events:
            if event.get("kind") == "agent":
                child_status = event.get("status", "running")
                for child_id in event.get("agentThreadIds", []):
                    if isinstance(child_id, str):
                        state = event.get("agentsStates", {}).get(child_id, child_status)
                        details = state if isinstance(state, dict) else {}
                        state = details.get("status", state)
                        state = {"inProgress": "running", "errored": "failed", "shutdown": "interrupted"}.get(state, state)
                        name = details.get("name") or child_id
                        summary = details.get("summary") or event.get("summary")
                        agent_rows[child_id] = {"id": child_id, "agentId": child_id, "name": self.safe_text(name, 100),
                                                "status": self.safe_text(state, 40),
                                                **({"summary": self.safe_text(summary, 240)} if isinstance(summary, str) and summary else {})}
                child_id = event.get("agentThreadId")
                if isinstance(child_id, str):
                    child_status = {"inProgress": "running", "errored": "failed", "shutdown": "interrupted"}.get(child_status, child_status)
                    agent_rows[child_id] = {"id": child_id, "agentId": child_id,
                        "name": self.safe_text(event.get("agentPath") or child_id, 100), "status": self.safe_text(child_status, 40),
                        **({"summary": self.safe_text(event["summary"], 240)} if event.get("summary") else {})}
                for child_id, state in event.get("agentsStates", {}).items():
                    details = state if isinstance(state, dict) else {}
                    status = details.get("status", state)
                    status = {"inProgress": "running", "errored": "failed", "shutdown": "interrupted"}.get(status, status)
                    name = details.get("name") or child_id
                    summary = details.get("summary")
                    current = agent_rows.setdefault(child_id, {"id": child_id, "agentId": child_id, "name": self.safe_text(name, 100)})
                    current["status"] = self.safe_text(status, 40)
                    if isinstance(summary, str) and summary:
                        current["summary"] = self.safe_text(summary, 240)
            elif event.get("kind") == "agentState" and isinstance(event.get("id"), str):
                agent_id = event["id"]
                agent_rows.setdefault(agent_id, {"id": agent_id, "agentId": agent_id, "name": agent_id})["status"] = event.get("status", "running")
            elif event.get("kind") == "agentMessage" and isinstance(event.get("agentThreadId"), str):
                agent_id = event["agentThreadId"]
                agent_rows.setdefault(agent_id, {"id": agent_id, "agentId": agent_id, "name": agent_id})["summary"] = event.get("text", "")[:240]
        for agent_id, value in context.get("agents", {}).items():
            agent_rows.setdefault(agent_id, {"id": agent_id, "agentId": agent_id, "name": agent_id, "status": "unknown"})["context"] = value
        phases = [e for e in events if e.get("kind") in ("phase", "tool", "webSearch", "compact", "agent")]
        phase = phases[-1].get("phase") or phases[-1].get("kind") if phases else row.get("status", "queued")
        summary = phases[-1].get("summary", "") if phases else ("任务已排队" if row.get("status") == "queued" else "")
        return {"activity": {"phase": phase, "lastEventAt": created, "elapsedSeconds": elapsed,
                             "quietSeconds": quiet, "summary": self.safe_text(summary, 300)},
                "context": context, "agents": list(agent_rows.values()),
                "progress": events,
                **({"liveMessage": self.safe_text(latest_live, 2000)} if latest_live else {})}

    def _save_provider_file(self) -> None:
        self.provider_dir.mkdir(parents=True, exist_ok=True)
        try: os.chmod(self.provider_dir, 0o700)
        except OSError: pass
        temp = self.provider_file.with_suffix(".json.tmp")
        temp.write_text(json.dumps(self.provider, ensure_ascii=False, indent=2), encoding="utf-8")
        try: os.chmod(temp, 0o600)
        except OSError: pass
        os.replace(temp, self.provider_file)
        try: os.chmod(self.provider_file, 0o600)
        except OSError: pass

    def _write_codex_config(self) -> None:
        self.codex_home.mkdir(parents=True, exist_ok=True)
        provider_id = self.provider["provider"]
        provider_name = "DeepSeek" if provider_id == "deepseek" else "Custom Responses"
        catalog = Path(__file__).resolve().parent / "models-deepseek.json"
        catalog_setting = f"model_catalog_json = {json.dumps(str(catalog))}\n" if provider_id == "deepseek" else ""
        settings = getattr(getattr(self, "services", None), "settings", {}) or {}
        try: agent_threads = max(1, min(4, int(settings.get("maxSubagents", 2))))
        except (TypeError, ValueError): agent_threads = 2
        compact = settings.get("autoCompactTokens", 32000)
        context_window = settings.get("contextWindow", 65536)
        content = (f"model_provider = {json.dumps(provider_id)}\n"
                   f"model = {json.dumps(self.provider['model'])}\n"
                   f"model_auto_compact_token_limit = {int(compact)}\n"
                   f"model_context_window = {int(context_window)}\n"
                   "tool_output_token_limit = 2000\n"
                   f"developer_instructions = \"For long tasks, give the user a brief public progress update after major steps and when waiting or blocked. Delegate independent research or checks to at most {agent_threads} subagents when useful; keep their results concise and do not recursively delegate. Handle simple tasks directly. Never reveal hidden reasoning or credentials.\"\n"
                   "preferred_auth_method = \"apikey\"\n"
                   "forced_login_method = \"api\"\n"
                   f"{catalog_setting}"
                   "web_search = \"disabled\"\n\n"
                   "[features]\n"
                   f"multi_agent = {str(bool(settings.get('subagentsEnabled', True))).lower()}\n"
                   "multi_agent_v2 = false\n\n"
                   "[agents]\n"
                   f"enabled = {str(bool(settings.get('subagentsEnabled', True))).lower()}\n"
                   f"max_concurrent_threads_per_session = {agent_threads}\n\n"
                   f"max_threads = {agent_threads}\n\n"
                   "max_depth = 1\n\n"
                   "[analytics]\n"
                   "enabled = false\n\n"
                   f"[model_providers.{provider_id}]\n"
                   f"name = {json.dumps(provider_name)}\n"
                   f"base_url = {json.dumps(self.provider['baseUrl'])}\n"
                   "wire_api = \"responses\"\n"
                   f"env_key = {json.dumps(API_KEY_ENV)}\n"
                   "requires_openai_auth = false\n"
                   "supports_websockets = false\n")
        config = self.codex_home / "config.toml"
        config.write_text(content, encoding="utf-8")
        try: os.chmod(config, 0o600)
        except OSError: pass

    def _new_engine(self) -> AppServer:
        self._write_codex_config()
        extra_env = {API_KEY_ENV: self.provider["apiKey"]} if self.provider_configured() else {}
        return AppServer([self.engine_path, "app-server"], self.codex_home, self._engine_event,
                         self._engine_request, self._engine_disconnected, extra_env=extra_env)

    def models(self) -> list[dict[str, Any]]:
        if self.provider["provider"] == "custom":
            choices = [{"id": self.provider["model"], "displayName": f"Custom · {self.provider['model']}"}]
        else:
            choices = [{"id": model, "displayName": name} for model, name in DEEPSEEK_MODELS]
        return [{**item, "isDefault": item["id"] == self.provider["model"]} for item in choices]

    def resolve_workspace_path(self, relative: str) -> tuple[Path, str]:
        if not isinstance(relative, str) or "\x00" in relative or len(relative) > 4096:
            raise ValueError("Invalid workspace path")
        normalized = relative.replace("\\", "/")
        windows_path = PureWindowsPath(relative)
        parts = [part for part in normalized.split("/") if part not in ("", ".")]
        if normalized.startswith("/") or windows_path.is_absolute() or windows_path.drive or ".." in parts:
            raise ValueError("Path must stay inside the workspace")
        root = self.workspace.resolve(strict=True)
        candidate = root.joinpath(*parts)
        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise ValueError("Workspace path cannot be accessed") from exc
        if resolved != root and root not in resolved.parents:
            raise ValueError("Path must stay inside the workspace")
        try:
            private = self.provider_dir.resolve(strict=False)
        except OSError:
            private = self.provider_dir.absolute()
        if resolved == private or private in resolved.parents:
            raise FileNotFoundError("Not found")
        display_path = "/".join(parts)
        return resolved, display_path

    def list_workspace_files(self, relative: str = "") -> dict[str, Any]:
        directory, display_path = self.resolve_workspace_path(relative)
        if not stat.S_ISDIR(directory.stat().st_mode): raise ValueError("Path must be a directory")
        root = self.workspace.resolve(strict=True)
        items: list[dict[str, Any]] = []
        for entry in directory.iterdir():
            try:
                resolved = entry.resolve(strict=True)
                if resolved != root and root not in resolved.parents: continue
                private = self.provider_dir.resolve(strict=False)
                if resolved == private or private in resolved.parents: continue
                mode = resolved.stat().st_mode
                is_directory = stat.S_ISDIR(mode)
                if not is_directory and not stat.S_ISREG(mode): continue
                rel = resolved.relative_to(root).as_posix() if entry.is_symlink() else entry.relative_to(root).as_posix()
                items.append({"name": entry.name, "path": rel, "size": 0 if is_directory else resolved.stat().st_size,
                              "isDirectory": is_directory})
            except (OSError, ValueError):
                continue
        items.sort(key=lambda item: (not item["isDirectory"], item["name"].casefold()))
        return {"files": items[:MAX_DIRECTORY_ITEMS], "path": display_path}

    def read_workspace_text(self, relative: str) -> dict[str, Any]:
        path, display_path = self.resolve_workspace_path(relative)
        info = path.stat()
        if not stat.S_ISREG(info.st_mode): raise ValueError("Path must be a regular file")
        if info.st_size > MAX_TEXT_FILE: raise OverflowError("Text file exceeds 1 MiB")
        raw = path.read_bytes()
        if len(raw) > MAX_TEXT_FILE: raise OverflowError("Text file exceeds 1 MiB")
        try: text = raw.decode("utf-8")
        except UnicodeDecodeError as exc: raise ValueError("File is not UTF-8 text") from exc
        return {"path": display_path, "text": text, "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}

    def read_workspace_download(self, relative: str) -> tuple[str, bytes]:
        path, display_path = self.resolve_workspace_path(relative)
        info = path.stat()
        if not stat.S_ISREG(info.st_mode): raise ValueError("Path must be a regular file")
        if info.st_size > MAX_DOWNLOAD_FILE: raise OverflowError("File exceeds 20 MiB")
        raw = path.read_bytes()
        if len(raw) > MAX_DOWNLOAD_FILE: raise OverflowError("File exceeds 20 MiB")
        return display_path, raw

    def save_provider(self, body: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            if self._has_active_tasks():
                raise ValueError("Cannot change provider while a task is active")
            if self.reconnecting: raise ValueError("Codex app-server reconnect is already in progress")
        provider_id = body.get("provider")
        if provider_id not in ("deepseek", "custom"):
            raise ValueError("Provider must be deepseek or custom")
        if provider_id == "deepseek":
            base_url = DEEPSEEK_BASE_URL
            model = body.get("model") or "deepseek-flash"
            if not isinstance(model, str) or model not in {name for name, _ in DEEPSEEK_MODELS}:
                raise ValueError("DeepSeek model must be deepseek-flash or deepseek-v4-pro")
        else:
            base_url = body.get("baseUrl")
            if not isinstance(base_url, str): raise ValueError("Custom provider requires an HTTPS baseUrl")
            parsed = urllib.parse.urlsplit(base_url)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("Custom provider baseUrl must be an HTTPS Responses-compatible URL")
            base_url = base_url.rstrip("/") + ("/" if parsed.path == "" else "")
            model = body.get("model")
            if not isinstance(model, str) or not model.strip(): raise ValueError("Custom provider requires a model")
            model = model.strip()
        supplied_key = body.get("apiKey", "")
        if not isinstance(supplied_key, str): raise ValueError("apiKey must be a string")
        same_key_scope = (provider_id == self.provider.get("provider")
                          and base_url == self.provider.get("baseUrl"))
        api_key = supplied_key.strip() if supplied_key.strip() else (self.provider.get("apiKey", "") if same_key_scope else "")
        if len(api_key) > 8192: raise ValueError("API key is too long")
        if api_key and api_key in base_url: raise ValueError("Enter the API key in the API key field, not in baseUrl")
        with self.lock:
            if self._has_active_tasks():
                raise ValueError("Cannot change provider while a task is active")
            if self.reconnecting: raise ValueError("Codex app-server reconnect is already in progress")
            self.reconnecting = True
        try:
            old_identity = (self.provider.get("provider"), self.provider.get("baseUrl"), self.provider.get("model"), self.provider.get("apiKey"))
            new_identity = (provider_id, base_url, model, api_key)
            self.provider = {"provider": provider_id, "baseUrl": base_url, "model": model, "apiKey": api_key,
                             "verified": bool(self.provider.get("verified")) if old_identity == new_identity else False,
                             "error": self.provider.get("error") if old_identity == new_identity else None}
            self.provider_saved = True
            self._save_provider_file()
            self._write_codex_config()
            status = self.reconnect(_provider_update=True)
            return {"provider": self.public_provider(), "status": status}
        finally:
            self.reconnecting = False

    def test_provider(self) -> dict[str, Any]:
        with self.lock:
            if self._has_active_tasks():
                raise ValueError("Cannot test provider while a task is active")
        if not self.provider_configured(): raise ValueError("Save an API key and provider before testing")
        base = self.provider["baseUrl"].rstrip("/")
        endpoint = base if base.endswith("/responses") else base + "/responses"
        payload = json.dumps({"model": self.provider["model"], "input": "Reply with OK.",
                              "reasoning": {"effort": "low"}, "max_output_tokens": 32}, separators=(",", ":")).encode()
        request = urllib.request.Request(endpoint, data=payload, method="POST", headers={
            "Authorization": "Bearer " + self.provider["apiKey"],
            "Content-Type": "application/json", "Accept": "application/json"})
        error = None
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirectHandler())
            with opener.open(request, timeout=20) as response:
                if not 200 <= response.status < 300:
                    error = self._provider_http_error(response.status)
                else:
                    body = response.read(256 * 1024)
                    try:
                        decoded = json.loads(body)
                        if not isinstance(decoded, dict): raise ValueError
                        output_text = "".join(
                            part.get("text", "")
                            for item in decoded.get("output", []) if isinstance(item, dict)
                            for part in item.get("content", []) if isinstance(part, dict)
                            and part.get("type") == "output_text" and isinstance(part.get("text", ""), str)
                        )
                        if decoded.get("status") != "completed" or not output_text.strip():
                            error = "Provider accepted the request but returned no completed text"
                    except (json.JSONDecodeError, ValueError):
                        error = "Provider returned an invalid Responses API reply"
        except urllib.error.HTTPError as exc:
            error = self._provider_http_error(exc.code)
            try: exc.close()
            except Exception: pass
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, (TimeoutError, socket.timeout)):
                error = "Provider test timed out"
            elif isinstance(reason, ssl.SSLError):
                error = "Provider TLS connection failed"
            else:
                error = "Provider network connection failed"
        except (TimeoutError, socket.timeout):
            error = "Provider test timed out"
        except ssl.SSLError:
            error = "Provider TLS connection failed"
        except Exception:
            error = "Provider test failed unexpectedly"
        self.provider["verified"] = error is None
        self.provider["error"] = error
        self._save_provider_file()
        result = {"verified": error is None}
        if error: result["error"] = error
        return result

    @staticmethod
    def _provider_http_error(code: int) -> str:
        if 300 <= code < 400: return "Provider endpoint redirected; redirects are not followed"
        if code in (401, 403): return "Provider rejected the API key or account permission"
        if code in (402, 429): return "Provider reports a quota, billing, or rate limit issue"
        if code == 404: return "Responses endpoint or model was not found"
        if 400 <= code < 500: return f"Provider rejected the test request (HTTP {code})"
        return f"Provider service returned an error (HTTP {code})"

    def _load_capability(self) -> str:
        try:
            token = self.capability_file.read_text(encoding="ascii").strip()
            if len(token) >= 32: return token
        except OSError: pass
        token = secrets.token_urlsafe(32)
        self.capability_file.write_text(token, encoding="ascii")
        try: os.chmod(self.capability_file, 0o600)
        except OSError: pass
        return token

    def _engine_event(self, event: dict[str, Any]) -> None:
        params = event.get("params") or {}
        tid = params.get("threadId")
        if not tid: return
        method = event.get("method", "")
        thread_level = method.startswith("thread/")
        child = False
        with self._task_event_lock:
            target = next((task for task, items in self._task_events.items()
                           if items and items[0].get("threadId") == tid
                           and ((thread_level and not (params.get("turnId") or (params.get("turn") or {}).get("id")))
                                or self._event_matches_turn(params, items[0]))), None)
            if not target and isinstance(tid, str):
                target = self._agent_threads.get(tid)
                child = bool(target)
        if not target: return
        item = params.get("item") or {}
        kind, text = None, None
        if method.endswith("/delta") and ("agentMessage" in method or item.get("type") == "agentMessage"):
            delta = params.get("delta", params.get("text", ""))
            if not isinstance(delta, str): return
            if child:
                key = (target, str(tid))
                self._agent_buffers[key] = self._agent_buffers.get(key, "") + delta
            else:
                self._task_buffers[target] = self._task_buffers.get(target, "") + delta
                full_public = self.safe_text(self._task_buffers[target], 4000)
                previous_time, previous_len = self._live_updates.get(target, (0.0, 0))
                current_time = time.monotonic()
                if full_public and (not previous_time or current_time - previous_time >= 1.0 or len(self._task_buffers[target]) - previous_len >= 200):
                    self.store.progress(target, "liveMessage", {"text": full_public})
                    self._live_updates[target] = (current_time, len(self._task_buffers[target]))
        elif method.endswith("/completed") and item.get("type") == "agentMessage":
            text = self.safe_text(item.get("text", ""), 10000)
            if child:
                if text: self.store.progress(target, "agentMessage", {"agentThreadId": str(tid), "text": text})
                kind = None
            else:
                kind = "message"
                if text:
                    self._task_buffers[target] = text
                    # The final native item is the authoritative complete public
                    # assistant message. Flush it even if deltas were throttled.
                    self.store.progress(target, "liveMessage", {"text": text})
                    self._live_updates[target] = (time.monotonic(), len(text))
                if text: self.store.progress(target, "message", {"summary": self.safe_text(text, 300)})
        elif method in ("item/started", "item/completed") and item.get("type") in ("commandExecution", "toolCall", "webSearch", "collabAgentToolCall", "subAgentActivity", "mcpToolCall", "dynamicToolCall", "contextCompaction"):
            item_type = item.get("type")
            status = "started" if method.endswith("started") else (item.get("status") or "completed")
            label = {"commandExecution": "命令", "toolCall": "工具", "webSearch": "网页检索",
                     "collabAgentToolCall": "子智能体", "subAgentActivity": "子智能体", "mcpToolCall": "外部工具", "dynamicToolCall": "动态工具",
                     "contextCompaction": "上下文整理"}.get(item_type, "工具")
            is_compact = item_type == "contextCompaction"
            payload: dict[str, Any] = {"phase": "compact" if is_compact else "tool", "summary": label, "status": self.safe_text(status, 40)}
            if is_compact and isinstance(item.get("id"), str): payload["itemId"] = self.safe_text(item["id"], 100)
            if item_type == "commandExecution":
                # Keep only the executable label; never expose command arguments or output.
                command = item.get("command")
                if isinstance(command, str): payload["label"] = self.safe_text(command.strip().split()[0].split("/")[-1].split("\\")[-1], 80)
                code = item.get("exitCode")
                if isinstance(code, int): payload["exitCode"] = code
            elif item_type == "webSearch":
                query = item.get("query")
                if isinstance(query, str): payload["query"] = self.safe_text(query, 120)
            elif item_type == "collabAgentToolCall":
                agent_id = item.get("agentId") or item.get("id")
                tool = item.get("tool")
                receiver_ids = item.get("receiverThreadIds") or []
                agents_states = item.get("agentsStates") or {}
                receiver_agents = item.get("receiverAgents") or []
                agent_names: dict[str, str] = {}
                if isinstance(receiver_agents, dict):
                    receiver_agents = [{"threadId": key, **value} for key, value in receiver_agents.items() if isinstance(value, dict)]
                if isinstance(receiver_agents, list):
                    for agent in receiver_agents:
                        if not isinstance(agent, dict): continue
                        child_id = agent.get("threadId") or agent.get("agentThreadId") or agent.get("id")
                        name = agent.get("nickname") or agent.get("name") or agent.get("role")
                        if isinstance(child_id, str) and isinstance(name, str) and name.strip():
                            agent_names[child_id] = self.safe_text(name.strip(), 100)
                if isinstance(receiver_ids, list):
                    for receiver_id in receiver_ids:
                        if isinstance(receiver_id, str): self._agent_threads[receiver_id] = target
                public_states: dict[str, dict[str, str]] = {}
                if isinstance(agents_states, dict):
                    for child_id, state in agents_states.items():
                        if not isinstance(child_id, str): continue
                        details = state if isinstance(state, dict) else {"status": state}
                        public: dict[str, str] = {}
                        status_value = details.get("status")
                        if isinstance(status_value, str): public["status"] = self.safe_text(status_value, 40)
                        name = agent_names.get(child_id) or details.get("nickname") or details.get("name") or details.get("role")
                        if isinstance(name, str) and name.strip(): public["name"] = self.safe_text(name.strip(), 100)
                        message = details.get("message")
                        if isinstance(message, dict):
                            message = message.get("text") or message.get("summary") or message.get("message")
                        if isinstance(message, str) and message.strip(): public["summary"] = self.safe_text(message.strip(), 240)
                        if public: public_states[self.safe_text(child_id, 80)] = public
                if isinstance(agent_id, str): payload["agentId"] = self.safe_text(agent_id, 80)
                if isinstance(tool, str): payload["tool"] = self.safe_text(tool, 40)
                self.store.progress(target, "agent", {"phase": "agent", "summary": label, "status": self.safe_text(status, 40),
                    **({"agentId": self.safe_text(agent_id, 80)} if isinstance(agent_id, str) else {}),
                    **({"tool": self.safe_text(tool, 40)} if isinstance(tool, str) else {}),
                    **({"agentThreadIds": [self.safe_text(x, 80) for x in receiver_ids if isinstance(x, str)]} if isinstance(receiver_ids, list) else {}),
                    **({"agentsStates": public_states} if public_states else {})})
            elif item_type == "subAgentActivity":
                agent_thread_id = item.get("agentThreadId")
                agent_path = item.get("agentPath")
                agent_kind = item.get("kind")
                if isinstance(agent_thread_id, str): self._agent_threads[agent_thread_id] = target
                self.store.progress(target, "agent", {"phase": "agent", "summary": label,
                    "status": self.safe_text(agent_kind or status, 40),
                    **({"agentThreadId": self.safe_text(agent_thread_id, 80)} if isinstance(agent_thread_id, str) else {}),
                    **({"agentPath": self.safe_text(agent_path, 100)} if isinstance(agent_path, str) else {})})
            if is_compact:
                self.store.progress(target, "compact", payload)
            else: self.store.progress(target, "tool", payload)
            kind, text = "tool", label + " " + status
        elif "tokenusage" in method.lower() or method == "thread/tokenUsage/updated":
            raw_usage = params.get("tokenUsage") or params.get("usage") or {}
            def breakdown(value: Any) -> dict[str, int]:
                if not isinstance(value, dict): return {}
                return {key: int(value[key]) for key in ("inputTokens", "outputTokens", "totalTokens", "cachedInputTokens", "reasoningOutputTokens", "cacheWriteInputTokens")
                        if isinstance(value.get(key), (int, float)) and value[key] >= 0}
            last = breakdown(raw_usage.get("last"))
            total = breakdown(raw_usage.get("total"))
            window = raw_usage.get("modelContextWindow")
            child = isinstance(tid, str) and self._agent_threads.get(tid) == target and not any(
                entry and entry[0].get("threadId") == tid for entry in self._task_events.values())
            current_context = self.usage.get(target, {}).get("context", {})
            usage = {"context": {**current_context, "agents": dict(current_context.get("agents", {}))}}
            context = {"windowTokens": int(window) if isinstance(window, (int, float)) and window > 0 else None,
                       "contextWindow": int(window) if isinstance(window, (int, float)) and window > 0 else None,
                       "inputTokens": last.get("inputTokens", total.get("inputTokens")),
                       "outputTokens": last.get("outputTokens", total.get("outputTokens")),
                       "totalTokens": total.get("totalTokens", last.get("totalTokens")), "last": last, "total": total}
            if child: usage["context"]["agents"][self.safe_text(tid, 80)] = context
            else: usage["context"].update(context)
            self.usage[target] = usage
            self.store.progress(target, "usage", usage)
            kind, text = "usage", usage
        elif method == "thread/compacted":
            # Older servers emit a thread-level completion instead of an item lifecycle.
            # Keep it separately so task_detail can deduplicate it against item events.
            self.store.progress(target, "compact", {"phase": "compact", "summary": "上下文整理完成",
                "status": "completed", "legacy": True})
            kind, text = "compact", "上下文整理完成"
        elif "compact" in method.lower() or "compaction" in method.lower():
            self.store.progress(target, "compact", {"phase": "compact", "summary": "正在整理上下文"})
            kind, text = "compact", "正在整理上下文"
        elif thread_level:
            status = params.get("status")
            if isinstance(status, str):
                self.store.progress(target, "phase", {"phase": "thread", "summary": self.safe_text(status, 100)})
        elif child and method == "turn/completed":
            turn = params.get("turn") or {}
            status = params.get("status", turn.get("status"))
            mapped = {"completed": "completed", "failed": "failed", "error": "failed",
                      "interrupted": "interrupted", "cancelled": "interrupted", "canceled": "interrupted"}.get(status, "unknown")
            self.store.progress(target, "agentState", {"id": self.safe_text(tid, 80), "status": mapped})
            return
        elif method == "turn/completed":
            turn = params.get("turn") or {}
            status = params.get("status", turn.get("status"))
            task = self.store.row(target)
            if not task or task["status"] in ("stopped", "failed"): return
            if status in ("failed", "error"):
                error = self.safe_text(params.get("error", turn.get("error", "Codex turn failed")), 1000)
                self.store.update(target, status="failed", error=error)
                self.store.event(target, "error", error)
            elif status in ("interrupted", "cancelled", "canceled"):
                self.store.update(target, status="stopped" if task["stop_requested"] else "interrupted")
                self.store.event(target, "status", self.store.row(target)["status"])
            elif status == "completed":
                result = self._last_message(target)
                self.store.update(target, status="completed", result=result)
                if result: self.store.add_message(target, "assistant", result, "completed", turn_id=(params.get("turnId") or turn.get("id")))
                self.store.event(target, "status", "completed")
            else:
                error = "Codex turn ended without a successful terminal status"
                self.store.update(target, status="interrupted", error=error)
                self.store.event(target, "status", "interrupted")
            with self.lock: self._sync_active_task_id()
            self._clear_task_mapping(target)
            self.store.progress(target, "phase", {"phase": self.store.row(target)["status"], "summary": self.store.row(target)["status"]})
            threading.Thread(target=self._start_waiting_queued, name="queued-tasks", daemon=True).start()
            return
        if kind and text:
            self.store.event(target, kind, text)

    def _last_message(self, task_id: str) -> str:
        return self.safe_text(self._task_buffers.get(task_id, ""), 10000)

    @staticmethod
    def _event_matches_turn(params: dict[str, Any], mapping: dict[str, Any]) -> bool:
        event_turn = params.get("turnId") or (params.get("turn") or {}).get("id")
        current_turn = mapping.get("turnId")
        if current_turn is not None:
            return event_turn == current_turn
        previous_turn = mapping.get("previousTurnId")
        if event_turn is None:
            return not previous_turn
        if event_turn == previous_turn:
            return False
        mapping["turnId"] = event_turn
        return True

    def _clear_task_mapping(self, task_id: str) -> None:
        with self._task_event_lock:
            self._task_events.pop(task_id, None)
            self._agent_threads = {thread: owner for thread, owner in self._agent_threads.items() if owner != task_id}
        self._task_buffers.pop(task_id, None)
        self._live_updates.pop(task_id, None)
        self._agent_buffers = {key: value for key, value in self._agent_buffers.items() if key[0] != task_id}

    def _engine_request(self, request: dict[str, Any]) -> None:
        params = request.get("params") or {}
        thread_id = params.get("threadId")
        turn_id = params.get("turnId")
        with self._task_event_lock:
            task_id = next((tid for tid, entries in self._task_events.items()
                            if entries and entries[0].get("threadId") == thread_id
                            and self._event_matches_turn({"turnId": turn_id}, entries[0])), None)
            if not task_id and isinstance(thread_id, str): task_id = self._agent_threads.get(thread_id)
        details = {"id": request["key"], "method": short_text(request["method"], 200),
                   "title": self.safe_text(params.get("title") or params.get("reason") or request["method"], 500),
                   "taskId": task_id, "_rpcId": request["id"]}
        with self._approval_lock:
            self.pending_approvals[request["key"]] = details

    def approvals(self) -> list[dict[str, Any]]:
        with self._approval_lock:
            return [{k: v for k, v in item.items() if not k.startswith("_")}
                    for item in self.pending_approvals.values()]

    def decide_approval(self, approval_id: str, decision: str) -> dict[str, Any]:
        if decision not in ("accept", "decline"): raise ValueError("Decision must be accept or decline")
        with self._approval_lock:
            item = self.pending_approvals.pop(approval_id, None)
        if not item: raise KeyError("Approval not found")
        try:
            self.engine.respond(item["_rpcId"], {"decision": decision})
        except Exception:
            with self._approval_lock: self.pending_approvals[approval_id] = item
            raise
        return {"id": approval_id, "decision": decision}

    def _engine_disconnected(self) -> None:
        with self.lock:
            self._loaded_threads.clear()
            active_ids = [r["id"] for r in self.store.active_rows()]
            for task_id in active_ids:
                self.store.update(task_id, status="interrupted", error="Codex app-server disconnected")
                self.store.event(task_id, "status", "interrupted")
            self.active_task_id = None
        for task_id in active_ids: self._clear_task_mapping(task_id)
        with self._approval_lock:
            self.pending_approvals.clear()

    def status(self) -> dict[str, Any]:
        e = self.engine
        if self.provider_configured():
            account = {"authenticated": True, "type": "apiKey"}
        else:
            account = e.account.copy() if e else {"authenticated": False, "type": None}
        if e and e.account_error: account["error"] = e.account_error
        active_rows = self.store.active_rows()
        active = next((r for r in active_rows if r["id"] == self.active_task_id), active_rows[0] if active_rows else None)
        return {"app": {"name": "DAXQO", "version": "0.3.0"}, "service": {"pid": os.getpid(), "startedAt": self.started_at},
                "engine": {"installed": bool(self.engine_path or self.engine), "version": e.version if e else None, "connected": bool(e and e.connected)},
                "account": account, "activeTaskId": active["id"] if active else None, "activeTaskIds": [r["id"] for r in active_rows],
                "workspace": str(self.workspace),
                "executionScope": "phone-linux" if self.sandbox == "danger-full-access" else "workspace",
                "provider": self.public_provider(),
                **({"engineError": self.safe_text(self.engine_error, 300)} if self.engine_error else {})}

    def reconnect(self, _provider_update: bool = False) -> dict[str, Any]:
        with self.lock:
            if self._has_active_tasks():
                raise ValueError("Cannot reconnect while a task is active")
            if self.reconnecting and not _provider_update: raise ValueError("Codex app-server reconnect is already in progress")
            if not self.engine_path: raise ValueError("Codex app-server executable was not found")
            was_reconnecting = self.reconnecting
            self.reconnecting = True
            previous = self.engine
            self.engine = None
            self._loaded_threads.clear()
        try:
            if previous: previous.close()
            engine = self._new_engine()
            engine.start()
            self.engine = engine
            self.engine_error = None
            return self.status()
        except Exception as exc:
            if "engine" in locals(): engine.close()
            self.engine = None
            self.engine_error = self.safe_text(str(exc), 300)
            raise ValueError(f"Could not reconnect Codex app-server: {self.engine_error}")
        finally:
            if not _provider_update or not was_reconnecting: self.reconnecting = False

    def start_task(self, body: dict[str, Any], resume_id: str | None = None) -> dict[str, Any]:
        body = self.services.prepare_body(body)
        if not self.engine or not self.engine.connected: raise ValueError("Codex app-server is unavailable")
        if self.reconnecting: raise ValueError("Codex app-server is reconnecting")
        if not self.provider_configured() and not self.engine.account.get("authenticated"):
            raise ValueError("Configure a model API provider or sign in to Codex before starting a task")
        with self.lock:
            if self.reconnecting: raise ValueError("Codex app-server is reconnecting")
            if len(self.store.active_rows()) >= self.max_concurrent_tasks():
                raise ValueError(f"并发任务数已达上限（{self.max_concurrent_tasks()}），请等待其他任务完成后再试。")
            if resume_id:
                row = self.store.row(resume_id)
                if not row: raise KeyError("Task not found")
                if row["status"] not in ("interrupted", "stopped", "failed"): raise ValueError("Only interrupted, stopped, or failed tasks can be resumed")
                if not row.get("thread_id"): raise ValueError("This task has no saved Codex thread to resume")
                tid = row["id"]
                self.store.update(tid, status="queued", error=None, stop_requested=0)
                self.store.event(tid, "status", "queued; resuming saved Codex thread")
            else:
                prompt = body.get("prompt")
                if not isinstance(prompt, str) or not prompt.strip(): raise ValueError("Prompt is required")
                tid = str(uuid.uuid4()); stamp = now()
                display_title = body.get("_displayText")
                attachments = body.get("_attachments")
                if body.get("title"):
                    title = body["title"]
                elif isinstance(display_title, str) and display_title.strip():
                    title = display_title.strip().splitlines()[0][:100]
                elif isinstance(attachments, list) and attachments:
                    first_name = attachments[0].get("name") if isinstance(attachments[0], dict) else None
                    title = "附件：" + short_text(first_name or "已导入文件", 80)
                else:
                    title = prompt.strip().splitlines()[0][:100]
                with self.store.lock, self.store.db:
                    chosen_model = body.get("model") or (self.provider["model"] if self.provider_configured() else None)
                    self.store.db.execute("INSERT INTO tasks(id,title,prompt,status,created_at,updated_at,model) VALUES(?,?,?,?,?,?,?)", (tid, short_text(title, 200), prompt[:50000], "queued", stamp, stamp, chosen_model))
                display_prompt = body.get("_displayText")
                self.store.add_message(tid, "user", prompt[:50000], "queued", client_id="initial:" + tid, delivery="started",
                                       display_text=display_prompt if isinstance(display_prompt, str) else None,
                                       attachments=attachments if isinstance(attachments, list) else None)
                self.store.event(tid, "status", "queued")
            self._sync_active_task_id()
        threading.Thread(target=self._run_task, args=(tid, bool(resume_id)), name=f"task-{tid}", daemon=True).start()
        return self.store.public(self.store.row(tid))

    def _run_task(self, task_id: str, resume: bool) -> None:
        engine = self.engine
        assert engine
        row = self.store.row(task_id)
        self.store.update(task_id, status="running")
        self.store.event(task_id, "status", "running")
        self.store.progress(task_id, "phase", {"phase": "running", "summary": "正在执行"})
        try:
            if resume:
                thread_id = row["thread_id"]
                engine.request("thread/resume", {"threadId": thread_id, "sandbox": self.sandbox, "approvalPolicy": "on-request"})
            else:
                params: dict[str, Any] = {"cwd": str(self.workspace), "approvalPolicy": "on-request", "sandbox": self.sandbox}
                if row.get("model"): params["model"] = row["model"]
                result = engine.request("thread/start", params)
                thread_id = result.get("thread", {}).get("id", result.get("threadId"))
                if not thread_id: raise RpcError("Codex did not return a thread ID")
                self.store.update(task_id, thread_id=thread_id)
            self._loaded_threads.add(thread_id)
            self._task_buffers.pop(task_id, None)
            with self._task_event_lock:
                self._task_events[task_id] = [{"threadId": thread_id, "turnId": None, "previousTurnId": row.get("turn_id")}]
            # Resume is an explicit user action. Ask the saved thread to inspect its prior work
            # and continue only unfinished steps, avoiding blind replay of external actions.
            resume_prompt = ("Resume the user's saved task from the conversation above. First inspect the current "
                             "workspace and prior work to determine what has already completed. Do not repeat "
                             "completed external actions; continue only unfinished work. Original task: " + row["prompt"]) if resume else row["prompt"]
            with self.store.lock, self.store.db:
                self.store.db.execute("UPDATE messages SET status='sending',delivery='sending' WHERE task_id=? AND role='user' AND client_message_id=? AND status='queued'", (task_id, "initial:" + task_id))
            result = engine.request("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": resume_prompt}], **({"model": row["model"]} if row.get("model") else {})})
            turn_id = result.get("turn", {}).get("id", result.get("turnId"))
            if not turn_id: raise RpcError("Codex did not return a turn ID")
            if self.store.row(task_id)["status"] in ("queued", "running"):
                self.store.update(task_id, turn_id=turn_id)
            with self._task_event_lock:
                if task_id in self._task_events: self._task_events[task_id][0]["turnId"] = turn_id
            with self.store.lock, self.store.db:
                self.store.db.execute("UPDATE messages SET status='started',turn_id=?,delivery='started' WHERE task_id=? AND role='user' AND client_message_id=? AND status='sending'", (turn_id, task_id, "initial:" + task_id))
            threading.Thread(target=self._steer_waiting, args=(task_id, turn_id), name=f"steer-queued-{task_id}", daemon=True).start()
        except Exception as exc:
            interrupted = not engine.connected
            error = self.safe_text("Codex app-server disconnected" if interrupted else str(exc), 1000)
            row_now = self.store.row(task_id)
            if row_now and row_now["status"] in ("queued", "running"):
                self.store.update(task_id, status="interrupted" if interrupted else "failed", error=error)
                self.store.event(task_id, "status" if interrupted else "error", error)
                with self.store.lock, self.store.db:
                    self.store.db.execute("UPDATE messages SET status='failed',delivery='failed',error=?,retry_safe=? WHERE task_id=? AND role='user' AND status IN ('queued','sending')", (error, int(bool(getattr(exc, "rpc_rejected", False))), task_id))
            with self.lock:
                self._sync_active_task_id()
            self._clear_task_mapping(task_id)
            threading.Thread(target=self._start_waiting_queued, name="queued-tasks", daemon=True).start()

    def send_message(self, task_id: str, body: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], str]:
        body = self.services.prepare_body(body)
        text = body.get("text")
        client_id = body.get("clientMessageId")
        if not isinstance(text, str) or not text.strip(): raise ValueError("Message text is required")
        if not isinstance(client_id, str) or not client_id.strip() or len(client_id) > 200: raise ValueError("clientMessageId is required")
        text = text[:50000]
        row = self.store.row(task_id)
        if not row: raise KeyError("Task not found")
        existing = self.store.message_by_client(task_id, client_id)
        if existing:
            if existing["status"] == "failed":
                if not body.get("retry"): raise ValueError("Message previously failed; retry:true is required")
                if not existing.get("retry_safe"): raise ValueError("Retry is unsafe because delivery may have occurred")
                text = existing["text"]
            else:
                delivery = existing.get("delivery") or ("queued" if existing["status"] == "queued" else existing["status"])
                return self.store.public(row), self.store.message_public(existing), delivery
        if not self.engine or not self.engine.connected: raise ValueError("Codex app-server is unavailable")
        if self.reconnecting: raise ValueError("Codex app-server is reconnecting")
        with self.lock:
            existing = self.store.message_by_client(task_id, client_id)
            retry_existing = None
            if existing:
                if existing["status"] == "failed":
                    if not body.get("retry"): raise ValueError("Message previously failed; retry:true is required")
                    if not existing.get("retry_safe"): raise ValueError("Retry is unsafe because delivery may have occurred")
                    text = existing["text"]
                    retry_existing = existing
                else:
                    delivery = existing.get("delivery") or existing["status"]
                    return self.store.public(row), self.store.message_public(existing), delivery
            else:
                message = None
            active_ids = {r["id"] for r in self.store.active_rows()}
            row = self.store.row(task_id)
            mapping = self._task_events.get(task_id, [{}])[0] if task_id in self._task_events else {}
            # While a new turn is being opened, the saved row may still contain the prior turn ID.
            # An explicit mapping with turnId=None means queue until the new turn is known.
            turn = ((mapping.get("turnId") if mapping else row.get("turn_id"))
                    if row["status"] in ("queued", "running") else None)
            if body.get("expectedTurnId") is not None and body.get("expectedTurnId") != turn:
                raise ValueError("expectedTurnId must match the active turn")
            if not turn and body.get("expectedTurnId") not in (None, ""):
                raise ValueError("There is no active turn matching expectedTurnId")
            if retry_existing:
                self.store.update_message(retry_existing["id"], status="queued", delivery="queued", error=None, retry_safe=0)
                message = {**self.store.message_public(retry_existing), "status": "queued", "retrySafe": False}
            else:
                display_text = body.get("_displayText")
                attachments = body.get("_attachments")
                message = self.store.add_message(task_id, "user", text, "queued", client_id=client_id, delivery="queued",
                    display_text=display_text if isinstance(display_text, str) else None,
                    attachments=attachments if isinstance(attachments, list) else None)
            if row["status"] in ("queued", "running"):
                if not turn: return self.store.public(row), message, "queued"
                thread_id = row.get("thread_id")
            else:
                if row["status"] not in ("completed", "interrupted", "stopped", "failed"):
                    raise ValueError("Task cannot accept a message in its current state")
                if not row.get("thread_id"): raise ValueError("Task has no saved Codex thread")
                if len(active_ids) >= self.max_concurrent_tasks():
                    raise ValueError(f"并发任务数已达上限（{self.max_concurrent_tasks()}），请等待其他任务完成后再试。")
                self.store.update(task_id, status="queued", error=None, stop_requested=0)
                self._sync_active_task_id()
                if len(active_ids) < self.max_concurrent_tasks():
                    threading.Thread(target=self._start_queued, args=(task_id,), name=f"followup-{task_id}", daemon=True).start()
                return self.store.public(self.store.row(task_id)), message, "queued"
            self.store.update_message(message["id"], status="sending", delivery="sending")
        try:
            self.engine.request("turn/steer", {"threadId": thread_id, "expectedTurnId": turn,
                "input": [{"type": "text", "text": text}], "clientUserMessageId": client_id}, timeout=RPC_TIMEOUT)
            self.store.update_message(message["id"], status="steered", turn_id=turn, delivery="steered")
            return self.store.public(self.store.row(task_id)), {**message, "status": "steered", "turnId": turn}, "steered"
        except Exception as exc:
            latest = self.store.row(task_id)
            message_text = str(exc).lower()
            race_rejection = any(part in message_text for part in ("active turn cannot accept", "not steerable", "active turn not found"))
            if race_rejection or (bool(getattr(exc, "rpc_rejected", False)) and latest and latest["status"] in ("completed", "interrupted", "stopped")):
                self.store.update_message(message["id"], status="queued", delivery="queued")
                threading.Thread(target=self._start_queued, args=(task_id,), name=f"followup-{task_id}", daemon=True).start()
                return self.store.public(self.store.row(task_id)), message, "queued"
            error = self.safe_text(str(exc), 500)
            self.store.update_message(message["id"], status="failed", delivery="failed", error=error,
                                      retry_safe=int(bool(getattr(exc, "rpc_rejected", False))))
            raise ValueError(f"Message delivery failed: {error}")

    def _steer_waiting(self, task_id: str, turn_id: str) -> None:
        row = self.store.row(task_id)
        if not row or not row.get("thread_id"): return
        with self.store.lock:
            pending = [dict(m) for m in self.store.db.execute("SELECT * FROM messages WHERE task_id=? AND role='user' AND status='queued' ORDER BY rowid", (task_id,))]
        for message in pending:
            if self.store.row(task_id)["status"] != "running": return
            self.store.update_message(message["id"], status="sending", delivery="sending")
            try:
                self.engine.request("turn/steer", {"threadId": row["thread_id"], "expectedTurnId": turn_id,
                    "input": [{"type": "text", "text": message["text"]}], "clientUserMessageId": message.get("client_message_id")}, timeout=RPC_TIMEOUT)
                self.store.update_message(message["id"], status="steered", turn_id=turn_id, delivery="steered")
            except Exception as exc:
                message_text = str(exc).lower()
                if any(part in message_text for part in ("active turn cannot accept", "not steerable", "active turn not found")):
                    self.store.update_message(message["id"], status="queued", delivery="queued")
                    current = self.store.row(task_id)
                    if current and current["status"] in ("completed", "interrupted", "stopped"):
                        threading.Thread(target=self._start_queued, args=(task_id,), name=f"followup-{task_id}", daemon=True).start()
                    continue
                self.store.update_message(message["id"], status="failed", delivery="failed", error=self.safe_text(str(exc), 500),
                                          retry_safe=int(bool(getattr(exc, "rpc_rejected", False))))

    def _start_queued(self, task_id: str) -> None:
        with self.lock:
            row = self.store.row(task_id)
            if not row or row["status"] not in ("queued", "completed", "interrupted", "stopped", "failed"): return
            active_rows = self.store.active_rows()
            active_ids = {r["id"] for r in active_rows}
            running_count = sum(r["status"] == "running" for r in active_rows)
            if running_count >= self.max_concurrent_tasks(): return
            with self.store.lock:
                pending = [dict(m) for m in self.store.db.execute("SELECT * FROM messages WHERE task_id=? AND role='user' AND status='queued' ORDER BY rowid", (task_id,))]
            if not pending: return
            if row["status"] == "queued" and not row.get("thread_id"):
                return # Initial turn is already owned by _run_task.
            self.active_task_id = task_id
            self.store.update(task_id, status="running", error=None)
            self._sync_active_task_id()
        with self._task_event_lock:
            self._task_events[task_id] = [{"threadId": row["thread_id"], "turnId": None, "previousTurnId": row.get("turn_id")}]
        self._task_buffers[task_id] = ""
        self.store.event(task_id, "status", "running")
        self.store.progress(task_id, "phase", {"phase": "running", "summary": "正在继续"})
        try:
            if row["thread_id"] not in self._loaded_threads:
                self.engine.request("thread/resume", {"threadId": row["thread_id"], "sandbox": self.sandbox,
                    "approvalPolicy": "on-request"})
                self._loaded_threads.add(row["thread_id"])
            for message in pending: self.store.update_message(message["id"], status="sending", delivery="sending")
            result = self.engine.request("turn/start", {"threadId": row["thread_id"],
                "input": [{"type": "text", "text": m["text"]} for m in pending], **({"model": row["model"]} if row.get("model") else {})})
            turn_id = result.get("turn", {}).get("id", result.get("turnId"))
            if not turn_id: raise RpcError("Codex did not return a turn ID")
            current = self.store.row(task_id)
            if current and current["status"] == "running": self.store.update(task_id, turn_id=turn_id)
            with self._task_event_lock:
                if task_id in self._task_events: self._task_events[task_id][0]["turnId"] = turn_id
            for message in pending:
                with self.store.lock:
                    latest = self.store.db.execute("SELECT status FROM messages WHERE id=?", (message["id"],)).fetchone()
                if latest and latest["status"] == "sending": self.store.update_message(message["id"], status="started", delivery="started", turn_id=turn_id)
            threading.Thread(target=self._steer_waiting, args=(task_id, turn_id), name=f"steer-queued-{task_id}", daemon=True).start()
        except Exception as exc:
            error = self.safe_text(str(exc), 500)
            for message in pending: self.store.update_message(message["id"], status="failed", delivery="failed", error=error,
                                                               retry_safe=int(bool(getattr(exc, "rpc_rejected", False))))
            self.store.update(task_id, status="failed", error=error)
            self.store.event(task_id, "error", error)
            with self.lock:
                self._sync_active_task_id()
            threading.Thread(target=self._start_waiting_queued, name="queued-tasks", daemon=True).start()

    def _start_waiting_queued(self) -> None:
        with self.lock:
            active_rows = self.store.active_rows()
            if sum(r["status"] == "running" for r in active_rows) >= self.max_concurrent_tasks(): return
            with self.store.lock:
                rows = self.store.db.execute("SELECT id FROM tasks WHERE status='queued' AND thread_id IS NOT NULL ORDER BY created_at").fetchall()
                task_id = next((r["id"] for r in rows if self.store.db.execute(
                    "SELECT 1 FROM messages WHERE task_id=? AND role='user' AND status='queued' LIMIT 1", (r["id"],)).fetchone()), None)
        if task_id:
            threading.Thread(target=self._start_queued, args=(task_id,), name=f"queued-{task_id}", daemon=True).start()

    def stop(self, task_id: str) -> dict[str, Any]:
        row = self.store.row(task_id)
        if not row: raise KeyError("Task not found")
        if row["status"] in ("queued", "running"):
            self.store.update(task_id, stop_requested=1)
            try:
                with self._task_event_lock: turn = self._task_events.get(task_id, [{}])[0].get("turnId")
                if turn: self.engine.request("turn/interrupt", {"threadId": row.get("thread_id"), "turnId": turn}, timeout=10)
                else: raise RpcError("Task has not started a turn yet")
            except Exception as exc:
                # Keep task active; the UI must not claim cancellation succeeded without server confirmation.
                raise ValueError(f"Could not stop task: {self.safe_text(str(exc), 200)}")
        return self.store.public(self.store.row(task_id))

def create_handler(harness: Harness):
    class Handler(BaseHTTPRequestHandler):
        server_version = "DAXQO/0.3"
        def log_message(self, fmt: str, *args: Any) -> None:
            # Avoid request bodies, headers, auth values, and URL query strings in logs.
            sys.stderr.write(f"{self.client_address[0]} {self.command} {self.path.split('?')[0]}\n")

        def _json(self, status: int, data: dict[str, Any]) -> None:
            raw = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw))); self.send_header("Cache-Control", "no-store")
            self.end_headers(); self.wfile.write(raw)

        def _download(self, filename: str, raw: bytes) -> None:
            name = filename.replace("\\", "/").split("/")[-1]
            name = "".join(ch for ch in name if ch >= " " and ch not in '\\"/;') or "download"
            fallback = "".join(ch if 32 <= ord(ch) < 127 else "_" for ch in name).replace("\\", "_").replace('"', "_")
            encoded = urllib.parse.quote(name, safe="")
            disposition = f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{encoded}'
            self.send_response(200); self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", disposition); self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(raw)

        def _authorized(self) -> bool:
            host = self.headers.get("Host", "")
            origin = self.headers.get("Origin")
            if host not in (f"127.0.0.1:{harness.port}", f"localhost:{harness.port}"):
                self._json(403, {"error": "Invalid host"}); return False
            if origin and origin not in (f"http://{host}", f"https://{host}"):
                self._json(403, {"error": "Cross-origin request rejected"}); return False
            expected = "Bearer " + harness.capability
            supplied = self.headers.get("Authorization", "")
            if not secrets.compare_digest(supplied, expected):
                self._json(401, {"error": "Authentication required"}); return False
            return True

        def _body(self) -> dict[str, Any]:
            n = int(self.headers.get("Content-Length", "0"))
            if n > MAX_BODY: raise ValueError("Request body is too large")
            value = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(value, dict): raise ValueError("JSON object required")
            return value

        def do_GET(self) -> None:
            path = urllib.parse.urlsplit(self.path).path
            if path == "/healthz": return self._json(200, {"ok": True})
            if path == "/" or path.startswith("/static/"):
                root = Path(__file__).parent / "static"
                target = root / ("index.html" if path == "/" else path.removeprefix("/static/"))
                try: resolved = target.resolve(strict=True)
                except OSError: return self._json(404, {"error": "Not found"})
                if root.resolve() not in resolved.parents: return self._json(404, {"error": "Not found"})
                if not resolved.is_file(): return self._json(404, {"error": "Not found"})
                raw = resolved.read_bytes()
                content_type = {".html": "text/html; charset=utf-8", ".png": "image/png", ".svg": "image/svg+xml",
                                ".json": "application/json; charset=utf-8", ".css": "text/css; charset=utf-8",
                                ".js": "text/javascript; charset=utf-8"}.get(resolved.suffix.lower(), "application/octet-stream")
                self.send_response(200); self.send_header("Content-Type", content_type); self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(raw); return
            if not self._authorized(): return
            if harness.services.handle_http(self, "GET", path): return
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query, keep_blank_values=True)
            if path == "/api/files":
                try: return self._json(200, harness.list_workspace_files(query.get("path", [""])[0]))
                except FileNotFoundError: return self._json(404, {"error": "Not found"})
                except PermissionError: return self._json(400, {"error": "File access denied"})
                except ValueError as exc: return self._json(400, {"error": str(exc)})
                except OSError: return self._json(404, {"error": "Not found"})
            if path == "/api/files/text":
                try: return self._json(200, harness.read_workspace_text(query.get("path", [""])[0]))
                except FileNotFoundError: return self._json(404, {"error": "Not found"})
                except PermissionError: return self._json(400, {"error": "File access denied"})
                except OverflowError as exc: return self._json(413, {"error": str(exc)})
                except ValueError as exc: return self._json(400, {"error": str(exc)})
                except OSError: return self._json(404, {"error": "Not found"})
            if path == "/api/files/download":
                try:
                    filename, raw = harness.read_workspace_download(query.get("path", [""])[0])
                    return self._download(filename, raw)
                except FileNotFoundError: return self._json(404, {"error": "Not found"})
                except PermissionError: return self._json(400, {"error": "File access denied"})
                except OverflowError as exc: return self._json(413, {"error": str(exc)})
                except ValueError as exc: return self._json(400, {"error": str(exc)})
                except OSError: return self._json(404, {"error": "Not found"})
            if path == "/api/status": return self._json(200, harness.status())
            if path == "/api/approvals": return self._json(200, {"approvals": harness.approvals()})
            if path == "/api/provider": return self._json(200, harness.public_provider())
            if path == "/api/models":
                return self._json(200, {"models": harness.models()})
            if path == "/api/tasks": return self._json(200, {"tasks": [harness.store.public(r) for r in harness.store.rows()]})
            if path.startswith("/api/tasks/") and path.endswith("/messages"):
                task_id = path.split("/")[3]
                if not harness.store.row(task_id): return self._json(404, {"error": "Task not found"})
                return self._json(200, {"messages": harness.store.messages(task_id)})
            if path.startswith("/api/tasks/"):
                task_id = path.split("/")[3]
                row = harness.store.row(task_id)
                if not row: return self._json(404, {"error": "Task not found"})
                try: after = max(0, int(urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("after", ["0"])[0]))
                except ValueError: return self._json(400, {"error": "Invalid event cursor"})
                return self._json(200, {"task": harness.store.public(row, True), "events": harness.store.events(task_id, after), **harness.task_detail(task_id)})
            return self._json(404, {"error": "Not found"})

        def do_POST(self) -> None:
            if not self._authorized(): return
            path = urllib.parse.urlsplit(self.path).path
            if harness.services.handle_http(self, "POST", path): return
            try:
                if path == "/api/login/start":
                    if not harness.engine: raise ValueError("Codex app-server is unavailable")
                    return self._json(200, harness.engine.begin_login())
                if path == "/api/provider/test":
                    return self._json(200, harness.test_provider())
                if path == "/api/provider":
                    return self._json(200, harness.save_provider(self._body()))
                if path == "/api/engine/reconnect":
                    return self._json(200, {"status": harness.reconnect()})
                parts = path.split("/")
                if len(parts) == 4 and parts[:3] == ["", "api", "approvals"]:
                    body = self._body()
                    return self._json(200, {"approval": harness.decide_approval(parts[3], body.get("decision"))})
                if path == "/api/tasks":
                    return self._json(201, {"task": harness.start_task(self._body())})
                if len(parts) == 5 and parts[:3] == ["", "api", "tasks"] and parts[4] == "messages":
                    task, message, delivery = harness.send_message(parts[3], self._body())
                    return self._json(202, {"task": task, "message": message, "delivery": delivery})
                parts = path.split("/")
                if len(parts) == 5 and parts[:3] == ["", "api", "tasks"]:
                    task_id, action = parts[3], parts[4]
                    if action == "stop": return self._json(200, {"task": harness.stop(task_id)})
                    if action == "resume": return self._json(202, {"task": harness.start_task({}, task_id)})
                return self._json(404, {"error": "Not found"})
            except KeyError as exc: return self._json(404, {"error": str(exc).strip("'")})
            except (ValueError, json.JSONDecodeError) as exc:
                message = str(exc)
                conflict = any(marker in message.lower() for marker in ("running", "active", "sign in", "retry", "并发任务数已达上限"))
                return self._json(409 if conflict else 400, {"error": harness.safe_text(message, 500)})
            except Exception as exc: return self._json(502, {"error": harness.safe_text(str(exc), 500)})

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1"); parser.add_argument("--port", type=int, default=18880)
    parser.add_argument("--codex", default="codex"); parser.add_argument("--state", default="./state")
    parser.add_argument("--workspace", default="./workspace"); parser.add_argument("--print-url", action="store_true")
    parser.add_argument("--sandbox", choices=("read-only", "workspace-write", "danger-full-access"), default="workspace-write")
    args = parser.parse_args()
    state, workspace = Path(args.state).resolve(), Path(args.workspace).resolve()
    harness = Harness(args.codex, state, workspace, args.host, args.port, sandbox=args.sandbox)
    url = f"http://{args.host}:{args.port}/#token={harness.capability}"
    url_file = state / "gui-url"
    url_file.write_text(url, encoding="utf-8")
    try: os.chmod(url_file, 0o600)
    except OSError: pass
    if args.print_url: print(url, flush=True)
    http = ThreadingHTTPServer((args.host, args.port), create_handler(harness))
    serving = threading.Event()
    def serve_loop() -> None:
        serving.set()
        http.serve_forever()
    def request_shutdown(_signum: int | None = None, _frame: Any = None) -> None:
        def shutdown_when_ready() -> None:
            serving.wait()
            http.shutdown()
        threading.Thread(target=shutdown_when_ready, name="http-shutdown", daemon=True).start()
    previous_sigterm = signal.signal(signal.SIGTERM, request_shutdown)
    server_thread = threading.Thread(target=serve_loop, name="phone-harness-http")
    server_thread.start()
    serving.wait()
    try:
        ensure_runtime_control(Path(__file__).resolve().parent, state, workspace, args.port)
        harness.services.start()
        server_thread.join()
    except KeyboardInterrupt:
        request_shutdown()
        server_thread.join()
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
        http.server_close()
        harness.services.close()
        if harness.engine: harness.engine.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
