"""Offline preparation, config and Linux process-lifecycle acceptance fixtures."""
from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
try:
    import tomllib
except ImportError:  # Python 3.10 remains supported by the protocol-test matrix.
    tomllib = None
from types import SimpleNamespace
import unittest
from unittest import mock

import embedded_runtime as embedded
import runtime_control
import server
import supervisor

TOKEN = "embedded-fixture-capability-0123456789"


def fixture_layout(root: Path) -> Path:
    app = root / "app"
    for name in ("server.py", "supervisor.py", "runtime_control.py", "run_motor_mcp.py", "motor/__init__.py",
                 "motor/mcp_server.py", "motor/task_guard.py", "trajectory/collector.py",
                 "runtime/codex-0.163.0/bin/codex"):
        path = app / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# test fixture only\n", encoding="utf-8")
    (app / "state").mkdir()
    (app / "state/capability").write_text(TOKEN, encoding="ascii")
    return app


class EmbeddedPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="daxqo-embedded-test-")
        self.root = Path(self.temp.name)
        self.app = fixture_layout(self.root)
        self.workspace = self.root / "workspace"

    def tearDown(self):
        self.temp.cleanup()

    def test_preparation_is_offline_and_preserves_device_credentials_and_existing_data(self):
        provider = self.app / "state/provider-fixture.json"
        provider.write_text('{"fixture":"kept"}', encoding="utf-8")
        self.workspace.mkdir()
        output = self.workspace / "existing.txt"
        output.write_text("existing task result", encoding="utf-8")
        with mock.patch("socket.create_connection", side_effect=AssertionError("network during preparation")), \
                mock.patch("subprocess.Popen", side_effect=AssertionError("installer during preparation")):
            embedded.prepare_runtime(self.app, self.workspace)
            embedded.prepare_runtime(self.app, self.workspace)
        runtime = json.loads((self.app / "runtime.json").read_text())
        self.assertEqual(runtime["binary"], str(self.app / "runtime/codex-0.163.0/bin/codex"))
        self.assertEqual((self.app / "state/capability").read_text(), TOKEN)
        self.assertEqual(provider.read_text(), '{"fixture":"kept"}')
        self.assertEqual(output.read_text(), "existing task result")

    def test_missing_android_credentials_and_missing_package_fail_without_download_fallback(self):
        token = self.app / "state/capability"
        token.unlink()
        with self.assertRaisesRegex(RuntimeError, "Android has not supplied"):
            embedded.prepare_runtime(self.app, self.workspace)
        self.assertFalse(token.exists())
        token.write_text(TOKEN)
        (self.app / "motor/task_guard.py").unlink()
        with self.assertRaisesRegex(RuntimeError, "component is missing"):
            embedded.prepare_runtime(self.app, self.workspace)

    def test_motor_registration_requires_embedded_environment_and_complete_bundled_package(self):
        with mock.patch.dict(os.environ, {"DAXQO_EMBEDDED": "0"}):
            self.assertFalse(server.bundled_motor_available(self.app))
        with mock.patch.dict(os.environ, {"DAXQO_EMBEDDED": "1"}):
            self.assertTrue(server.bundled_motor_available(self.app))
            (self.app / "motor/task_guard.py").unlink()
            self.assertFalse(server.bundled_motor_available(self.app))

    def test_motor_launcher_imports_its_bundle_from_another_cwd_without_pythonpath(self):
        source = Path(embedded.__file__).resolve().parent
        launcher = self.app / "run_motor_mcp.py"
        shutil.copy2(source / launcher.name, launcher)
        (self.app / "motor/mcp_server.py").write_text(
            "import json,sys\ndef main():\n print(json.dumps({'fixture_args':sys.argv[1:]}));return 0\n",
            encoding="utf-8")
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        result = subprocess.run([sys.executable, str(launcher), "local-fixture"], cwd=self.root,
                                env=env, capture_output=True, text=True, timeout=3, check=True)
        self.assertEqual(json.loads(result.stdout), {"fixture_args": ["local-fixture"]})

    @unittest.skipIf(tomllib is None, "TOML parsing requires Python 3.11+; remaining bootstrap checks run on 3.10")
    def test_config_registers_one_guarded_motor_and_keeps_all_android_tools(self):
        harness = server.Harness.__new__(server.Harness)
        harness.state, harness.codex_home = self.app / "state", self.app / "state/codex"
        harness.provider = {"provider": "deepseek", "model": "deepseek-flash", "baseUrl": server.DEEPSEEK_BASE_URL}
        harness.services = SimpleNamespace(settings={})
        with mock.patch.object(server, "bundled_motor_available", return_value=True):
            harness._write_codex_config()
            harness._write_codex_config()
        text = (harness.codex_home / "config.toml").read_text(encoding="utf-8")
        config = tomllib.loads(text)
        self.assertEqual(text.count("[mcp_servers.daxqo_motor]\n"), 1)
        motor = config["mcp_servers"]["daxqo_motor"]
        self.assertEqual(motor["command"], sys.executable)
        self.assertEqual(motor["args"], [str(Path(server.__file__).resolve().parent / "run_motor_mcp.py"), "--state-dir", str(harness.state),
                         "--map-db", str(harness.state / "motor/app-map.sqlite3"),
                         "--trajectory-feedback", "--harness-task-guard"])
        self.assertEqual(set(motor["enabled_tools"]), {"motor_map", "motor_scout", "motor_navigate", "motor_reflex", "motor_cancel"})
        self.assertEqual(config["mcp_servers"]["daxqo_android"]["enabled_tools"],
                         ["android_status", "android_apps", "android_observe", "android_action"])
        self.assertIn("never issue the same action through both tools", config["developer_instructions"])
        with mock.patch.object(server, "bundled_motor_available", return_value=False):
            harness._write_codex_config()
        ordinary = tomllib.loads((harness.codex_home / "config.toml").read_text(encoding="utf-8"))
        self.assertNotIn("daxqo_motor", ordinary["mcp_servers"])

    def test_embedded_workspace_and_existing_bridge_ownership_are_preserved(self):
        with mock.patch.dict(os.environ, {"DAXQO_EMBEDDED": "1", "DAXQO_WORKSPACE": str(self.workspace)}):
            command = supervisor.Supervisor(self.app).command(Path("bundled-codex"))
            self.assertEqual(command[command.index("--workspace") + 1], str(self.workspace))
            with mock.patch.object(server.subprocess, "Popen", side_effect=AssertionError("duplicate bridge")):
                self.assertIsNone(server.ensure_runtime_control(self.app, self.app / "state", self.workspace, 18880))

    def test_process_ownership_uses_proc_uid_and_still_rejects_other_uid_or_start_time(self):
        script = self.app / "supervisor.py"
        child = {"pid": 12345, "uid": 10321, "startTime": 42,
                 "args": [sys.executable, str(script), "--app", str(self.app), "--sandbox", "danger-full-access"]}
        own = {"pid": os.getpid(), "uid": 10321}
        manager = runtime_control.RuntimeManager(self.app, self.app / "stage",
                    proc_reader=lambda pid: own if pid == os.getpid() else child)
        record = {"pid": child["pid"], "uid": 10321, "startTime": 42, "script": str(script)}
        with mock.patch.object(runtime_control.sys, "platform", "linux"), \
                mock.patch.object(runtime_control.os, "getuid", return_value=0, create=True):
            self.assertTrue(manager._process_matches(record))
            self.assertFalse(manager._process_matches(dict(record, startTime=41)))
            child["uid"] = 10322
            self.assertFalse(manager._process_matches(dict(record, uid=10322)))

    def test_bridge_bind_failure_releases_instance_lock(self):
        manager = runtime_control.RuntimeManager(self.app, self.app / "stage")
        lock = mock.Mock()
        manager._lock_stream = lock
        with mock.patch.object(runtime_control, "ThreadingHTTPServer", side_effect=OSError("port occupied")):
            with self.assertRaises(OSError):
                manager.serve()
        lock.close.assert_called_once()
        self.assertIsNone(manager._lock_stream)


