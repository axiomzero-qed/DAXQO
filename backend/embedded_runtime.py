#!/usr/bin/env python3
"""Start the APK's already-installed Linux runtime without package downloads."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import signal
import stat
import sys
import threading
from typing import Any

from runtime_control import APP_VERSION, RuntimeManager, _atomic_write
from supervisor import _no_symlink_path

RUNTIME_VERSION = "0.163.0"


def prepare_runtime(app: Path, workspace: Path) -> None:
    """Materialize device-specific paths only; bundled packages stay untouched."""
    app, workspace = Path(os.path.abspath(app)), Path(os.path.abspath(workspace))
    _no_symlink_path(app)
    required = ("server.py", "supervisor.py", "runtime_control.py", "run_motor_mcp.py",
                "motor/mcp_server.py", "motor/task_guard.py", "trajectory/collector.py",
                f"runtime/codex-{RUNTIME_VERSION}/bin/codex")
    for relative in required:
        file = app / relative
        _no_symlink_path(file)
        if not file.is_file():
            raise RuntimeError("A required bundled runtime component is missing")
    for directory in (app / "state", app / "update-stage", workspace):
        _no_symlink_path(directory)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not directory.is_dir() or (hasattr(os, "getuid") and directory.stat().st_uid != os.getuid()):
            raise RuntimeError("Runtime data directory is not owned by this app")
        directory.chmod(0o700)
    capability = app / "state" / "capability"
    _no_symlink_path(capability)
    if not capability.is_file() or not stat.S_ISREG(capability.stat().st_mode):
        raise RuntimeError("Android has not supplied this installation's local credentials")
    if hasattr(os, "getuid") and capability.stat().st_uid != os.getuid():
        raise RuntimeError("Local credentials are not owned by this app")
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,512}", capability.read_text(encoding="ascii").strip()):
        raise RuntimeError("Android local credentials are invalid")
    capability.chmod(0o600)
    runtime = app / "runtime" / ("codex-" + RUNTIME_VERSION)
    metadata = {"version": RUNTIME_VERSION, "binary": str(runtime / "bin" / "codex"),
                "persistent_runtime": str(runtime)}
    _atomic_write(app, "runtime.json", (json.dumps(metadata, indent=2) + "\n").encode(), 0o600)


class TrajectoryReceiver:
    """One managed ACK receiver; recording consent remains in the native app."""

    def __init__(self, manager: RuntimeManager, *, interval: float = 2.0):
        self.manager = manager
        self.interval = interval
        self.stopping = threading.Event()
        self.thread: threading.Thread | None = None
        self.last_error = ""

    def start(self) -> None:
        if self.thread is not None and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._run, name="embedded-trajectory-receiver", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        # Reuse the public receiver protocol: durable ingest/checkpoint precedes
        # ACK. Motor's --trajectory-feedback emits metadata and never ACKs.
        try:
            from motor.transport import BridgeTransport
            from trajectory import Dataset
            from trajectory.collector import sync_device
            import fcntl
        except ImportError:
            self.last_error = "bundled_collector_unavailable"
            return
        stop = self.stopping

        class ReceiverClient(BridgeTransport):
            def request(self, path, body=None, timeout=2):
                if stop.is_set():
                    raise RuntimeError("receiver_stopping")
                return super().request(path, body, timeout=min(timeout, 2))
        root = self.manager.state_dir / "trajectory"
        _no_symlink_path(root)
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock = root / "receiver.lock"
        _no_symlink_path(lock)
        dataset = None
        client = None
        try:
            with lock.open("a+b") as ownership:
                lock.chmod(0o600)
                try:
                    fcntl.flock(ownership.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    self.last_error = "receiver_already_running"
                    return
                client = ReceiverClient(18883, self.manager.capability())
                while not self.stopping.is_set():
                    try:
                        status = client.request("/api/trajectory/status", timeout=2)
                        if int(status.get("pending", 0)) > 0:
                            if dataset is None:
                                dataset = Dataset(root / "dataset")
                            sync_device(client, dataset, max_batches=8)
                        self.last_error = ""
                    except Exception as exc:
                        self.last_error = type(exc).__name__
                    self.stopping.wait(self.interval)
        except Exception as exc:
            self.last_error = type(exc).__name__
        finally:
            if client is not None:
                client.close()
            if dataset is not None:
                dataset.close()

    def close(self) -> None:
        self.stopping.set()
        if self.thread is not None:
            self.thread.join(timeout=6)


def run(app: Path, workspace: Path) -> int:
    if os.environ.get("DAXQO_EMBEDDED") != "1":
        raise RuntimeError("This entry point is reserved for the APK's embedded environment")
    app, workspace = Path(app).absolute(), Path(workspace).absolute()
    prepare_runtime(app, workspace)
    os.environ["DAXQO_WORKSPACE"] = str(workspace)
    os.environ["DAXQO_STAGE"] = str(app / "update-stage")
    manager = RuntimeManager(app, app / "update-stage")
    if not manager.capability():
        raise RuntimeError("Local credentials are unavailable")
    receiver = TrajectoryReceiver(manager)
    stopping = threading.Event()
    previous = {}
    launched = False

    def request_stop(_signum: int, _frame: Any) -> None:
        stopping.set()

    def startup() -> None:
        nonlocal launched
        manager.start()
        launched = True
        receiver.start()
        print(f"DAXQO {APP_VERSION}: bundled runtime started", flush=True)

    def shutdown() -> None:
        stopping.set()
        receiver.close()
        if launched:
            manager.stop_supervisor()

    try:
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, request_stop)
        manager.serve(startup=startup, shutdown=shutdown, stop_event=stopping)
        return 0
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--workspace", type=Path)
    args = parser.parse_args(argv)
    try:
        return run(args.app, args.workspace or args.app / "workspace")
    except Exception as exc:
        # File paths can contain user data. Android receives only the phase/type.
        print(f"DAXQO embedded startup failed: {type(exc).__name__}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
