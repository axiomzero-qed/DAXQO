"""Offline tests for the Android MCP adapter and loopback HTTP contract."""
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import android_mcp_server as mcp


class BridgeFixture(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def _send(self, code, value, headers=None):
        raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        for key, item in (headers or {}).items(): self.send_header(key, item)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers(); self.wfile.write(raw)

    def _record(self):
        self.server.seen.append({"method": self.command, "path": self.path,
            "host": self.headers.get("Host"), "authorization": self.headers.get("Authorization"),
            "origin": self.headers.get("Origin")})

    def do_GET(self):
        self._record()
        if self.path == "/api/device/status" and getattr(self.server, "redirect_status", False):
            return self._send(302, {}, {"Location": f"http://127.0.0.1:{self.server.server_port}/redirect-target"})
        if self.path == "/redirect-target": return self._send(200, {"followed": True})
        if self.path != "/api/device/status": return self._send(404, {})
        self._send(200, {"version": "0.4", "accessibilityConnected": True, "controlEnabled": True,
                         "needsUser": False, "capability": "fixture-capability-must-not-leak"})

    def do_POST(self):
        self._record()
        request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        if self.path.startswith("/api/device/approvals/"):
            return self._send(200, {"id": self.path.rsplit("/", 1)[-1],
                                    "status": "approved" if request["decision"] == "accept" else "declined"})
        self.server.actions.append(request)
        action = request.get("action")
        if action == "tree":
            result = getattr(self.server, "tree_result", None) or {"snapshotId": "snap-1", "packageName": "example.app", "width": 100, "height": 200,
                      "truncated": False, "nodes": [
                          {"id": "0", "parentId": "", "text": "visible label", "bounds": [0,0,100,40], "visible": True, "password": False},
                          {"id": "1", "parentId": "0", "text": "hidden password", "description": "secret description", "bounds": [0,40,100,80], "visible": True, "password": True}]}
            return self._send(200, {"id": request["id"], "ok": True, "result": result})
        if action == "screenshot":
            image = base64.b64encode(getattr(self.server, "screenshot_data", b"\x89PNG\r\n\x1a\nfixture-png")).decode("ascii")
            return self._send(200, {"id": request["id"], "ok": True, "result": {"image": {
                "mimeType": "image/png", "data": image, "width": 1, "height": 1}}})
        if action == "back":
            return self._send(200, {"id": request["id"], "ok": False,
                "error": {"code": "needs_user", "message": "请在手机上完成身份确认"}})
        return self._send(200, {"id": request["id"], "ok": True, "result": {"action": action, "accepted": True}})


class AndroidMCPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name) / "state"
        self.state.mkdir()
        (self.state / "capability").write_text("fixture-capability-0123456789abcdef", encoding="ascii")
        os.chmod(self.state / "capability", 0o600)
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), BridgeFixture)
        self.http.seen = []
        self.http.actions = []
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.bridge = mcp.AndroidBridge(self.state, self.http.server_port, timeout=1)
        self.server = mcp.AndroidMCP(self.bridge)

    def test_default_timeout_allows_native_execution_window(self):
        self.assertGreaterEqual(mcp.HTTP_TIMEOUT, 10)
        bridge = mcp.AndroidBridge(self.state, self.http.server_port)
        with patch.object(bridge.opener, "open", wraps=bridge.opener.open) as opened:
            bridge.status()
        self.assertEqual(opened.call_args.kwargs["timeout"], 15.0)

    def test_timed_out_write_is_not_automatically_replayed(self):
        with patch.object(self.bridge.opener, "open", side_effect=TimeoutError) as opened:
            with self.assertRaises(mcp.BridgeError) as raised:
                self.bridge.execute("tap", {"x": 10, "y": 20})
        self.assertEqual(raised.exception.code, "timeout")
        self.assertEqual(opened.call_count, 1)

    def tearDown(self):
        self.http.shutdown(); self.http.server_close(); self.thread.join(timeout=1)
        self.temp.cleanup()

    def test_pending_consent_survives_mcp_as_structured_json_without_secret_text(self):
        pending = {"id": "746bdd02-1c84-4f1a-9ed4-cb880402f4ec", "status": "pending", "summary": "安装指定应用", "password": "must-not-leak"}
        with patch.object(self.bridge, "execute", side_effect=mcp.BridgeError("approval_required", "等待机主确认", pending)):
            response = self.server.dispatch({"jsonrpc":"2.0", "id":1, "method":"tools/call", "params":{"name":"android_action", "arguments":{"action":"tap", "args":{"x":10,"y":20}}}})
        value = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual(value["error"]["approval"]["id"], pending["id"])
        self.assertEqual(value["error"]["code"], "approval_required")
        self.assertNotIn("must-not-leak", json.dumps(response))

    def test_adapter_exposes_no_self_approval_or_policy_change_tool(self):
        self.assertEqual({t["name"] for t in mcp.TOOL_DEFINITIONS}, {"android_status", "android_observe", "android_action"})
        response = self.server.dispatch({"jsonrpc":"2.0", "id":1, "method":"tools/call", "params":{"name":"android_approve", "arguments":{}}})
        self.assertTrue(response["result"]["isError"])

    def test_owner_decision_traverses_authenticated_bridge_without_becoming_agent_tool(self):
        ident = "746bdd02-1c84-4f1a-9ed4-cb880402f4ec"
        result = self.bridge.request("POST", "/api/device/approvals/" + ident, {"decision": "accept"})
        self.assertEqual(result, {"id": ident, "status": "approved"})
        self.assertEqual(self.http.seen[-1]["authorization"], "Bearer fixture-capability-0123456789abcdef")
        self.assertEqual(self.http.seen[-1]["path"], "/api/device/approvals/" + ident)
        before = len(self.http.seen)
        for path, body in [("/api/device/policy", {"decision": "accept"}),
                           ("/api/device/approvals/../status", {"decision": "accept"}),
                           ("/api/device/approvals/" + ident, {"decision": "full"})]:
            with self.assertRaises(mcp.BridgeError): self.bridge.request("POST", path, body)
        self.assertEqual(len(self.http.seen), before)

    def test_status_uses_loopback_capability_and_redacts_response(self):
        response = self.server.dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "android_status", "arguments": {}}})
        result = response["result"]
        text = result["content"][0]["text"]
        self.assertIn("accessibilityConnected", text)
        self.assertNotIn("fixture-capability-must-not-leak", text)
        request = self.http.seen[-1]
        self.assertEqual(request["path"], "/api/device/status")
        self.assertEqual(request["host"], f"127.0.0.1:{self.http.server_port}")
        self.assertEqual(request["authorization"], "Bearer fixture-capability-0123456789abcdef")
        self.assertIsNone(request["origin"])

    def test_observe_returns_tree_and_mcp_image_content_without_password_text(self):
        response = self.server.dispatch({"jsonrpc": "2.0", "id": "observe-1", "method": "tools/call",
            "params": {"name": "android_observe", "arguments": {"includeTree": True, "includeScreenshot": True}}})
        contents = response["result"]["content"]
        tree_text = contents[0]["text"]
        self.assertIn("visible label", tree_text)
        self.assertNotIn("hidden password", tree_text)
        self.assertNotIn("secret description", tree_text)
        image = next(item for item in contents if item["type"] == "image")
        self.assertEqual(image["mimeType"], "image/png")
        self.assertTrue(base64.b64decode(image["data"]).startswith(mcp.PNG_SIGNATURE))
        self.assertEqual([item["action"] for item in self.http.actions], ["tree", "screenshot"])

    def test_default_compact_tree_preserves_early_visible_target_as_valid_bounded_json(self):
        nodes = [{"id": "0", "parentId": "", "text": "Root", "bounds": [0,0,1080,2400], "visible": True}]
        nodes.append({"id": "0.1", "parentId": "0", "text": "途虎养车", "bounds": [20,1157,1060,1225],
                      "visible": True, "clickable": True})
        nodes.extend({"id": f"0.2.{i}", "parentId": "0.2", "text": "后续节点" + str(i) + "x" * 180,
                      "bounds": [0, i * 10, 1080, i * 10 + 8], "visible": True} for i in range(120))
        self.http.tree_result = {"snapshotId": "snap-tu-hu", "packageName": "com.xiaomi.market",
                                 "width": 1080, "height": 2400, "truncated": False, "nodes": nodes}
        response = self.server.call_tool("android_observe", {})
        compact_text = response["content"][0]["text"]
        compact = json.loads(compact_text)["tree"]
        self.assertLessEqual(len(compact_text), mcp.COMPACT_TREE_LIMIT + 100)
        self.assertEqual(compact["snapshotId"], "snap-tu-hu")
        target = next(node for node in compact["nodes"] if node["id"] == "0.1")
        self.assertEqual(target["parentId"], "0")
        self.assertEqual(target["text"], "途虎养车")
        self.assertTrue(target["clickable"])
        self.assertTrue(compact["truncated"])

    def test_compact_tree_preserves_focused_unlabeled_node_and_safe_selection_metadata(self):
        bounds = [0, 0, 100, 40]
        nodes = [
            {"id": "0", "parentId": "", "text": "Root", "bounds": [0, 0, 100, 2000], "visible": True},
            {"id": "focus", "parentId": "0", "text": "", "description": "", "bounds": bounds,
             "visible": True, "editable": True, "focused": True, "selectionStart": 1, "selectionEnd": 2},
            {"id": "invalid-selection", "parentId": "0", "text": "Field", "bounds": bounds,
             "visible": True, "editable": True, "selectionStart": True, "selectionEnd": -2},
            {"id": "password", "parentId": "0", "text": "secret", "bounds": bounds,
             "visible": True, "editable": True, "focused": True, "password": True,
             "selectionStart": 1, "selectionEnd": 3},
        ]
        nodes.extend({"id": f"filler-{index}", "parentId": "0", "text": "x" * 160,
                      "bounds": bounds, "visible": True} for index in range(50))
        self.http.tree_result = {"snapshotId": "snap-input", "packageName": "example.app",
                                 "width": 100, "height": 2000, "nodes": nodes}
        response = self.server.call_tool("android_observe", {})
        compact = json.loads(response["content"][0]["text"])["tree"]
        by_id = {node["id"]: node for node in compact["nodes"]}
        self.assertTrue(compact["truncated"])
        self.assertEqual(by_id["focus"]["focused"], True)
        self.assertEqual(by_id["focus"]["selectionStart"], 1)
        self.assertEqual(by_id["focus"]["selectionEnd"], 2)
        self.assertNotIn("selectionStart", by_id["invalid-selection"])
        self.assertNotIn("selectionEnd", by_id["invalid-selection"])
        self.assertTrue(by_id["password"]["focused"])
        self.assertNotIn("selectionStart", by_id["password"])
        self.assertNotIn("selectionEnd", by_id["password"])
        self.assertNotIn("secret", json.dumps(compact))

    def test_raw_tree_remains_available_when_compact_false(self):
        self.http.tree_result = {"snapshotId": "snap-raw", "packageName": "example.app", "width": 10, "height": 10,
            "truncated": False, "nodes": [{"id": "0", "parentId": "", "text": "Raw", "viewId": "pkg:id/title",
            "bounds": [0,0,10,10], "visible": True, "enabled": True},
            {"id": "1", "parentId": "0", "text": "secret", "password": True,
             "bounds": [0,0,10,10], "visible": True, "selectionStart": 1, "selectionEnd": 2}]}
        response = self.server.call_tool("android_observe", {"compact": False})
        tree = json.loads(response["content"][0]["text"])["tree"]
        self.assertEqual(tree["nodes"][0]["viewId"], "pkg:id/title")
        self.assertEqual(tree["nodes"][0]["enabled"], True)
        self.assertNotIn("selectionStart", tree["nodes"][1])
        self.assertNotIn("selectionEnd", tree["nodes"][1])
        self.assertEqual(tree["nodes"][1]["text"], "")

    def test_tap_node_requires_snapshot_identity_and_never_falls_back_to_coordinates(self):
        result = self.server.call_tool("android_action", {"action": "tap_node", "args": {
            "nodeId": "0.1", "snapshotId": "snap-1"}})
        self.assertIn('"accepted":true', result["content"][0]["text"])
        self.assertEqual(self.http.actions[-1], {"id": self.http.actions[-1]["id"], "action": "tap_node",
            "args": {"nodeId": "0.1", "snapshotId": "snap-1"}})
        before = len(self.http.actions)
        for args in ({"nodeId": "0.1", "snapshotId": "snap-1", "x": 20, "y": 30},
                     {"nodeId": "0.1"}, {"snapshotId": "snap-1"}):
            with self.assertRaises(mcp.BridgeError): self.server.call_tool("android_action", {"action": "tap_node", "args": args})
        self.assertEqual(len(self.http.actions), before)

    def test_large_screenshot_survives_real_bridge_request_without_base64_truncation(self):
        expected = mcp.PNG_SIGNATURE + bytes(range(256)) * 100
        self.assertGreater(len(base64.b64encode(expected)), 20000)
        self.http.screenshot_data = expected

        # This traverses AndroidMCP -> AndroidBridge.execute -> authenticated local
        # HTTP fixture, then validates and returns the MCP image content.
        response = self.server.call_tool("android_observe", {"includeTree": False, "includeScreenshot": True})
        image = next(item for item in response["content"] if item["type"] == "image")
        self.assertEqual(base64.b64decode(image["data"], validate=True), expected)
        self.assertEqual([item["action"] for item in self.http.actions], ["screenshot"])

    def test_android_pause_is_returned_as_tool_error_and_never_resumed(self):
        response = self.server.dispatch({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "android_action", "arguments": {"action": "back"}}})
        result = response["result"]
        self.assertTrue(result["isError"])
        self.assertIn("needs_user", result["content"][0]["text"])
        self.assertEqual([item["action"] for item in self.http.actions], ["back"])
        self.assertNotIn("resume", [tool["name"] for tool in mcp.TOOL_DEFINITIONS])

    def test_action_validates_stale_node_reference_before_http_call(self):
        with self.assertRaises(mcp.BridgeError) as raised:
            self.server.call_tool("android_action", {"action": "input", "args": {"text": "code", "nodeId": "4"}})
        self.assertEqual(raised.exception.code, "invalid_args")
        self.assertEqual(self.http.actions, [])

    def test_input_preserves_complete_unicode_and_defaults_to_insert(self):
        text = "你好，世界\n第二行 😀"
        result = self.server.call_tool("android_action", {"action": "input", "args": {"text": text}})
        self.assertIn('"accepted":true', result["content"][0]["text"])
        self.assertEqual(self.http.actions[-1]["args"], {"text": text, "mode": "insert"})

    def test_swipe_uses_explicit_pixel_coordinates_and_native_duration(self):
        args = {"x1": 600, "y1": 1800, "x2": 600, "y2": 700}
        self.server.call_tool("android_action", {"action": "swipe", "args": args})
        self.assertEqual(self.http.actions[-1]["args"], {**args, "durationMs": 350})
        for duration in (50, 2000):
            self.server.call_tool("android_action", {"action": "swipe", "args": {**args, "durationMs": duration}})
            self.assertEqual(self.http.actions[-1]["args"]["durationMs"], duration)

    def test_invalid_swipe_never_reaches_phone(self):
        args = {"x1": 600, "y1": 1800, "x2": 600, "y2": 700}
        for invalid in ({"direction": "up"}, {**args, "x1": float("nan")},
                        *({**args, "durationMs": duration} for duration in (True, 49, 2001, 350.5))):
            with self.subTest(args=invalid):
                with self.assertRaises(mcp.BridgeError) as raised:
                    self.server.call_tool("android_action", {"action": "swipe", "args": invalid})
                self.assertEqual(raised.exception.code, "invalid_args")
        self.assertEqual(self.http.actions, [])

    def test_input_replace_mode_and_matching_node_reference_are_forwarded(self):
        args = {"text": "替换内容", "mode": "replace", "nodeId": "0.1", "snapshotId": "snap-1"}
        self.server.call_tool("android_action", {"action": "input", "args": args})
        self.assertEqual(self.http.actions[-1]["args"], args)

    def test_input_validates_mode_unicode_length_and_node_binding_before_write(self):
        before = len(self.http.actions)
        invalid_args = [
            {"text": "x", "mode": "append"}, {"text": "x", "mode": 1},
            {"text": "x", "mode": None}, {"text": "x", "text2": "ignored", "nodeId": ""},
            {"text": "x", "nodeId": "n"}, {"text": "x", "snapshotId": "snap"},
            {"text": "x", "nodeId": "n", "snapshotId": ""},
            {"text": "x", "nodeId": "n" * 256, "snapshotId": "snap"},
            {"text": "x", "nodeId": "n", "snapshotId": "s" * 256},
            {"text": "x" * 8001}, {"text": "\ud800"},
        ]
        for args in invalid_args:
            with self.subTest(args={k: v if k not in ("nodeId", "snapshotId") or not isinstance(v, str) else f"len={len(v)}" for k, v in args.items()}):
                with self.assertRaises(mcp.BridgeError) as raised:
                    self.server.call_tool("android_action", {"action": "input", "args": args})
                self.assertEqual(raised.exception.code, "invalid_args")
        boundary = "x" * 7998 + "😀"
        self.server.call_tool("android_action", {"action": "input", "args": {"text": boundary}})
        self.assertEqual(self.http.actions[-1]["args"]["text"], boundary)
        self.assertEqual(len(self.http.actions), before + 1)

    def test_http_redirect_is_rejected_without_following(self):
        self.http.redirect_status = True
        with self.assertRaises(mcp.BridgeError) as raised:
            self.bridge.request("GET", "/api/device/status")
        self.assertEqual(raised.exception.code, "redirect_rejected")
        self.assertEqual(len(self.http.seen), 1)

    def test_stdio_initialize_list_and_notifications_are_protocol_clean(self):
        source = io.BytesIO((
            '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}\n'
            '{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
            '{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n').encode())
        sink = io.BytesIO()
        self.assertEqual(self.server.serve(source, sink), 0)
        replies = [json.loads(line) for line in sink.getvalue().splitlines()]
        self.assertEqual(len(replies), 2)
        self.assertEqual(replies[0]["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual({tool["name"] for tool in replies[1]["result"]["tools"]},
                         {"android_status", "android_observe", "android_action"})

    def test_tool_annotations_do_not_disguise_device_actions_as_read_only(self):
        definitions = {tool["name"]: tool for tool in mcp.TOOL_DEFINITIONS}
        for name in ("android_status", "android_observe"):
            self.assertTrue(definitions[name]["annotations"]["readOnlyHint"])
            self.assertFalse(definitions[name]["annotations"]["destructiveHint"])
        actions = definitions["android_action"]
        self.assertFalse(actions["annotations"]["readOnlyHint"])
        self.assertTrue(actions["annotations"]["destructiveHint"])
        self.assertTrue(actions["annotations"]["openWorldHint"])
        self.assertFalse(actions["annotations"]["idempotentHint"])
        allowed = set(actions["inputSchema"]["properties"]["action"]["enum"])
        self.assertEqual(allowed, {"wake", "open_app", "tap_node", "tap", "swipe", "long_press", "input", "back", "home"})
        self.assertTrue(allowed.isdisjoint({"resume", "unlock", "grant_permission", "approve_payment"}))
        description = actions["description"]
        for required in ("open_app requires args.packageName", "tap requires args.x and args.y", "complete Unicode string in one args.text call"):
            self.assertIn(required, description)
        self.assertIn("Prefer tap_node", description)
        self.assertIn("original screen pixel coordinates", description)

    def test_capability_symlink_is_rejected(self):
        linked_state = Path(self.temp.name) / "linked-state"
        linked_state.mkdir()
        target = Path(self.temp.name) / "cap-target"
        target.write_text("fixture-capability-0123456789abcdef", encoding="ascii")
        try: (linked_state / "capability").symlink_to(target)
        except OSError as exc: self.skipTest(f"symlinks unavailable: {exc}")
        with self.assertRaises(mcp.BridgeError) as raised:
            mcp.AndroidBridge(linked_state, self.http.server_port)
        self.assertEqual(raised.exception.code, "credentials_unavailable")


if __name__ == "__main__":
    unittest.main()