@unittest.skipUnless(sys.platform.startswith("linux"), "Actual bootstrap process ownership needs Linux /proc and flock")
class EmbeddedProcessTests(unittest.TestCase):
    def test_actual_bootstrap_owns_authenticated_bridge_starts_backend_and_cleans_children(self):
        # This deliberately uses a tiny HTTP backend fixture, not a model or a
        # simulated product acceptance. The bootstrap/supervisor/bridge are real.
        for port in (18880, 18882):
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", port)) == 0:
                    self.skipTest("Fixed runtime test port already has an owner")
        with tempfile.TemporaryDirectory(prefix="daxqo-embedded-process-") as temp:
            root = Path(temp)
            app = fixture_layout(root)
            source = Path(embedded.__file__).resolve().parent
            for name in ("embedded_runtime.py", "runtime_control.py", "supervisor.py"):
                shutil.copy2(source / name, app / name)
            codex = app / "runtime/codex-0.163.0/bin/codex"
            codex.write_text(f"#!{sys.executable}\nprint('bootstrap fixture 0.163.0')\n", encoding="utf-8")
            codex.chmod(0o700)
            (app / "server.py").write_text('''import argparse,json,os
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
p=argparse.ArgumentParser();p.add_argument('--state');p.add_argument('--workspace');p.add_argument('--port',type=int);a,_=p.parse_known_args()
state=Path(a.state);token=(state/'capability').read_text().strip()
(state/'fixture-backend.json').write_text(json.dumps({'pid':os.getpid(),'workspace':a.workspace}))
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args): pass
 def do_GET(self):
  body=json.dumps({'service':{'pid':os.getpid()},'app':{'name':'DAXQO'}}).encode()
  self.send_response(200 if self.headers.get('Authorization')=='Bearer '+token else 401);self.end_headers();self.wfile.write(body)
ThreadingHTTPServer(('127.0.0.1',a.port),Handler).serve_forever()
''', encoding="utf-8")
            env = dict(os.environ, DAXQO_EMBEDDED="1")
            process = subprocess.Popen([sys.executable, str(app / "embedded_runtime.py"), "--app", str(app),
                                        "--workspace", str(root / "workspace")], env=env,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            backend_pid = None
            try:
                deadline = time.monotonic() + 12
                while time.monotonic() < deadline:
                    try:
                        info = json.loads((app / "state/fixture-backend.json").read_text())
                        backend_pid = info["pid"]
                        conn = http.client.HTTPConnection("127.0.0.1", 18882, timeout=.5)
                        conn.request("GET", "/api/runtime/status", headers={"Authorization": "Bearer " + TOKEN})
                        response = conn.getresponse(); status = json.loads(response.read()); conn.close()
                        if response.status == 200 and status["backend"]["connected"]:
                            break
                    except (OSError, ValueError, http.client.HTTPException):
                        pass
                    if process.poll() is not None:
                        self.fail("Bootstrap exited before backend readiness: " + process.communicate(timeout=1)[1].decode())
                    time.sleep(.05)
                else:
                    self.fail("Real embedded bootstrap did not bring its fixture backend online")
                self.assertEqual(info["workspace"], str(root / "workspace"))
                conn = http.client.HTTPConnection("127.0.0.1", 18882, timeout=1)
                conn.request("GET", "/api/runtime/status")
                response = conn.getresponse(); response.read(); conn.close()
                self.assertEqual(response.status, 401)
                # A second bootstrap must fail instead of adopting/duplicating
                # the running runtime; its shutdown must not kill the first.
                duplicate = subprocess.run(process.args, env=env, capture_output=True, timeout=4)
                self.assertEqual(duplicate.returncode, 1)
                self.assertIsNone(process.poll())
                os.kill(process.pid, signal.SIGTERM)
                stdout, stderr = process.communicate(timeout=12)
                self.assertEqual(process.returncode, 0, stderr.decode())
                self.assertNotIn(TOKEN.encode(), stdout + stderr)
                self.assertFalse((app / "state/supervisor.pid").exists())
                self.assertFalse((app / "state/runtime-supervisor.json").exists())
                with self.assertRaises(ProcessLookupError):
                    os.kill(backend_pid, 0)
            finally:
                if process.poll() is None:
                    process.terminate()
                    try: process.communicate(timeout=12)
                    except subprocess.TimeoutExpired:
                        process.kill(); process.communicate(timeout=2)


if __name__ == "__main__":
    unittest.main()
