"""Runtime bridge API tests. All process lifecycle cases use a fake fixture."""
from __future__ import annotations

import hashlib
import http.client
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock

import runtime_control as bridge


class FakeLifecycleFixture:
    """An in-memory supervisor fixture; it never launches Python or Codex."""
    def __init__(self):
        self.starts = 0
        self.alive = False

    def start(self):
        self.starts += 1
        self.alive = True


class RuntimeBridgeApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="daxqo-runtime-test-")
        self.root = Path(self.temp.name)
        (self.root / "state").mkdir()
        capability = self.root / "state" / "capability"
        capability.write_text("fixture-capability-token-0123456789", encoding="ascii")
        if os.name != "nt":
            os.chmod(capability, 0o600)
        self.stage = self.root / "stage"
        self.stage.mkdir()
        self.manager = bridge.RuntimeManager(self.root, self.stage, port=0, backend_port=18880)
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), bridge.create_handler(self.manager))
        self.manager.port = self.http.server_port
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request(self, method="GET", path="/api/runtime/status", *, auth=True,
                host=None, origin=None, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.http.server_port, timeout=3)
        headers = {"Host": host or f"127.0.0.1:{self.http.server_port}"}
        if auth:
            headers["Authorization"] = "Bearer fixture-capability-token-0123456789"
        if origin is not None:
            headers["Origin"] = origin
        raw = json.dumps(body or {}).encode("utf-8") if method == "POST" else None
        if raw is not None:
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=raw, headers=headers)
        response = conn.getresponse()
        data = json.loads(response.read().decode("utf-8"))
        status = response.status
        conn.close()
        return status, data

    def make_update(self, relative="server.py", content=b"fixture source"):
        staged = self.stage / relative
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(content)
        return {"version": bridge.APP_VERSION,
                "files": {relative: hashlib.sha256(content).hexdigest()}}

    def test_healthz_is_public_but_runtime_status_requires_capability(self):
        status, data = self.request(path="/healthz", auth=False)
        self.assertEqual(status, 200)
        self.assertEqual(data, {"ok": True})
        status, _ = self.request(auth=False)
        self.assertEqual(status, 401)
        status, data = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(data["app"], {"name": "DAXQO", "version": bridge.APP_VERSION})
        self.assertEqual(data["state"], "stopped")

    def test_host_origin_and_bearer_are_restricted(self):
        status, _ = self.request(host="localhost:18882")
        self.assertEqual(status, 403)
        status, _ = self.request(origin="http://127.0.0.1:9999")
        self.assertEqual(status, 403)
        status, _ = self.request(origin="http://127.0.0.1:18880")
        self.assertEqual(status, 200)
        status, _ = self.request(auth=False, path="/api/runtime/status")
        self.assertEqual(status, 401)

    def test_backend_origin_preflight_is_limited_to_runtime_headers(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.http.server_port, timeout=3)
        origin = "http://127.0.0.1:18880"
        conn.request("OPTIONS", "/api/runtime/start", headers={
            "Host": f"127.0.0.1:{self.http.server_port}", "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        })
        response = conn.getresponse()
        self.assertEqual(response.status, 204)
        self.assertEqual(response.getheader("Access-Control-Allow-Origin"), origin)
        response.read()
        conn.close()

    def test_update_rejects_traversal_unknown_source_symlink_and_bad_digest(self):
        cases = [
            ({"version": bridge.APP_VERSION, "files": {"../server.py": "0" * 64}}, 400),
            ({"version": bridge.APP_VERSION, "files": {"state/capability": "0" * 64}}, 403),
            ({"version": bridge.APP_VERSION, "files": {"server.py": "0" * 64}}, 400),
        ]
        for body, expected in cases:
            with self.subTest(body=body):
                status, _ = self.request("POST", "/api/runtime/update", body=body)
                self.assertEqual(status, expected)
        if hasattr(Path, "symlink_to"):
            outside = self.root / "outside.py"
            outside.write_text("fixture", encoding="utf-8")
            link = self.stage / "server.py"
            try:
                link.symlink_to(outside)
            except (OSError, NotImplementedError):
                return
            body = {"version": bridge.APP_VERSION, "files": {"server.py": hashlib.sha256(b"fixture").hexdigest()}}
            status, _ = self.request("POST", "/api/runtime/update", body=body)
            self.assertEqual(status, 403)

    def test_other_active_task_blocks_restart_and_update(self):
        self.manager._probe_override = lambda: {"activeTaskId": "fixture-task", "service": {"pid": 7}}
        status, _ = self.request("POST", "/api/runtime/restart", body={})
        self.assertEqual(status, 409)
        status, _ = self.request("POST", "/api/runtime/update", body=self.make_update())
        self.assertEqual(status, 409)

    def test_duplicate_start_uses_one_fake_supervisor_fixture(self):
        fixture = FakeLifecycleFixture()
        self.manager._probe_override = lambda: None
        self.manager._owned_supervisor = lambda: {"pid": 9876} if fixture.alive else None
        self.manager._launch_supervisor = fixture.start
        first, body1 = self.request("POST", "/api/runtime/start", body={})
        second, body2 = self.request("POST", "/api/runtime/start", body={})
        self.assertEqual((first, body1["state"]), (202, "starting"))
        self.assertEqual((second, body2["state"]), (200, "already-running"))
        self.assertEqual(fixture.starts, 1)

    def test_nonempty_start_body_is_rejected(self):
        status, _ = self.request("POST", "/api/runtime/start", body={"unexpected": True})
        self.assertEqual(status, 400)

    def test_oversize_request_is_rejected(self):
        # The server rejects the declared length before reading the body. Sending
        # a huge body concurrently races its early close on Android/Linux TCP.
        conn = http.client.HTTPConnection("127.0.0.1", self.http.server_port, timeout=3)
        try:
            conn.putrequest("POST", "/api/runtime/update")
            conn.putheader("Authorization", "Bearer fixture-capability-token-0123456789")
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Content-Length", str(bridge.MAX_REQUEST + 1))
            conn.endheaders()
            response = conn.getresponse()
            self.assertEqual(response.status, 413)
            response.read()
        finally:
            conn.close()

    def test_oversize_staged_file_is_rejected(self):
        content = b"x" * (bridge.MAX_FILE + 1)
        status, _ = self.request("POST", "/api/runtime/update", body=self.make_update(content=content))
        self.assertEqual(status, 413)

    def test_verified_update_uses_cached_payload_and_sqlite_backup(self):
        target = self.root / "server.py"
        target.write_text("old fixture source", encoding="utf-8")
        db_path = self.root / "state" / "harness.sqlite3"
        db = sqlite3.connect(db_path)
        try:
            db.execute("CREATE TABLE tasks (id TEXT, status TEXT)")
            db.execute("INSERT INTO tasks VALUES ('fixture', 'completed')")
            db.commit()
        finally:
            db.close()
        private = self.root / "state" / "private"
        private.mkdir()
        (private / "provider.json").write_text('{"apiKey":"fixture-secret"}', encoding="utf-8")
        body = self.make_update(content=b"new fixture source")
        fixture = FakeLifecycleFixture()
        self.manager._probe_override = lambda: None
        self.manager.tests_runner = lambda _cwd: 0
        self.manager._launch_supervisor = fixture.start
        self.manager._wait_backend_ready = lambda _timeout=45.0: True
        status, _ = self.request("POST", "/api/runtime/update", body=body)
        self.assertEqual(status, 202)
        self.manager._update_thread.join(timeout=5)
        update = self.manager.status()["update"]
        self.assertEqual(update["state"], "completed")
        self.assertEqual(update["tests"]["exitCode"], 0)
        report = self.root / "state" / "update-tests.log"
        self.assertTrue(report.is_file())
        if os.name != "nt":
            self.assertEqual(os.stat(report).st_mode & 0o777, 0o600)
        self.assertEqual(target.read_text(encoding="utf-8"), "new fixture source")
        backup = next((self.root / "state" / "backups").glob(bridge.APP_VERSION + "-*/harness.sqlite3"))
        self.assertTrue(backup.is_file())
        self.assertFalse(any((self.root / "state" / "backups").rglob("provider.json")))
        self.assertEqual(fixture.starts, 1)

    def test_failed_update_restores_sources_without_stopping_old_supervisor(self):
        target = self.root / "server.py"
        target.write_text("old fixture source", encoding="utf-8")
        self.manager._probe_override = lambda: None
        self.manager.tests_runner = lambda _cwd: (1, "fixture test failure details")
        self.manager._owned_supervisor = lambda: {"pid": 1234}
        with mock.patch.object(self.manager, "stop_supervisor") as stop:
            status, _ = self.request("POST", "/api/runtime/update",
                                     body=self.make_update(content=b"bad fixture source"))
            self.assertEqual(status, 202)
            self.manager._update_thread.join(timeout=5)
            update = self.manager.status()["update"]
            self.assertEqual(update["state"], "failed")
            self.assertEqual(update["tests"]["exitCode"], 1)
            self.assertIn("fixture test failure details", update["tests"]["summary"])
            self.assertEqual(target.read_text(encoding="utf-8"), "old fixture source")
            stop.assert_not_called()

    def test_update_backend_readiness_timeout_rolls_back_and_does_not_complete(self):
        target = self.root / "server.py"
        target.write_text("old fixture source", encoding="utf-8")
        fixture = FakeLifecycleFixture()
        self.manager._probe_override = lambda: None
        self.manager.tests_runner = lambda _cwd: 0
        self.manager._launch_supervisor = fixture.start
        self.manager._wait_backend_ready = lambda _timeout=45.0: False
        status, _ = self.request("POST", "/api/runtime/update",
                                 body=self.make_update(content=b"new fixture source"))
        self.assertEqual(status, 202)
        self.manager._update_thread.join(timeout=5)
        update = self.manager.status()["update"]
        self.assertEqual(update["state"], "failed")
        self.assertIn("did not become ready", update["error"])
        self.assertEqual(update["tests"]["exitCode"], 0)
        self.assertEqual(target.read_text(encoding="utf-8"), "old fixture source")


if __name__ == "__main__":
    unittest.main()
