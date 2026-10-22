import datetime as dt
import hashlib
import hmac
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "remote_email_mcp.py"
if not SCRIPT.is_file():
    SCRIPT = Path(__file__).resolve().with_name("remote_email_mcp.py")
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts"))
from remote_protocol import canonical, sign, verify  # noqa: E402
from remote_email_mcp import RemoteEmailMCP, load_config, make_payload, serve_stdio  # noqa: E402

SECRET = "synthetic-test-secret-that-is-not-a-real-credential"
NOW = dt.datetime(2026, 10, 22, 4, 0, tzinfo=dt.timezone.utc)


def payload(action="submit", **fields):
    return {"v": 1, "action": action, "contact": "owner@example.org", "nonce": "0123456789abcdef", "sentAt": NOW.isoformat(), **fields}


class FakeSMTP:
    instances = []

    def __init__(self, host, port, **kwargs):
        self.host, self.port, self.kwargs = host, port, kwargs
        self.sent = []
        self.logged_in = None
        self.__class__.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def login(self, user, password):
        self.logged_in = (user, password)

    def send_message(self, message):
        self.sent.append(message)


class RemoteProtocolTests(unittest.TestCase):
    def test_canonical_and_sign_do_not_mutate(self):
        original = payload(prompt="你好", signature="ignored")
        before = dict(original)
        canonical_json = '{"action":"submit","contact":"owner@example.org","nonce":"0123456789abcdef","prompt":"你好","sentAt":"2026-10-22T04:00:00+00:00","v":1}'.encode("utf-8")
        expected = hmac.new(SECRET.encode(), canonical_json, hashlib.sha256).hexdigest()
        self.assertEqual(canonical(original), canonical_json)
        signed = sign(original, SECRET)
        self.assertEqual(original, before)
        self.assertEqual(signed["signature"], expected)
        self.assertNotIn("signature", verify(signed, SECRET, now=NOW))

    def test_strict_fields_timestamp_and_signature(self):
        good = sign(payload(prompt="do it"), SECRET)
        verify(good, SECRET, now=NOW)
        cases = []
        cases.append({**good, "adminShell": "rm -rf /"})
        cases.append({**good, "v": True})
        cases.append({**good, "contact": "Owner@example.org"})
        cases.append({**good, "nonce": "short"})
        cases.append({**good, "action": ["submit"]})
        cases.append({**good, "signature": "0" * 64})
        for item in cases:
            with self.subTest(item=item):
                with self.assertRaises(ValueError) as caught:
                    verify(item, SECRET, now=NOW)
                self.assertNotIn(SECRET, str(caught.exception))
        for offset in (dt.timedelta(seconds=-601), dt.timedelta(seconds=121)):
            stale = payload(prompt="x", sentAt=(NOW + offset).isoformat())
            with self.assertRaises(ValueError):
                verify(sign(stale, SECRET), SECRET, now=NOW)
        naive = sign(payload(prompt="x", sentAt="2026-10-22T04:00:00"), SECRET)
        with self.assertRaises(ValueError):
            verify(naive, SECRET, now=NOW)

    def test_action_limits_and_required_fields(self):
        self.assertTrue(verify(sign(payload("submit", prompt="a" * 8000), SECRET), SECRET, now=NOW))
        for data in (
            payload("submit", prompt=""), payload("submit", prompt="x" * 8001),
            payload("submit", prompt="ok", title="x" * 101), payload("stop"),
            payload("result", report="not-an-object"),
        ):
            with self.subTest(data=data):
                with self.assertRaises(ValueError):
                    verify(sign(data, SECRET), SECRET, now=NOW)
        big = {"x": "a" * 8100}
        with self.assertRaises(ValueError):
            verify(sign(payload("result", report=big), SECRET), SECRET, now=NOW)

    def test_timestamp_utc_overflow_is_a_validation_error(self):
        for timestamp in ("0001-01-01T00:00:00+23:59", "9999-12-31T23:59:59-23:59"):
            with self.subTest(timestamp=timestamp):
                with self.assertRaisesRegex(ValueError, "timestamp"):
                    verify(sign(payload(prompt="fixture", sentAt=timestamp), SECRET), SECRET, now=NOW)


