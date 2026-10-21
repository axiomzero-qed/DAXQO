"""Lifecycle tests use a tiny Python child, never a fake Codex engine."""
from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import supervisor


LINUX = sys.platform.startswith("linux")


@unittest.skipUnless(LINUX, "Supervisor locking and signal semantics are Linux-specific")
class SupervisorProcessTests(unittest.TestCase):
    def test_failed_child_restarts_then_signal_stops_child(self):
        with tempfile.TemporaryDirectory(prefix="phone-harness-supervisor-") as td:
            app = Path(td)
            state = app / "state"
            counter = state / "fixture-count"
            (app / "server.py").write_text(
                "import pathlib,sys,time\n"
                f"p=pathlib.Path({str(counter)!r})\n"
                "n=int(p.read_text())+1 if p.exists() else 1\n"
                "p.write_text(str(n))\n"
                "if n == 1: raise SystemExit(17)\n"
                "while True: time.sleep(.1)\n",
                encoding="utf-8",
            )
            spawned: list[subprocess.Popen] = []
            commands: list[list[str]] = []
            poll_wait = threading.Event()
            wait_result: list[bool] = []

            def wait_for_second_child():
                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline:
                    try:
                        if counter.read_text(encoding="ascii") == "2":
                            wait_result.append(True)
                            break
                    except (FileNotFoundError, OSError):
                        pass
                    poll_wait.wait(0.02)
                else:
                    wait_result.append(False)
                # Always release the supervisor so a failed readiness assertion
                # cannot leave the fixture child running.
                os.kill(os.getpid(), signal.SIGTERM)

            def spawn(command, **kwargs):
                commands.append(command)
                child = subprocess.Popen(command, **kwargs)
                spawned.append(child)
                if len(spawned) == 2:
                    threading.Thread(target=wait_for_second_child, daemon=True).start()
                return child

            runner = supervisor.Supervisor(
                app, "danger-full-access", poll_interval=0.02, backoff=0.01,
                popen=spawn, binary_resolver=lambda _app: Path(sys.executable),
            )
            self.assertEqual(runner.run(), 0)
            self.assertTrue(wait_result and wait_result[0],
                            "second fixture child did not write its counter within 3 seconds")
            self.assertEqual(counter.read_text(), "2")
            self.assertEqual(len(commands), 2)
            self.assertEqual(commands[0][commands[0].index("--sandbox") + 1], "danger-full-access")
            self.assertTrue(all(child.poll() is not None for child in spawned))
            self.assertFalse((state / "supervisor.pid").exists())
            self.assertEqual((state / "supervisor.log").stat().st_mode & 0o777, 0o600)

    def test_flock_rejects_second_supervisor(self):
        with tempfile.TemporaryDirectory(prefix="phone-harness-supervisor-lock-") as td:
            app = Path(td)
            first = supervisor.Supervisor(app, binary_resolver=lambda _app: Path(sys.executable))
            second = supervisor.Supervisor(app, binary_resolver=lambda _app: Path(sys.executable))
            first._prepare()
            try:
                with self.assertRaisesRegex(RuntimeError, "already running"):
                    second._prepare()
            finally:
                first._cleanup()
                second._cleanup()


if __name__ == "__main__":
    unittest.main()
