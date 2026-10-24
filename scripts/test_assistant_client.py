import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import assistant_client as client


TASK_ID = "d0bfc84e-59f8-4e3c-8d61-c4e468693e43"


class FakeProcess:
    def __init__(self, returncode=0, response=None):
        self.returncode = returncode
        self.response = response or {"jsonrpc": "2.0", "id": 2, "result": {"content": [{"type": "text", "text": json.dumps({"taskId": TASK_ID, "accepted": True})}], "isError": False}}
        self.command = None
        self.kwargs = None
        self.input = None

    def factory(self, command, **kwargs):
        self.command, self.kwargs = command, kwargs
        return self

    def communicate(self, data, timeout):
        self.input = data
        self.timeout = timeout
        return (json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}) + "\n" + json.dumps(self.response) + "\n", "")


class TimeoutProcess(FakeProcess):
    def communicate(self, data, timeout):
        self.input = data
        raise subprocess.TimeoutExpired("mcp", timeout)

    def kill(self):
        self.killed = True


class AssistantClientTests(unittest.TestCase):
    def test_flat_installed_layout_launches_app_root_mcp_server(self):
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory) / "phone-harness"
            app.mkdir()
            installed = app / "assistant_mcp_server.py"
            installed.write_text(textwrap.dedent("""
                import json, sys
                requests = [json.loads(line) for line in sys.stdin if line.strip()]
                call = next(item for item in requests if item.get("method") == "tools/call")
                result = {"taskId": "d0bfc84e-59f8-4e3c-8d61-c4e468693e43", "accepted": True,
                          "serverFile": __file__, "tool": call["params"]["name"]}
                print(json.dumps({"jsonrpc":"2.0", "id":2, "result":{"isError":False,
                    "content":[{"type":"text", "text":json.dumps(result)}]}}))
            """), encoding="utf-8")
            result = client.run_client("daxqo_submit", {"prompt": "Do it", "requestId": "stable-request-123"}, app)
            self.assertEqual(result["serverFile"], str(installed))
            self.assertEqual(result["tool"], "daxqo_submit")

    def test_launches_server_and_performs_mcp_initialize_then_tool_call(self):
        proc = FakeProcess()
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory) / "phone-harness"
            app.mkdir()
            installed = app / "assistant_mcp_server.py"
            installed.write_text("# fake app server path for FakeProcess", encoding="utf-8")
            args = {"prompt": "Make a long task", "requestId": "stable-request-123"}
            result = client.run_client("daxqo_submit", args, app, popen_factory=proc.factory)
            self.assertEqual(result, {"taskId": TASK_ID, "accepted": True})
            self.assertEqual(proc.command[:2], [sys.executable, str(installed)])
            self.assertEqual(proc.command[2:], ["--app", str(app)])
            messages = [json.loads(line) for line in proc.input.splitlines()]
            self.assertEqual(messages[0]["method"], "initialize")
            self.assertEqual(messages[1]["method"], "notifications/initialized")
            self.assertEqual(messages[2]["method"], "tools/call")
            self.assertEqual(messages[2]["params"], {"name": "daxqo_submit", "arguments": args})
            self.assertIs(proc.kwargs["stderr"], subprocess.DEVNULL)

    def test_timeout_reports_uncertain_delivery_and_keeps_request_id(self):
        proc = TimeoutProcess()
        with self.assertRaises(client.ClientFailure) as raised:
            client.run_client("daxqo_submit", {"prompt": "task", "requestId": "stable-request-123"}, popen_factory=proc.factory)
        self.assertEqual(raised.exception.code, "timeout")
        self.assertEqual(raised.exception.request_id, "stable-request-123")
        self.assertIn("delivery may be unknown", str(raised.exception))

    def test_invalid_tool_and_non_object_args_fail_before_starting_server(self):
        with self.assertRaises(client.ClientFailure):
            client.run_client("arbitrary_http", {})
        with self.assertRaises(client.ClientFailure):
            client.run_client("daxqo_status", [])

    def test_stdout_is_one_json_object_on_success_and_error(self):
        output = io.StringIO()
        proc = FakeProcess()
        with patch.object(client, "run_client", return_value={"taskId": TASK_ID}), contextlib.redirect_stdout(output):
            status = client.main(["daxqo_status", "--args", '{"taskId":"' + TASK_ID + '"}'])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output.getvalue()), {"taskId": TASK_ID})
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        output = io.StringIO()
        with patch.object(client, "run_client", side_effect=client.ClientFailure("timeout", "uncertain", request_id="stable-id-123")), contextlib.redirect_stdout(output):
            status = client.main(["daxqo_submit", "--args", '{"requestId":"stable-id-123","prompt":"secret prompt"}'])
        value = json.loads(output.getvalue())
        self.assertEqual(status, 1)
        self.assertEqual(value["requestId"], "stable-id-123")
        self.assertNotIn("secret prompt", output.getvalue())


if __name__ == "__main__":
    unittest.main()