class RemoteEmailTests(unittest.TestCase):
    def setUp(self):
        FakeSMTP.instances.clear()
        self.config = {
            "deviceEmail": "device@example.net", "contactEmail": "owner@example.org",
            "contactSecret": SECRET, "smtpHost": "smtp.example.net", "smtpPort": 465,
            "username": "synthetic-user", "password": "synthetic-password",
        }
        self.client = RemoteEmailMCP(self.config, smtp_factory=FakeSMTP)

    def test_tool_sends_signed_email_and_only_reports_smtp_acceptance(self):
        result = self.client.call("remote_submit", {"prompt": "分析本地报告", "title": "测试"})
        smtp = FakeSMTP.instances[-1]
        self.assertEqual((smtp.host, smtp.port), ("smtp.example.net", 465))
        self.assertEqual(smtp.logged_in, ("synthetic-user", "synthetic-password"))
        email = smtp.sent[0]
        self.assertEqual(email["To"], "device@example.net")
        self.assertEqual(email["From"], "owner@example.org")
        self.assertEqual(email["Subject"], "DAXQO/1 " + result["nonce"])
        signed = json.loads(email.get_content())
        verified = verify(signed, SECRET)
        self.assertEqual((verified["action"], verified["prompt"], verified["title"]), ("submit", "分析本地报告", "测试"))
        self.assertEqual(result["delivery"], "sent")
        self.assertIn("unconfirmed", result["notice"])
        self.assertNotIn("completed", result["notice"])

    def test_status_optional_task_and_stop_required(self):
        self.client.call("remote_status", {})
        self.assertNotIn("taskId", json.loads(FakeSMTP.instances[-1].sent[0].get_content()))
        with self.assertRaises(ValueError):
            self.client.call("remote_stop", {})
        self.client.call("remote_stop", {"taskId": "task_123"})
        self.assertEqual(json.loads(FakeSMTP.instances[-1].sent[0].get_content())["action"], "stop")

    def test_failure_is_error_not_automatic_retry(self):
        class Broken(FakeSMTP):
            def send_message(self, message):
                raise OSError("private SMTP detail")
        client = RemoteEmailMCP(self.config, smtp_factory=Broken)
        with self.assertRaisesRegex(ValueError, "unconfirmed") as caught:
            client.send("status", {})
        self.assertNotIn("private SMTP detail", str(caught.exception))

    def test_config_permissions_and_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "client.json"
            path.write_text(json.dumps(self.config), encoding="utf-8")
            if os.name != "nt":
                path.chmod(0o600)
            self.assertEqual(load_config(path, require_smtp=True)["smtpPort"], 465)
            if os.name != "nt":
                path.chmod(0o644)
                with self.assertRaisesRegex(ValueError, "mode 600"):
                    load_config(path, require_smtp=True)
                path.chmod(0o600)
            link = Path(tmp) / "link.json"
            try:
                link.symlink_to(path)
            except (OSError, NotImplementedError):
                self.skipTest("symlink creation unavailable")
            with self.assertRaisesRegex(ValueError, "configuration"):
                load_config(link, require_smtp=True)

    def test_stdio_lists_tools_and_calls_fixture_transport(self):
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "remote_status", "arguments": {}}},
        ]
        import io
        output = io.StringIO()
        serve_stdio(self.client, io.StringIO("\n".join(map(json.dumps, requests)) + "\n"), output)
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(responses[0]["result"]["serverInfo"]["name"], "daxqo-remote-email")
        self.assertEqual({t["name"] for t in responses[1]["result"]["tools"]}, {"remote_submit", "remote_status", "remote_stop"})
        self.assertFalse(responses[2]["result"]["isError"])

    def test_print_command_excludes_secret_and_needs_no_smtp(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "print.json"
            cfg = {k: self.config[k] for k in ("deviceEmail", "contactEmail", "contactSecret")}
            path.write_text(json.dumps(cfg), encoding="utf-8")
            if os.name != "nt":
                path.chmod(0o600)
            proc = subprocess.run([sys.executable, str(SCRIPT), "--config", str(path), "--print-command", "submit", "--prompt", "fixture"], capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertNotIn(SECRET, proc.stdout)
            signed = json.loads(proc.stdout)
            self.assertEqual(verify(signed, SECRET)["prompt"], "fixture")


if __name__ == "__main__":
    unittest.main()
