#!/usr/bin/env python3
"""Foreground service owner and secret-free environment checks for Termux/PRoot."""
from __future__ import annotations

import argparse
import importlib
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import socket
import sqlite3
import ssl
import subprocess
import sys
import threading
import time
from http.server import ThreadingHTTPServer


def doctor(app: Path) -> int:
    checks = {"python": sys.version_info >= (3, 10),
              "architecture": platform.machine().lower() in ("aarch64", "arm64", "x86_64", "amd64"),
              "tls": bool(ssl.create_default_context().get_ca_certs()),
              "sqlite": bool(sqlite3.connect(":memory:").execute("select 1").fetchone())}
    for tool in ("bash", "git", "rg", "node", "npm", "ps"):
        checks[tool] = shutil.which(tool) is not None
    try:
        checks["network_dns"] = bool(socket.getaddrinfo("api.deepseek.com", 443))
    except OSError:
        checks["network_dns"] = False
    sys.path.insert(0, str(app / "runtime" / "mail-libs"))
    for module in ("dkim", "dns.resolver"):
        try:
            importlib.import_module(module)
            checks[module] = True
        except ImportError:
            checks[module] = False
    try:
        from runtime_control import _proc_info
        info = _proc_info(os.getpid())
        checks["proc_identity"] = bool(info and info["uid"] == os.getuid())
    except (ImportError, OSError):
        checks["proc_identity"] = False
    try:
        from install_linux import _probe_codex
        binary = Path(json.loads((app / "runtime.json").read_text())["binary"])
        _probe_codex(binary)
        checks["codex_0.163.0"] = True
    except (OSError, ValueError, KeyError, RuntimeError):
        checks["codex_0.163.0"] = False
    print(json.dumps({"platform": "DAXQO for XM", "checks": checks, "ok": all(checks.values())}, ensure_ascii=False))
    return 0 if all(checks.values()) else 1


def status(app: Path) -> int:
    from runtime_control import RuntimeManager
    manager = RuntimeManager(app, app / "update-stage")
    backend = manager.backend_probe()
    report = {"platform": "DAXQO for XM", "backend": backend is not None,
              "supervisor_verified": bool(manager._owned_supervisor()),
              "engine": backend.get("engine") if backend else None,
              "model_configured": bool(backend and backend.get("provider", {}).get("configured"))}
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["backend"] and report["supervisor_verified"] and report["engine"].get("connected") else 1


def run(app: Path) -> int:
    from runtime_control import RuntimeManager, create_handler
    os.environ["DAXQO_STAGE"] = str(app / "update-stage")
    manager = RuntimeManager(app, app / "update-stage")
    manager.acquire_lock()
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    http = ThreadingHTTPServer(("127.0.0.1", 18882), create_handler(manager))
    http.daemon_threads = True
    worker = threading.Thread(target=http.serve_forever, daemon=True)
    worker.start()
    try:
        manager.start()
        deadline = time.monotonic() + 90
        while not stop.is_set() and time.monotonic() < deadline:
            if manager.backend_probe():
                print("DAXQO for XM ready on this phone (18880/18882). Keep this Termux session open.", flush=True)
                break
            stop.wait(1)
        else:
            if not stop.is_set():
                raise RuntimeError("Backend did not become ready in 90 seconds; check private state/supervisor.log")
        while not stop.wait(1):
            if not worker.is_alive():
                raise RuntimeError("Runtime control stopped")
        return 0
    finally:
        http.shutdown()
        http.server_close()
        manager.stop_supervisor()
        if manager._lock_stream:
            manager._lock_stream.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("action", choices=("start", "doctor", "status"))
    args = parser.parse_args()
    app = args.app.expanduser().resolve()
    sys.path.insert(0, str(app))
    try:
        if args.action == "doctor":
            return doctor(app)
        return status(app) if args.action == "status" else run(app)
    except (OSError, RuntimeError) as exc:
        print(f"XM runtime: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
