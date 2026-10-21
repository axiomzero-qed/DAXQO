#!/usr/bin/env python3
"""Small Linux process supervisor for the phone-harness HTTP server."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
from collections import deque
from typing import Callable

APP_DEFAULT = Path("/root/phone-harness")
RUNTIME_CACHE = Path("/tmp/phone-harness-runtime")
VALID_SANDBOXES = ("read-only", "workspace-write", "danger-full-access")


def _no_symlink_path(path: Path) -> None:
    """Reject symlinks in every existing component of an application path."""
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            if current.is_symlink():
                raise RuntimeError(f"Symlink is not allowed in runtime path: {current}")
        except OSError as exc:
            raise RuntimeError(f"Cannot inspect runtime path: {current}") from exc


def _owned_directory(path: Path, *, create: bool = False) -> Path:
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    _no_symlink_path(path)
    st = path.stat()
    if not path.is_dir() or st.st_uid != os.getuid():
        raise RuntimeError(f"Runtime directory is not owned by this user: {path}")
    path.chmod(0o700)
    return path


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def resolve_runtime(app: Path, probe: Callable[[Path], bool] | None = None) -> Path:
    """Resolve runtime.json's binary; recover a missing /tmp copy from app/runtime."""
    app = Path(os.path.abspath(app))
    _no_symlink_path(app)
    app = app.resolve(strict=True)
    runtime_file = app / "runtime.json"
    _no_symlink_path(runtime_file)
    if runtime_file.is_symlink() or not runtime_file.is_file():
        raise RuntimeError("runtime.json is missing or unsafe")
    try:
        data = json.loads(runtime_file.read_text(encoding="utf-8"))
        binary = Path(data["binary"])
        persistent = Path(data["persistent_runtime"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RuntimeError("runtime.json has invalid runtime paths") from exc
    if not binary.is_absolute() or not persistent.is_absolute():
        raise RuntimeError("runtime paths must be absolute")
    _no_symlink_path(app / "runtime")
    app_runtime = (app / "runtime").resolve(strict=True)
    _no_symlink_path(persistent)
    persistent_real = persistent.resolve(strict=True)
    if not _inside(persistent_real, app_runtime) or persistent_real == app_runtime:
        raise RuntimeError("Persistent runtime must be inside this application's runtime directory")
    _no_symlink_path(app_runtime)
    _no_symlink_path(persistent_real)
    if persistent_real.stat().st_uid != os.getuid():
        raise RuntimeError("Persistent runtime is not owned by this user")

    if probe is None:
        def probe(path: Path) -> bool:
            try:
                result = subprocess.run([str(path), "--version"], stdin=subprocess.DEVNULL,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                        timeout=8, check=False)
                return result.returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                return False

    binary = Path(os.path.abspath(binary))
    if not _inside(binary, app) and not _inside(binary, RUNTIME_CACHE):
        raise RuntimeError("Configured binary is outside the application runtime and its approved cache")
    _no_symlink_path(binary)
    if binary.is_file() and os.access(binary, os.X_OK) and probe(binary):
        return binary

    # Recovery is limited to a runtime folder named for the configured version and
    # the single app-owned cache directory. Copy only regular files, never links.
    version_dir = persistent_real
    if version_dir.name != binary.parent.parent.name and binary.parent.name == "bin":
        raise RuntimeError("Persistent runtime version does not match runtime.json binary")
    codex_rel = Path("bin/codex")
    source_binary = version_dir / codex_rel
    _no_symlink_path(source_binary)
    if not source_binary.is_file() or not os.access(source_binary, os.X_OK):
        raise RuntimeError("Persistent Codex executable is missing")
    cache = _owned_directory(RUNTIME_CACHE, create=True)
    cache_runtime = cache / version_dir.name
    _no_symlink_path(cache_runtime)
    if not cache_runtime.exists():
        cache_runtime.mkdir(mode=0o700)
    if cache_runtime.is_symlink() or cache_runtime.stat().st_uid != os.getuid() or not cache_runtime.is_dir():
        raise RuntimeError("Cached runtime is not owned by this user")
    for root, dirs, files in os.walk(version_dir, followlinks=False):
        root_path = Path(root)
        for name in dirs:
            item = root_path / name
            if item.is_symlink():
                raise RuntimeError("Persistent runtime contains a symlink")
            if item.stat().st_uid != os.getuid():
                raise RuntimeError("Persistent runtime directory is not owned by this user")
        for name in files:
            src = root_path / name
            if src.is_symlink() or not src.is_file():
                raise RuntimeError("Persistent runtime contains an unsafe file")
            if src.stat().st_uid != os.getuid():
                raise RuntimeError("Persistent runtime file is not owned by this user")
            relative = src.relative_to(version_dir)
            dst = cache_runtime / relative
            _no_symlink_path(dst)
            dst.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            dst.chmod(0o755 if os.access(src, os.X_OK) else 0o644)
    recovered = cache_runtime / codex_rel
    if not probe(recovered):
        raise RuntimeError("Recovered Codex executable failed its version probe")
    return recovered


class Supervisor:
    def __init__(self, app: Path = APP_DEFAULT, sandbox: str = "workspace-write", *,
                 poll_interval: float = 0.75, max_restarts: int = 5,
                 restart_window: float = 60.0, backoff: float = 1.0,
                 popen: Callable = subprocess.Popen, clock: Callable[[], float] = time.monotonic,
                 sleeper: Callable[[float], None] = time.sleep,
                 binary_resolver: Callable[[Path], Path] = resolve_runtime):
        if sandbox not in VALID_SANDBOXES:
            raise ValueError("Invalid sandbox mode")
        self.app = Path(app).resolve()
        self.sandbox = sandbox
        self.poll_interval = poll_interval
        self.max_restarts = max_restarts
        self.restart_window = restart_window
        self.backoff = backoff
        self.popen = popen
        self.clock = clock
        self.sleeper = sleeper
        self.binary_resolver = binary_resolver
        self.stopping = False
        self.child: subprocess.Popen | None = None
        self._old_handlers: dict[int, object] = {}
        self._lock_stream = None
        self._pid_path: Path | None = None
        self._log_stream = None

    @property
    def state(self) -> Path:
        return self.app / "state"

    def _signal(self, _signum: int, _frame: object) -> None:
        self.stopping = True
        if self.child is not None and self.child.poll() is None:
            try:
                self.child.terminate()
            except OSError:
                pass

    def _acquire_lock(self) -> None:
        try:
            import fcntl
        except ImportError as exc:
            raise RuntimeError("The phone-harness supervisor requires Linux fcntl locks") from exc
        lock_path = self.state / "supervisor.lock"
        self._lock_stream = lock_path.open("a+")
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(self._lock_stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._lock_stream.close()
            self._lock_stream = None
            raise RuntimeError("Another phone-harness supervisor is already running") from exc

    def _prepare(self) -> None:
        _owned_directory(self.state, create=True)
        self._acquire_lock()
        self._pid_path = self.state / "supervisor.pid"
        self._pid_path.write_text(f"{os.getpid()}\n", encoding="ascii")
        os.chmod(self._pid_path, 0o600)
        log_path = self.state / "supervisor.log"
        self._log_stream = log_path.open("ab", buffering=0)
        os.chmod(log_path, 0o600)
        for signum in (signal.SIGTERM, signal.SIGINT):
            self._old_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, self._signal)

    def command(self, binary: Path) -> list[str]:
        return [sys.executable, str(self.app / "server.py"), "--host", "127.0.0.1",
                "--port", "18880", "--codex", str(binary), "--state", str(self.state),
                "--workspace", str(self.app / "workspace"), "--sandbox", self.sandbox]

    def _spawn(self, binary: Path) -> subprocess.Popen:
        assert self._log_stream is not None
        self.child = self.popen(self.command(binary), cwd=str(self.app), stdin=subprocess.DEVNULL,
                                stdout=self._log_stream, stderr=self._log_stream,
                                close_fds=True)
        return self.child

    def run(self) -> int:
        self._prepare()
        crashes: deque[float] = deque()
        try:
            binary = self.binary_resolver(self.app)
            while not self.stopping:
                child = self._spawn(binary)
                while not self.stopping and child.poll() is None:
                    self.sleeper(self.poll_interval)
                if self.stopping:
                    self._stop_child()
                    return 0
                # Child exited. Keep a bounded rolling crash budget and back off.
                now = self.clock()
                crashes.append(now)
                while crashes and now - crashes[0] > self.restart_window:
                    crashes.popleft()
                if len(crashes) > self.max_restarts:
                    return 1
                delay = min(self.backoff * (2 ** (len(crashes) - 1)), 30.0)
                deadline = self.clock() + delay
                while not self.stopping and self.clock() < deadline:
                    self.sleeper(min(self.poll_interval, max(0.0, deadline - self.clock())))
            self._stop_child()
            return 0
        finally:
            self._stop_child()
            self._cleanup()

    def _stop_child(self) -> None:
        child = self.child
        if child is None or child.poll() is not None:
            return
        try:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=2)
        except OSError:
            pass

    def _cleanup(self) -> None:
        for signum, handler in self._old_handlers.items():
            try:
                signal.signal(signum, handler)
            except (ValueError, OSError):
                pass
        if self._pid_path:
            try:
                if self._pid_path.read_text(encoding="ascii").strip() == str(os.getpid()):
                    self._pid_path.unlink()
            except OSError:
                pass
        if self._log_stream:
            self._log_stream.close()
            self._log_stream = None
        if self._lock_stream:
            self._lock_stream.close()
            self._lock_stream = None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, default=APP_DEFAULT)
    parser.add_argument("--sandbox", choices=VALID_SANDBOXES, default="workspace-write")
    args = parser.parse_args(argv)
    try:
        return Supervisor(args.app, args.sandbox).run()
    except Exception as exc:
        print(f"phone-harness supervisor: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
