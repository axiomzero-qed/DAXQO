import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import unittest
import urllib.parse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
import assistant_mcp_server as mcp


TASK_ID = "d0bfc84e-59f8-4e3c-8d61-c4e468693e43"
CAPABILITY = "fixture-capability-0123456789abcdef"


class FixtureHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def _send(self, code, value, headers=None):
        raw = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(code)
        for key, item in (headers or {}).items():
            self.send_header(key, item)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _auth(self):
        self.server.seen.append({"path": self.path, "auth": self.headers.get("Authorization"),
                                 "host": self.headers.get("Host")})
        return self.headers.get("Authorization") == "Bearer " + CAPABILITY

    def do_GET(self):
        if not self._auth():
            return self._send(401, {"error": "unauthorized"})
        if self.server.redirect:
            self.send_response(302)
            self.send_header("Location", "http://example.invalid/steal")
            self.end_headers()
            return
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.path).query))
        offset, limit = int(query.get("offset", 0)), int(query.get("limit", 2000))
        source = "0123456789"
        chunk = source[offset:offset + limit]
        next_offset = offset + len(chunk)
        result = {"text": chunk, "offset": offset, "totalLength": len(source), "truncated": next_offset < len(source),
                  **({"nextOffset": next_offset} if next_offset < len(source) else {})}
        self._send(200, {"taskId": TASK_ID, "title": "Fixture task", "status": "running", "result": result,
                        "resultLength": len(source), "resultNextOffset": next_offset if next_offset < len(source) else None,
                        "prompt": "PRIVATE_PROMPT_DO_NOT_RETURN",
                        "events": [{"text": "PRIVATE_EVENT_DO_NOT_RETURN"}],
                        "activity": {"phase": "working", "elapsedSeconds": 13, "quietSeconds": 2,
                                     "summary": "PRIVATE_ACTIVITY_DO_NOT_RETURN"},
                        "progress": [{"kind": "phase", "phase": "working", "summary": "PRIVATE_PROGRESS_DO_NOT_RETURN",
                                      "createdAt": "2026-10-24T00:00:00Z"}],
                        "liveMessage": "PRIVATE_LIVE_DO_NOT_RETURN", "provider": {"apiKey": "PRIVATE_CONFIG"}})

    def do_POST(self):
        if not self._auth():
            return self._send(401, {"error": "unauthorized"})
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
        self.server.bodies.append((self.path, body))
        if self.server.redirect:
            self.send_response(307)
            self.send_header("Location", "http://example.invalid/steal")
            self.end_headers()
            return
        if self.path == "/api/tasks":
            return self._send(201, {"task": {"id": TASK_ID, "status": "queued", "prompt": body.get("prompt")}})
        if self.path.endswith("/messages"):
            return self._send(202, {"task": {"id": TASK_ID, "status": "running"}, "delivery": "queued"})
        return self._send(200, {"task": {"id": TASK_ID, "status": "stopped"}})


class AssistantMCPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        state = Path(self.temp.name) / "state"
        state.mkdir()
        (state / "capability").write_text(CAPABILITY, encoding="ascii")
        try:
            os.chmod(state / "capability", 0o600)
        except OSError:
            pass
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        self.http.seen, self.http.bodies, self.http.redirect = [], [], False
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.bridge = mcp.TaskBridge(state, self.http.server_port, timeout=1)
        self.server = mcp.AssistantMCP(self.bridge)

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(timeout=1)
        self.temp.cleanup()

    def tool(self, name, args):
        response = self.server.dispatch({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                                         "params": {"name": name, "arguments": args}})
        return response["result"]

    def value(self, result):
        return json.loads(result["content"][0]["text"])

    def test_submit_is_async_receipt_and_forwards_stable_request_id(self):
        request_id = "stable-request-012345"
        result = self.value(self.tool("daxqo_submit", {"prompt": "Do a long research task", "title": "Research", "requestId": request_id}))
        self.assertEqual(result, {"taskId": TASK_ID, "status": "queued", "accepted": True})
        self.assertEqual(self.http.bodies[-1], ("/api/tasks", {"prompt": "Do a long research task", "title": "Research", "clientRequestId": request_id}))
        self.assertNotIn("Do a long research task", json.dumps(result))
        self.assertNotIn(CAPABILITY, json.dumps(result))
        self.assertEqual(self.http.seen[-1]["host"], f"127.0.0.1:{self.http.server_port}")
        self.assertEqual(self.http.seen[-1]["auth"], "Bearer " + CAPABILITY)
        retry = self.value(self.tool("daxqo_submit", {"prompt": "Do a long research task", "title": "Research", "requestId": request_id}))
        self.assertEqual(retry, result)
        self.assertEqual(self.http.bodies[-1][1]["clientRequestId"], request_id)

    def test_status_returns_only_selected_public_fields_and_offset_result_chunks(self):
        result = self.value(self.tool("daxqo_status", {"taskId": TASK_ID, "offset": 3, "limit": 4}))
        self.assertEqual(result["result"], {"text": "3456", "offset": 3, "totalLength": 10, "truncated": True, "nextOffset": 7})
        self.assertEqual(result["resultNextOffset"], 7)
        self.assertEqual(self.http.seen[-1]["path"], f"/api/assistant/tasks/{TASK_ID}?offset=3&limit=4")
        self.assertEqual(result["activity"]["phase"], "working")
        text = json.dumps(result)
        for secret in ("PRIVATE_PROMPT", "PRIVATE_EVENT", "PRIVATE_ACTIVITY", "PRIVATE_PROGRESS", "PRIVATE_LIVE", "PRIVATE_CONFIG", "provider", "events"):
            self.assertNotIn(secret, text)
        self.assertNotIn(CAPABILITY, text)

    def test_status_default_limit_and_final_chunk_are_explicit(self):
        result = self.value(self.tool("daxqo_status", {"taskId": TASK_ID}))
        self.assertEqual(result["result"]["text"], "0123456789")
        self.assertFalse(result["result"]["truncated"])
        self.assertIsNone(result["resultNextOffset"])

    def test_uppercase_task_ids_use_the_canonical_service_route(self):
        result = self.value(self.tool("daxqo_status", {"taskId": TASK_ID.upper()}))
        self.assertEqual(result["taskId"], TASK_ID)
        self.assertEqual(self.http.seen[-1]["path"], f"/api/assistant/tasks/{TASK_ID}?offset=0&limit=2000")

    def test_oversized_frame_cannot_dispatch_a_json_suffix_as_a_task(self):
        suffix = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "daxqo_submit", "arguments": {"prompt": "must not execute", "requestId": "oversized-frame-001"}}}).encode()
        incoming = b" " * (mcp.MAX_REQUEST_LINE + 1) + suffix + b'\n{"jsonrpc":"2.0","id":2,"method":"ping"}\n'
        output = io.BytesIO()
        self.server.serve(io.BytesIO(incoming), output)
        replies = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(replies), 2)
        self.assertEqual(replies[0]["error"]["code"], -32600)
        self.assertEqual(replies[1], {"jsonrpc": "2.0", "id": 2, "result": {}})
        self.assertEqual(self.http.seen, [])

    def test_invalid_rpc_identifiers_are_rejected_before_task_dispatch(self):
        for ident in (True, [], {}):
            with self.subTest(ident=ident):
                reply = self.server.dispatch({"jsonrpc": "2.0", "id": ident, "method": "tools/call",
                    "params": {"name": "daxqo_submit", "arguments": {"prompt": "must not execute", "requestId": "invalid-rpc-id-001"}}})
                self.assertEqual(reply["error"]["code"], -32600)
        self.assertEqual(self.http.seen, [])

    def test_continue_and_stop_use_only_fixed_routes_and_forward_request_id(self):
        request_id = "follow-up-id-0001"
        continued = self.value(self.tool("daxqo_continue", {"taskId": TASK_ID, "message": "Check one more source", "requestId": request_id}))
        self.assertEqual(continued["delivery"], "queued")
        self.assertEqual(self.http.bodies[-1], (f"/api/tasks/{TASK_ID}/messages", {"text": "Check one more source", "clientMessageId": request_id, "retry": False}))
        self.server.call_tool("daxqo_continue", {"taskId": TASK_ID, "message": "Check one more source",
                                                   "requestId": request_id, "retry": True})
        self.assertEqual(self.http.bodies[-1][1]["retry"], True)
        stopped = self.value(self.tool("daxqo_stop", {"taskId": TASK_ID}))
        self.assertEqual(stopped["status"], "stopped")
        self.assertEqual(self.http.bodies[-1], (f"/api/tasks/{TASK_ID}/stop", {}))

    def test_invalid_args_never_reach_http(self):
        invalid = [("daxqo_submit", {"prompt": "x"}),
                   ("daxqo_submit", {"prompt": "x", "requestId": "short"}),
                   ("daxqo_submit", {"prompt": "x", "requestId": "valid-id-0001", "extra": 1}),
                   ("daxqo_status", {"taskId": "../status"}),
                   ("daxqo_status", {"taskId": TASK_ID, "offset": -1}),
                   ("daxqo_status", {"taskId": TASK_ID, "limit": 8001}),
                   ("daxqo_continue", {"taskId": TASK_ID, "message": "x"}),
                   ("daxqo_continue", {"taskId": TASK_ID, "message": "x", "requestId": "valid-id-0001", "retry": "true"})]
        for name, args in invalid:
            with self.subTest(name=name, args=args):
                with self.assertRaises(mcp.BridgeError):
                    self.server.call_tool(name, args)
        self.assertEqual(self.http.seen, [])

    def test_redirect_is_not_followed_and_offline_error_is_safe(self):
        self.http.redirect = True
        with self.assertRaises(mcp.BridgeError) as raised:
            self.server.call_tool("daxqo_submit", {"prompt": "private", "requestId": "request-id-00001"})
        self.assertEqual(raised.exception.code, "redirect_rejected")
        self.assertEqual(len(self.http.seen), 1)
        self.http.shutdown()
        self.http.server_close()
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            offline_port = probe.getsockname()[1]
        self.bridge.port = offline_port
        self.bridge.base = f"http://127.0.0.1:{offline_port}"
        with self.assertRaises(mcp.BridgeError) as raised:
            self.bridge.request("GET", "/api/assistant/tasks/" + TASK_ID + "?offset=0&limit=1")
        self.assertIn(raised.exception.code, {"unavailable", "timeout"})
        self.assertNotIn(CAPABILITY, str(raised.exception))

    def test_stdio_initialize_call_and_tool_list(self):
        data = (json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}) + "\n" +
                '{"jsonrpc":"2.0","method":"notifications/initialized"}\n' +
                '{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n').encode()
        output = io.BytesIO()
        self.server.serve(io.BytesIO(data), output)
        replies = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(replies), 2)
        self.assertEqual(replies[0]["result"]["serverInfo"]["version"], "0.6.0")
        self.assertEqual({item["name"] for item in replies[1]["result"]["tools"]},
                         {"daxqo_submit", "daxqo_status", "daxqo_continue", "daxqo_stop"})
        tools = {item["name"]: item for item in replies[1]["result"]["tools"]}
        self.assertTrue(tools["daxqo_submit"]["annotations"]["destructiveHint"])
        self.assertTrue(tools["daxqo_submit"]["annotations"]["openWorldHint"])
        self.assertTrue(tools["daxqo_continue"]["annotations"]["openWorldHint"])
        self.assertEqual(mcp.MAX_TASK_RESPONSE, 8 * 1024 * 1024)

    def test_capability_symlink_is_rejected(self):
        state = Path(self.temp.name) / "linked"
        state.mkdir()
        try:
            (state / "capability").symlink_to(self.bridge.state_dir / "capability")
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        with self.assertRaises(mcp.BridgeError) as raised:
            mcp.TaskBridge(state, self.http.server_port)
        self.assertEqual(raised.exception.code, "credentials_unavailable")


if __name__ == "__main__":
    unittest.main()
