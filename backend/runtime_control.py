#!/usr/bin/env python3
"""Loopback-only lifecycle and verified-update bridge for DAXQO."""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import urlsplit

APP_VERSION = "0.3.0"
DEFAULT_PORT = 18882
DEFAULT_BACKEND_PORT = 18880
MAX_REQUEST = 1024 * 1024
MAX_FILE = 2 * 1024 * 1024
MAX_UPDATE = 16 * 1024 * 1024
ALLOWED_FILES = {
    "server.py", "supervisor.py", "runtime_control.py", "test_server.py",
    "test_supervisor.py", "test_runtime_control.py", "models-deepseek.json",
    "WORKSPACE_AGENTS.md",
    "task_services.py", "test_task_services.py",
}


class BridgeError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _safe_relative(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise BridgeError(400, "Invalid relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise BridgeError(400, "Invalid relative path")
    normalized = path.as_posix()
    if normalized != value or not (normalized in ALLOWED_FILES or normalized.startswith("static/")):
        raise BridgeError(403, "Source path is not allowed")
    return normalized


def _check_components(root: Path, relative: str, *, final_regular: bool = True,
                      allow_missing_parents: bool = False) -> Path:
    """Return a path only after rejecting symlinks and non-regular final entries."""
    current = root
    parts = PurePosixPath(relative).parts
    for index, part in enumerate(parts):
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            if index != len(parts) - 1 and not allow_missing_parents:
                raise BridgeError(400, "Path parent does not exist")
            continue
        if stat.S_ISLNK(info.st_mode):
            raise BridgeError(403, "Symlink paths are not allowed")
        if index < len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
            raise BridgeError(400, "Path parent is not a directory")
        if index == len(parts) - 1 and final_regular and not stat.S_ISREG(info.st_mode):
            raise BridgeError(403, "Only regular files are allowed")
    return current


def _read_regular(path: Path, limit: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise BridgeError(403, "Only regular files are allowed")
            if info.st_size > limit:
                raise BridgeError(413, "File exceeds size limit")
            data = stream.read(limit + 1)
            if len(data) > limit:
                raise BridgeError(413, "File exceeds size limit")
            return data
    except BridgeError:
        raise
    except OSError as exc:
        raise BridgeError(400, "Unable to read requested file") from exc


def _atomic_write(root: Path, relative: str, content: bytes, mode: int) -> None:
    target = _check_components(root, relative, allow_missing_parents=True)
    target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    target = _check_components(root, relative, allow_missing_parents=True)
    temp = target.with_name("." + target.name + "." + secrets.token_hex(8) + ".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(temp, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp, mode)
        _check_components(root, relative, allow_missing_parents=True)
        os.replace(temp, target)
    except Exception:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _proc_info(pid: int) -> dict[str, Any] | None:
    """Read Linux process identity without trusting PID files alone."""
    proc = Path("/proc") / str(pid)
    try:
        raw = (proc / "stat").read_text(encoding="ascii")
        tail = raw[raw.rfind(")") + 2:].split()
        start_time = int(tail[19])  # /proc stat field 22, after pid/comm.
        status = (proc / "status").read_text(encoding="ascii")
        uid_line = next(line for line in status.splitlines() if line.startswith("Uid:"))
        uid = int(uid_line.split()[1])
        cmdline = (proc / "cmdline").read_bytes().split(b"\x00")
        args = [item.decode("utf-8", "strict") for item in cmdline if item]
        return {"pid": pid, "uid": uid, "startTime": start_time, "args": args}
    except (OSError, ValueError, StopIteration, UnicodeError, IndexError):
        return None


class RuntimeManager:
    def __init__(self, app: Path, stage: Path, port: int = DEFAULT_PORT,
                 backend_port: int = DEFAULT_BACKEND_PORT, *,
                 popen: Callable[..., Any] = subprocess.Popen,
                 probe: Callable[[], dict[str, Any] | None] | None = None,
                 proc_reader: Callable[[int], dict[str, Any] | None] = _proc_info,
                 tests_runner: Callable[[Path], int] | None = None):
        self.app = Path(os.path.abspath(app))
        self.stage = Path(os.path.abspath(stage))
        self.port, self.backend_port = int(port), int(backend_port)
        self.popen, self.proc_reader, self.tests_runner = popen, proc_reader, tests_runner
        self._probe_override = probe
        self._lock = threading.RLock()
        self._child: Any = None
        self._started_at = 0.0
        self._update: dict[str, Any] = {"state": "idle"}
        self._update_thread: threading.Thread | None = None
        self._payload_dir: Path | None = None
        self._lock_stream = None

    @property
    def state_dir(self) -> Path:
        return self.app / "state"

    @property
    def capability_file(self) -> Path:
        return self.state_dir / "capability"

    @property
    def supervisor_file(self) -> Path:
        return self.state_dir / "runtime-supervisor.json"

    @property
    def backup_root(self) -> Path:
        return self.state_dir / "backups"

    def capability(self) -> str:
        try:
            state_info = self.state_dir.lstat()
            if not stat.S_ISDIR(state_info.st_mode) or stat.S_ISLNK(state_info.st_mode):
                return ""
            if hasattr(os, "getuid") and state_info.st_uid != os.getuid():
                return ""
            info = self.capability_file.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                return ""
            if hasattr(os, "getuid") and info.st_uid != os.getuid():
                return ""
            if sys.platform.startswith("linux") and stat.S_IMODE(info.st_mode) & 0o077:
                return ""
            return self.capability_file.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            return ""

    def acquire_lock(self) -> None:
        if not sys.platform.startswith("linux"):
            raise RuntimeError("Runtime control is supported on Linux only")
        import fcntl
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.state_dir.is_symlink():
            raise RuntimeError("Unsafe state directory")
        if hasattr(os, "getuid") and self.state_dir.stat().st_uid != os.getuid():
            raise RuntimeError("State directory is not owned by this user")
        os.chmod(self.state_dir, 0o700)
        self._lock_stream = (self.state_dir / "runtime-control.lock").open("a+")
        os.chmod(self.state_dir / "runtime-control.lock", 0o600)
        try:
            fcntl.flock(self._lock_stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._lock_stream.close()
            self._lock_stream = None
            raise RuntimeError("Another runtime-control bridge is already running") from exc

    def backend_probe(self) -> dict[str, Any] | None:
        if self._probe_override:
            return self._probe_override()
        token = self.capability()
        if len(token) < 32:
            return None
        conn = http.client.HTTPConnection("127.0.0.1", self.backend_port, timeout=1.5)
        try:
            conn.request("GET", "/api/status", headers={"Host": f"127.0.0.1:{self.backend_port}",
                "Authorization": "Bearer " + token, "Connection": "close"})
            response = conn.getresponse()
            raw = response.read(64 * 1024 + 1)
            if response.status != 200 or len(raw) > 64 * 1024:
                return None
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("service"), dict):
                return None
            return data
        except (OSError, ValueError, UnicodeError, http.client.HTTPException):
            return None
        finally:
            conn.close()

    def _db_active(self) -> bool | None:
        db = self.state_dir / "harness.sqlite3"
        if not db.exists():
            return False
        try:
            if db.is_symlink() or not db.is_file():
                return None
            connection = sqlite3.connect(db.resolve(strict=True).as_uri() + "?mode=ro", uri=True, timeout=1)
            try:
                row = connection.execute("SELECT 1 FROM tasks WHERE status IN ('queued','running') LIMIT 1").fetchone()
                return row is not None
            finally:
                connection.close()
        except sqlite3.Error:
            return None

    def _active_task(self, backend: dict[str, Any] | None) -> bool | None:
        if backend is not None:
            return bool(backend.get("activeTaskId") or backend.get("activeTaskIds"))
        return self._db_active()

    def _supervisor_script(self) -> Path:
        script = self.app / "supervisor.py"
        _check_components(self.app, "supervisor.py")
        if not script.is_file():
            raise BridgeError(503, "The DAXQO supervisor is not installed")
        return script.resolve(strict=True)

    def _record_data(self) -> dict[str, Any] | None:
        try:
            if self.supervisor_file.is_symlink() or not self.supervisor_file.is_file():
                return None
            data = json.loads(self.supervisor_file.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return None
            return data
        except (OSError, ValueError):
            return None

    def _process_matches(self, record: dict[str, Any] | None) -> bool:
        if not record or not sys.platform.startswith("linux"):
            return False
        try:
            pid, start = int(record["pid"]), int(record["startTime"])
            uid, script = int(record["uid"]), str(record["script"])
        except (KeyError, TypeError, ValueError):
            return False
        info = self.proc_reader(pid)
        if not info or info.get("uid") != uid or info.get("uid") != os.getuid() or info.get("startTime") != start:
            return False
        try:
            expected = self._supervisor_script()
            args = info.get("args", [])
            return (Path(script).resolve(strict=True) == expected and len(args) == 6
                    and Path(args[1]).resolve(strict=True) == expected
                    and args[2:] == ["--app", str(self.app.resolve(strict=True)), "--sandbox", "danger-full-access"])
        except (OSError, RuntimeError, BridgeError, IndexError):
            return False

    def _owned_supervisor(self) -> dict[str, Any] | None:
        record = self._record_data()
        if self._process_matches(record):
            return record
        # A prior bridge instance may have launched the process. Adoption still
        # requires exact UID, script path, app argument, sandbox and proc start time.
        if not sys.platform.startswith("linux"):
            return None
        try:
            script = self._supervisor_script()
            for entry in Path("/proc").iterdir():
                if not entry.name.isdigit():
                    continue
                info = self.proc_reader(int(entry.name))
                if not info or info.get("uid") != os.getuid():
                    continue
                args = info.get("args", [])
                if (len(args) == 6 and Path(args[1]).resolve() == script
                        and args[2:] == ["--app", str(self.app.resolve()), "--sandbox", "danger-full-access"]):
                    adopted = {"pid": info["pid"], "uid": info["uid"], "script": str(script),
                               "startTime": info["startTime"]}
                    self._write_record(adopted)
                    return adopted
        except (OSError, BridgeError, RuntimeError, IndexError):
            pass
        return None

    def _write_record(self, record: dict[str, Any]) -> None:
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        temp = self.supervisor_file.with_suffix(".tmp")
        temp.write_text(json.dumps(record), encoding="utf-8")
        os.chmod(temp, 0o600)
        os.replace(temp, self.supervisor_file)

    def _launch_supervisor(self) -> None:
        if not sys.platform.startswith("linux"):
            raise BridgeError(503, "Runtime start is available on Linux only")
        script = self._supervisor_script()
        app = self.app.resolve(strict=True)
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.state_dir.is_symlink():
            raise BridgeError(403, "Unsafe state directory")
        log_path = self.state_dir / "runtime-supervisor.log"
        if log_path.is_symlink():
            raise BridgeError(403, "Unsafe supervisor log")
        log = log_path.open("ab", buffering=0)
        os.chmod(log_path, 0o600)
        try:
            child = self.popen([sys.executable, str(script), "--app", str(app), "--sandbox", "danger-full-access"],
                               cwd=str(app), stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                               close_fds=True, start_new_session=True)
        finally:
            log.close()
        deadline = time.monotonic() + 2.0
        record = None
        while time.monotonic() < deadline:
            info = self.proc_reader(int(child.pid))
            if info:
                record = {"pid": int(child.pid), "uid": int(info["uid"]), "script": str(script),
                          "startTime": int(info["startTime"])}
                if self._process_matches(record):
                    break
                record = None
            if child.poll() is not None:
                break
            time.sleep(.05)
        if not record:
            raise BridgeError(503, "Could not verify the DAXQO supervisor process")
        self._write_record(record)
        self._child = child
        self._started_at = time.monotonic()

    def start(self) -> tuple[int, dict[str, Any]]:
        with self._lock:
            backend = self.backend_probe()
            existing = self._owned_supervisor()
            if existing:
                return 200, {"state": "already-running"}
            if backend is not None:
                raise BridgeError(409, "Backend is responding but its DAXQO supervisor ownership cannot be verified")
            self._launch_supervisor()
            return 202, {"state": "starting"}

    def stop_supervisor(self) -> bool:
        record = self._owned_supervisor()
        if not record:
            return False
        pid = int(record["pid"])
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if not self._process_matches(record):
                self.supervisor_file.unlink(missing_ok=True)
                return True
            time.sleep(.1)
        if self._process_matches(record):
            os.kill(pid, signal.SIGKILL)
        self.supervisor_file.unlink(missing_ok=True)
        return True

    def restart(self) -> tuple[int, dict[str, Any]]:
        with self._lock:
            backend = self.backend_probe()
            active = self._active_task(backend)
            if active is None:
                raise BridgeError(503, "Cannot verify whether a task is active")
            if active:
                raise BridgeError(409, "Cannot restart while a task is active")
            if backend is not None and not self._owned_supervisor():
                raise BridgeError(409, "Backend is responding but supervisor ownership cannot be verified")
            if self._active_task(self.backend_probe()) is not False:
                raise BridgeError(409, "Cannot safely restart until task activity is confirmed clear")
            self.stop_supervisor()
            self._launch_supervisor()
            return 202, {"state": "starting"}

    def _validate_update(self, body: dict[str, Any]) -> tuple[str, dict[str, bytes]]:
        if body.get("version") != APP_VERSION:
            raise BridgeError(400, "Unsupported update version")
        files = body.get("files")
        if not isinstance(files, dict) or not files or len(files) > 256:
            raise BridgeError(400, "Update files must be a non-empty object")
        stage = self.stage
        try:
            if stage.is_symlink() or not stage.is_dir():
                raise BridgeError(400, "Stage directory is missing or unsafe")
            stage = stage.resolve(strict=True)
        except OSError as exc:
            raise BridgeError(400, "Stage directory is missing") from exc
        payload: dict[str, bytes] = {}
        total = 0
        for raw_path, digest in files.items():
            relative = _safe_relative(raw_path)
            if not isinstance(digest, str) or len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
                raise BridgeError(400, "Invalid SHA-256 digest")
            source = _check_components(stage, relative)
            if not source.is_file():
                raise BridgeError(400, "Staged file does not exist")
            content = _read_regular(source, MAX_FILE)
            total += len(content)
            if total > MAX_UPDATE:
                raise BridgeError(413, "Update exceeds total size limit")
            if not secrets.compare_digest(hashlib.sha256(content).hexdigest(), digest):
                raise BridgeError(400, "SHA-256 verification failed")
            payload[relative] = content
        return APP_VERSION, payload

    def submit_update(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        version, payload = self._validate_update(body)
        with self._lock:
            if self._update_thread and self._update_thread.is_alive():
                raise BridgeError(409, "An update is already in progress")
            backend = self.backend_probe()
            active = self._active_task(backend)
            if active is None:
                raise BridgeError(503, "Cannot verify whether a task is active")
            if active:
                raise BridgeError(409, "Cannot update while a task is active")
            self._payload_dir = Path(tempfile.mkdtemp(prefix=".runtime-update-", dir=self.state_dir))
            os.chmod(self._payload_dir, 0o700)
            for relative, content in payload.items():
                cached = self._payload_dir.joinpath(*PurePosixPath(relative).parts)
                cached.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                cached.write_bytes(content)
                os.chmod(cached, 0o600)
            self._update = {"state": "queued", "version": version}
            self._update_thread = threading.Thread(target=self._apply_update,
                args=(version, payload.keys()), name="daxqo-update", daemon=True)
            self._update_thread.start()
        return 202, {"state": "queued", "version": version}

    def _backup_database(self, backup: Path) -> None:
        db = self.state_dir / "harness.sqlite3"
        if not db.exists():
            return
        if db.is_symlink() or not db.is_file():
            raise BridgeError(403, "Unsafe harness database")
        if self.backup_root.is_symlink():
            raise BridgeError(403, "Unsafe backup directory")
        backup.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.backup_root, 0o700)
        os.chmod(backup, 0o700)
        destination = backup / "harness.sqlite3"
        source_db = sqlite3.connect(db.resolve(strict=True).as_uri() + "?mode=ro", uri=True, timeout=5)
        target_db = sqlite3.connect(destination)
        try:
            source_db.backup(target_db)
        finally:
            target_db.close()
            source_db.close()
        os.chmod(destination, 0o600)

    def _write_test_report(self, exit_code: int, output: str) -> dict[str, Any]:
        output = output[-12000:]
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.state_dir.is_symlink():
            raise BridgeError(403, "Unsafe state directory")
        report_path = self.state_dir / "update-tests.log"
        if report_path.is_symlink():
            raise BridgeError(403, "Unsafe update test report")
        temp = self.state_dir / (".update-tests." + secrets.token_hex(8) + ".tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(temp, flags, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", errors="replace") as stream:
                stream.write(output)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temp, 0o600)
            if report_path.is_symlink():
                raise BridgeError(403, "Unsafe update test report")
            os.replace(temp, report_path)
        except Exception:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        return {"exitCode": int(exit_code), "summary": output[-2000:]}

    def _run_tests(self, cwd: Path) -> dict[str, Any]:
        if self.tests_runner:
            result = self.tests_runner(cwd)
            if isinstance(result, tuple):
                return self._write_test_report(int(result[0]), str(result[1]))
            return self._write_test_report(int(result), "")
        try:
            result = subprocess.run([sys.executable, "-m", "unittest", "test_server", "test_supervisor",
                                     "test_runtime_control", "test_task_services"], cwd=str(cwd), stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                                    timeout=600, check=False)
            output = "[stdout]\n" + (result.stdout or "") + "\n[stderr]\n" + (result.stderr or "")
            return self._write_test_report(int(result.returncode), output)
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout or ""
            stderr = exc.stderr or ""
            if isinstance(stdout, bytes):
                stdout = stdout.decode("utf-8", "replace")
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", "replace")
            output = "[stdout]\n" + stdout + "\n[stderr]\n" + stderr + "\nTest process timed out after 600 seconds."
            return self._write_test_report(124, output)

    def _wait_backend_ready(self, timeout: float = 45.0) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            backend = self.backend_probe()
            app = backend.get("app") if isinstance(backend, dict) else None
            engine = backend.get("engine") if isinstance(backend, dict) else None
            if (isinstance(app, dict) and app.get("name") == "DAXQO"
                    and app.get("version") == APP_VERSION and isinstance(engine, dict)
                    and engine.get("connected") is True):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(min(.5, max(0.0, deadline - time.monotonic())))

    def _apply_update(self, version: str, relative_paths: Any) -> None:
        backup = self.backup_root / (version + "-" + time.strftime("%Y%m%d-%H%M%S")
                                     + f"-{os.getpid()}-{secrets.token_hex(3)}")
        originals: dict[str, tuple[bool, bytes, int]] = {}
        supervisor_stopped = False
        test_report: dict[str, Any] | None = None
        try:
            with self._lock:
                active = self._active_task(self.backend_probe())
                if active is None:
                    raise BridgeError(503, "Cannot verify whether a task is active")
                if active:
                    raise BridgeError(409, "Cannot update while a task is active")
                self._backup_database(backup)
                for relative in relative_paths:
                    target = _check_components(self.app, relative, allow_missing_parents=True)
                    existed = target.exists()
                    data = b""
                    mode = 0o644
                    if existed:
                        info = target.lstat()
                        data = _read_regular(target, MAX_UPDATE * 4)
                        mode = stat.S_IMODE(info.st_mode)
                        saved = backup.joinpath(*PurePosixPath(relative).parts)
                        saved.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                        saved.write_bytes(data)
                        os.chmod(saved, 0o600)
                    originals[relative] = (existed, data, mode)
                self._update = {"state": "applying", "version": version}
            assert self._payload_dir is not None
            for relative in relative_paths:
                cached = self._payload_dir.joinpath(*PurePosixPath(relative).parts)
                content = cached.read_bytes()  # Stage bytes are never read after digest validation.
                mode = originals[relative][2] if originals[relative][0] else 0o644
                _atomic_write(self.app, relative, content, mode)
            test_report = self._run_tests(self.app)
            with self._lock:
                self._update = {"state": "testing", "version": version, "tests": test_report}
            if test_report["exitCode"] != 0:
                raise BridgeError(500, "Update tests failed; previous source files were restored")
            active = self._active_task(self.backend_probe())
            if active is not False:
                raise BridgeError(409, "Task activity changed during update; previous source files were restored")
            with self._lock:
                self._update = {"state": "restarting", "version": version, "tests": test_report}
            supervisor_stopped = self.stop_supervisor()
            self._launch_supervisor()
            if not self._wait_backend_ready(45.0):
                # Stop only the verified DAXQO supervisor we just launched before
                # restoring sources. The existing rollback path then starts the old version.
                self.stop_supervisor()
                raise BridgeError(503, "Backend did not become ready after update restart")
            with self._lock:
                self._update = {"state": "completed", "version": version, "tests": test_report}
        except Exception as exc:
            rollback_ok = True
            for relative, (existed, data, mode) in reversed(list(originals.items())):
                try:
                    target = _check_components(self.app, relative, allow_missing_parents=True)
                    if existed:
                        _atomic_write(self.app, relative, data, mode)
                    elif target.exists():
                        target.unlink()
                except Exception:
                    rollback_ok = False
            # If a post-test restart failed, restore and bring the previous code back up.
            try:
                if supervisor_stopped and not self._owned_supervisor():
                    self._launch_supervisor()
            except Exception:
                rollback_ok = False
            message = str(exc) if isinstance(exc, BridgeError) else "Update failed"
            if not rollback_ok:
                message = "Update failed and source rollback needs attention"
            with self._lock:
                failed = {"state": "failed", "version": version, "error": message[:240]}
                if test_report is not None:
                    failed["tests"] = test_report
                self._update = failed
        finally:
            if self._payload_dir:
                shutil.rmtree(self._payload_dir, ignore_errors=True)
                self._payload_dir = None

    def status(self) -> dict[str, Any]:
        backend = self.backend_probe()
        owned = self._owned_supervisor()
        if backend:
            state = "running"
            backend_info: dict[str, Any] = {"connected": True}
            pid = backend.get("service", {}).get("pid")
            if isinstance(pid, int):
                backend_info["pid"] = pid
        else:
            backend_info = {"connected": False}
            if owned:
                state = "starting" if time.monotonic() - self._started_at < 20 else "degraded"
            else:
                state = "stopped"
        return {"state": state, "backend": backend_info,
                "app": {"name": "DAXQO", "version": APP_VERSION}, "update": dict(self._update)}

    def serve(self) -> None:
        self.acquire_lock()
        server = ThreadingHTTPServer(("127.0.0.1", self.port), create_handler(self))
        server.daemon_threads = True
        try:
            server.serve_forever(poll_interval=.25)
        finally:
            server.server_close()
            if self._lock_stream:
                self._lock_stream.close()
                self._lock_stream = None


def create_handler(manager: RuntimeManager):
    class Handler(BaseHTTPRequestHandler):
        server_version = "DAXQORuntime/0.2"
        def log_message(self, _fmt: str, *_args: Any) -> None:
            return

        def _json(self, status: int, data: dict[str, Any]) -> None:
            raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            origin = self.headers.get("Origin")
            if origin in (f"http://127.0.0.1:{self.server.server_port}",
                          f"http://127.0.0.1:{manager.backend_port}"):
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
            self.end_headers()
            self.wfile.write(raw)

        def _guard(self) -> bool:
            expected_host = f"127.0.0.1:{self.server.server_port}"
            if self.headers.get("Host", "") != expected_host:
                self._json(403, {"error": "Invalid host"})
                return False
            origin = self.headers.get("Origin")
            allowed = {f"http://{expected_host}", f"http://127.0.0.1:{manager.backend_port}"}
            if origin and origin not in allowed:
                self._json(403, {"error": "Cross-origin request rejected"})
                return False
            token = manager.capability()
            if len(token) < 32 or not secrets.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
                self._json(401, {"error": "Authentication required"})
                return False
            return True

        def _body(self) -> dict[str, Any]:
            transfer = self.headers.get("Transfer-Encoding", "").lower()
            if transfer:
                raise BridgeError(400, "Transfer-Encoding is not supported")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise BridgeError(400, "Invalid Content-Length") from exc
            if length < 0 or length > MAX_REQUEST:
                raise BridgeError(413, "Request body exceeds size limit")
            raw = self.rfile.read(length)
            try:
                value = json.loads(raw or b"{}")
            except (ValueError, UnicodeError) as exc:
                raise BridgeError(400, "Invalid JSON body") from exc
            if not isinstance(value, dict):
                raise BridgeError(400, "JSON object required")
            return value

        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            if path == "/healthz":
                if self.headers.get("Host", "") != f"127.0.0.1:{self.server.server_port}":
                    return self._json(403, {"error": "Invalid host"})
                return self._json(200, {"ok": True})
            if path != "/api/runtime/status":
                return self._json(404, {"error": "Not found"})
            if not self._guard():
                return
            return self._json(200, manager.status())

        def do_POST(self) -> None:
            if not self._guard():
                return
            path = urlsplit(self.path).path
            try:
                body = self._body()
                if path in ("/api/runtime/start", "/api/runtime/restart"):
                    if body:
                        raise BridgeError(400, "Empty JSON object required")
                    status, data = manager.start() if path.endswith("/start") else manager.restart()
                    return self._json(status, data)
                if path == "/api/runtime/update":
                    status, data = manager.submit_update(body)
                    return self._json(status, data)
                return self._json(404, {"error": "Not found"})
            except BridgeError as exc:
                return self._json(exc.status, {"error": str(exc)})
            except (OSError, RuntimeError) as exc:
                return self._json(503, {"error": str(exc)[:240]})

        def do_OPTIONS(self) -> None:
            expected_host = f"127.0.0.1:{self.server.server_port}"
            origin = self.headers.get("Origin", "")
            allowed_origins = {f"http://{expected_host}", f"http://127.0.0.1:{manager.backend_port}"}
            method = self.headers.get("Access-Control-Request-Method", "")
            requested_headers = {item.strip().lower() for item in
                                 self.headers.get("Access-Control-Request-Headers", "").split(",") if item.strip()}
            if (self.headers.get("Host", "") != expected_host or origin not in allowed_origins
                    or method not in ("GET", "POST")
                    or not requested_headers.issubset({"authorization", "content-type"})):
                return self._json(403, {"error": "Preflight request rejected"})
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
            self.send_header("Access-Control-Max-Age", "300")
            self.send_header("Vary", "Origin")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_PUT(self) -> None:
            self._json(405, {"error": "Method not allowed"})

        def do_DELETE(self) -> None:
            self._json(405, {"error": "Method not allowed"})

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--stage", type=Path, required=True)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--backend-port", type=int, default=DEFAULT_BACKEND_PORT)
    args = parser.parse_args(argv)
    if args.port != DEFAULT_PORT or args.backend_port != DEFAULT_BACKEND_PORT:
        parser.error("Runtime bridge ports are fixed at 18882 and 18880")
    RuntimeManager(args.app, args.stage, args.port, args.backend_port).serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
