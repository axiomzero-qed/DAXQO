#!/usr/bin/env python3
"""One-shot, identity-checked refresh of the installed DAXQO runtime bridge."""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import signal
import stat
import subprocess
import sys
import tempfile
import time

APP = Path("/root/phone-harness")
STAGE = Path("/sdcard/Download/daxqo-stage")
BACKEND_PORT = 18880
RUNTIME_PORT = 18882
MAX_SOURCE = 2 * 1024 * 1024
MANAGER_STAGE_ARG = "/sdcard/Download/daxqo-stage/backend"


class RefreshError(RuntimeError):
    pass


def checked_regular(path: Path, *, limit: int | None = None) -> os.stat_result:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RefreshError("Expected a regular non-link file")
    if limit is not None and info.st_size > limit:
        raise RefreshError("File exceeds size limit")
    return info


def read_regular(path: Path, limit: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise RefreshError("Expected a bounded regular file")
        data = stream.read(limit + 1)
        if len(data) > limit:
            raise RefreshError("File exceeds size limit")
        return data


def safe_dir(path: Path) -> Path:
    if path.is_symlink() or not path.is_dir():
        raise RefreshError("Expected a safe directory")
    resolved = path.resolve(strict=True)
    current = Path(resolved.anchor)
    for part in resolved.parts[1:]:
        current /= part
        if current.is_symlink():
            raise RefreshError("Symlink directory component rejected")
    return resolved


def atomic_write(path: Path, data: bytes, mode: int) -> None:
    if path.exists() or path.is_symlink():
        checked_regular(path)
    fd, temp_name = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=str(path.parent))
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp, mode)
        if path.is_symlink():
            raise RefreshError("Symlink target rejected")
        os.replace(temp, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def write_result(stage: Path, phase: str, *, pids: dict[str, int] | None = None,
                 error: str | None = None) -> None:
    result = {"phase": phase, "pids": pids or {}}
    if error:
        result["error"] = error[:240]
    atomic_write(stage / "refresh-runtime-result.json",
                 (json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"), 0o600)


def proc_info(pid: int) -> dict | None:
    base = Path("/proc") / str(pid)
    try:
        raw = (base / "stat").read_text(encoding="ascii")
        tail = raw[raw.rfind(")") + 2:].split()
        start_time = int(tail[19])
        status = (base / "status").read_text(encoding="ascii")
        uid = int(next(line for line in status.splitlines() if line.startswith("Uid:")).split()[1])
        argv = [item.decode("utf-8", "strict") for item in (base / "cmdline").read_bytes().split(b"\0") if item]
        return {"pid": pid, "uid": uid, "startTime": start_time, "args": argv}
    except (OSError, ValueError, StopIteration, UnicodeError, IndexError):
        return None


def same_process(identity: dict) -> bool:
    now = proc_info(identity["pid"])
    return bool(now and now["uid"] == identity["uid"] and now["startTime"] == identity["startTime"]
                and now["args"] == identity["args"])


def find_manager(app: Path, stage: Path) -> dict:
    expected_script = str((app / "runtime_control.py").resolve(strict=True))
    expected_app = str(app.resolve(strict=True))
    expected_stage = MANAGER_STAGE_ARG
    matches = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        info = proc_info(int(entry.name))
        if not info or info["uid"] != os.getuid():
            continue
        args = info["args"]
        try:
            script = str(Path(args[1]).resolve(strict=True))
        except (IndexError, OSError):
            continue
        if (script == expected_script and args[2:] == ["--app", expected_app, "--stage", expected_stage,
                "--port", str(RUNTIME_PORT), "--backend-port", str(BACKEND_PORT)]):
            matches.append(info)
    if len(matches) != 1:
        raise RefreshError("Could not identify exactly one owned runtime manager")
    return matches[0]


def backend_identity(app: Path, status: dict) -> dict:
    service = status.get("service") if isinstance(status, dict) else None
    pid = service.get("pid") if isinstance(service, dict) else None
    if not isinstance(pid, int) or pid <= 1:
        raise RefreshError("Backend status has no valid PID")
    info = proc_info(pid)
    if not info or info["uid"] != os.getuid():
        raise RefreshError("Backend PID identity cannot be verified")
    args = info["args"]
    expected_script = str((app / "server.py").resolve(strict=True))
    expected_state, expected_workspace = str((app / "state").resolve(strict=True)), str((app / "workspace").resolve(strict=True))
    if len(args) != 14 or str(Path(args[1]).resolve(strict=True)) != expected_script:
        raise RefreshError("Backend process script identity does not match")
    if args[2:6] != ["--host", "127.0.0.1", "--port", str(BACKEND_PORT)]:
        raise RefreshError("Backend process bind identity does not match")
    if (args[6] != "--codex" or not args[7] or args[8:12] != ["--state", expected_state,
            "--workspace", expected_workspace] or args[12:] != ["--sandbox", "danger-full-access"]):
        raise RefreshError("Backend process arguments do not match the installed service")
    return info


def http_json(port: int, path: str, token: str, *, timeout: float = 2.0) -> dict:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request("GET", path, headers={"Host": f"127.0.0.1:{port}",
            "Authorization": "Bearer " + token, "Connection": "close"})
        response = conn.getresponse()
        raw = response.read(64 * 1024 + 1)
        if response.status != 200 or len(raw) > 64 * 1024:
            raise RefreshError("Authenticated local status request failed")
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise RefreshError("Local status response is invalid")
        return value
    except RefreshError:
        raise
    except (OSError, ValueError, UnicodeError, http.client.HTTPException) as exc:
        raise RefreshError("Local status endpoint is unavailable") from exc
    finally:
        conn.close()


def runtime_port_open() -> bool:
    conn = http.client.HTTPConnection("127.0.0.1", RUNTIME_PORT, timeout=.3)
    try:
        conn.request("GET", "/healthz", headers={"Host": f"127.0.0.1:{RUNTIME_PORT}", "Connection": "close"})
        response = conn.getresponse()
        response.read(128)
        return True
    except (OSError, http.client.HTTPException):
        return False
    finally:
        conn.close()


def wait_until(predicate, timeout: float, interval: float = .25) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(interval, max(0, deadline - time.monotonic())))


def check_idle(status: dict) -> None:
    if status.get("activeTaskId"):
        raise RefreshError("A task is active; maintenance was not started")


def run(expected_sha256: str) -> None:
    if not sys.platform.startswith("linux") or not Path("/proc").is_dir():
        raise RefreshError("This maintenance script requires Linux")
    app = safe_dir(APP)
    stage = safe_dir(STAGE)
    stage_backend = safe_dir(stage / "backend")
    state = safe_dir(app / "state")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise RefreshError("Invalid expected SHA-256")

    import fcntl
    lock_path = state / ".refresh-runtime.lock"
    if lock_path.is_symlink():
        raise RefreshError("Unsafe maintenance lock")
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    lock_info = os.fstat(lock_fd)
    if not stat.S_ISREG(lock_info.st_mode) or lock_info.st_uid != os.getuid():
        os.close(lock_fd)
        raise RefreshError("Unsafe maintenance lock ownership")
    os.fchmod(lock_fd, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(lock_fd)
        raise RefreshError("Another maintenance refresh is already running") from exc

    pids: dict[str, int] = {}
    target = app / "runtime_control.py"
    backup = state / ("runtime_control.pre-refresh-" + time.strftime("%Y%m%d-%H%M%S") + ".py")
    replaced = False
    backend_signalled = False
    old_mode = 0o644
    try:
        capability_file = state / "capability"
        checked_regular(capability_file, limit=4096)
        cap_info = capability_file.stat()
        if cap_info.st_uid != os.getuid() or stat.S_IMODE(cap_info.st_mode) & 0o077:
            raise RefreshError("Capability file permissions are unsafe")
        capability = read_regular(capability_file, 4096).decode("ascii").strip()
        if len(capability) < 32:
            raise RefreshError("Local capability is unavailable")

        candidate = stage_backend / "runtime_control.py"
        checked_regular(candidate, limit=MAX_SOURCE)
        new_bytes = read_regular(candidate, MAX_SOURCE)
        if not secrets.compare_digest(hashlib.sha256(new_bytes).hexdigest(), expected_sha256):
            raise RefreshError("Staged source SHA-256 does not match")
        old_info = checked_regular(target, limit=MAX_SOURCE)
        old_mode = stat.S_IMODE(old_info.st_mode)
        old_bytes = read_regular(target, MAX_SOURCE)
        if backup.exists() or backup.is_symlink():
            raise RefreshError("Backup path already exists")
        atomic_write(backup, old_bytes, 0o600)

        deadline = time.monotonic() + 180
        while True:
            backend_status = http_json(BACKEND_PORT, "/api/status", capability)
            if not backend_status.get("activeTaskId"):
                break
            write_result(stage, "waiting-for-launch-task", pids={})
            if time.monotonic() >= deadline:
                raise RefreshError("Timed out waiting for the launch task to finish")
            time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
        check_idle(backend_status)
        backend = backend_identity(app, backend_status)
        pids["backend"] = backend["pid"]
        manager = find_manager(app, stage)
        pids["manager"] = manager["pid"]
        runtime_status = http_json(RUNTIME_PORT, "/api/runtime/status", capability)
        if runtime_status.get("app", {}).get("name") != "DAXQO":
            raise RefreshError("Runtime bridge identity does not match")
        # Recheck task state immediately before the code replacement.
        check_idle(http_json(BACKEND_PORT, "/api/status", capability))

        write_result(stage, "backed-up", pids=pids)
        atomic_write(target, new_bytes, old_mode)
        replaced = True
        write_result(stage, "source-replaced", pids=pids)

        if not same_process(manager):
            raise RefreshError("Runtime manager identity changed before maintenance stop")
        os.kill(manager["pid"], signal.SIGTERM)
        def manager_stopped() -> bool:
            return not same_process(manager) and not runtime_port_open()
        if not wait_until(manager_stopped, 12):
            raise RefreshError("Owned runtime manager did not stop cleanly")
        write_result(stage, "manager-stopped", pids=pids)

        current_status = http_json(BACKEND_PORT, "/api/status", capability)
        check_idle(current_status)
        current_backend = backend_identity(app, current_status)
        if current_backend["pid"] != backend["pid"] or current_backend["startTime"] != backend["startTime"]:
            raise RefreshError("Backend identity changed before maintenance restart")
        write_result(stage, "backend-restarting", pids=pids)
        # The harness may reap this worker when the backend stops. Root performs
        # final acceptance with independent HTTP health checks after the restart.
        os.kill(backend["pid"], signal.SIGTERM)
        backend_signalled = True

        def refreshed() -> bool:
            try:
                status = http_json(BACKEND_PORT, "/api/status", capability, timeout=.6)
                if status.get("service", {}).get("pid") == backend["pid"]:
                    return False
                check_idle(status)
                backend_identity(app, status)
                bridge_status = http_json(RUNTIME_PORT, "/api/runtime/status", capability, timeout=.6)
                return bridge_status.get("app") == {"name": "DAXQO", "version": "0.4.0"}
            except (RefreshError, OSError):
                return False

        if not wait_until(refreshed, 75, .5):
            raise RefreshError("New backend and runtime manager did not become ready")
        new_status = http_json(BACKEND_PORT, "/api/status", capability)
        new_backend = backend_identity(app, new_status)
        if new_backend["pid"] == backend["pid"]:
            raise RefreshError("Backend PID did not change after restart")
        pids["backend"] = new_backend["pid"]
        new_manager = find_manager(app, stage)
        if new_manager["pid"] == manager["pid"] and new_manager["startTime"] == manager["startTime"]:
            raise RefreshError("Runtime manager identity did not change after restart")
        pids["manager"] = new_manager["pid"]
        write_result(stage, "completed", pids=pids)
    except Exception as exc:
        if replaced and not backend_signalled:
            try:
                old_bytes = read_regular(backup, MAX_SOURCE)
                atomic_write(target, old_bytes, old_mode)
            except Exception:
                pass
        try:
            write_result(stage, "failed", pids=pids, error=str(exc))
        except Exception:
            pass
        raise
    finally:
        os.close(lock_fd)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--launch", choices=("detach", "worker"), required=True)
    parser.add_argument("--sha256", required=True)
    args = parser.parse_args()
    if args.launch == "detach":
        command = [sys.executable, str(Path(__file__).resolve()), "--launch", "worker", "--sha256", args.sha256]
        app = safe_dir(APP)
        state = safe_dir(app / "state")
        log_path = state / "refresh-runtime.log"
        if log_path.is_symlink():
            raise RefreshError("Unsafe maintenance log")
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        log_fd = os.open(log_path, flags, 0o600)
        try:
            log_info = os.fstat(log_fd)
            if not stat.S_ISREG(log_info.st_mode) or log_info.st_uid != os.getuid():
                raise RefreshError("Unsafe maintenance log ownership")
            os.fchmod(log_fd, 0o600)
            with os.fdopen(log_fd, "ab", buffering=0) as log:
                log_fd = -1
                child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log,
                                         stderr=subprocess.STDOUT, close_fds=True, start_new_session=True)
        finally:
            if log_fd >= 0:
                os.close(log_fd)
        print(json.dumps({"phase": "launched", "pid": child.pid}, separators=(",", ":")))
        return 0
    try:
        run(args.sha256)
        print(json.dumps({"phase": "completed"}, separators=(",", ":")))
        return 0
    except Exception as exc:
        try:
            write_result(safe_dir(STAGE), "failed", error=str(exc) or "Maintenance worker failed")
        except Exception:
            pass
        print(json.dumps({"phase": "failed", "error": str(exc)[:240]}, separators=(",", ":")), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
