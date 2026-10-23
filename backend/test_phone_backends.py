"""Backend selection and MCP adapter boundary tests without Android or HTTP."""
import base64
import io
import json
import unittest
from unittest.mock import patch

import android_mcp_server as mcp
import phone_backends as backends


class FakeBackend:
    def __init__(self): self.calls = []
    def status(self):
        self.calls.append(("status", None))
        return {"state": "ready", "backend": "fixture"}
    def execute(self, action, args):
        self.calls.append((action, args))
        if action == "tree":
            return {"snapshotId": "snap-fake", "packageName": "fixture.app", "width": 100, "height": 200,
                    "truncated": False, "nodes": [{"id": "0", "parentId": "", "text": "Fixture",
                    "bounds": [0, 0, 100, 40], "visible": True}]}
        if action == "screenshot":
            return {"image": {"mimeType": "image/png",
                "data": base64.b64encode(mcp.PNG_SIGNATURE + b"fixture").decode("ascii"), "width": 1, "height": 1}}
        return {"accepted": True, "action": action, "args": args}


class PhoneBackendTests(unittest.TestCase):
    def test_default_catalog_is_accessibility_only_and_factory_is_lazy_once(self):
        self.assertEqual(backends.DEFAULT_BACKEND, "accessibility")
        self.assertEqual([(item.id, item.implemented) for item in backends.BACKENDS],
                         [("accessibility", True), ("shizuku", False), ("oem", False)])
        calls = []
        factories = {"accessibility": lambda: calls.append("accessibility") or FakeBackend(),
                     "shizuku": lambda: calls.append("shizuku") or FakeBackend(),
                     "oem": lambda: calls.append("oem") or FakeBackend()}
        backend = backends.create_backend(backends.DEFAULT_BACKEND, factories)
        self.assertEqual(calls, ["accessibility"])
        self.assertIsInstance(backend, FakeBackend)

    def test_unavailable_unknown_and_unregistered_backends_never_fallback(self):
        calls = []
        factories = {"accessibility": lambda: calls.append("accessibility") or FakeBackend(),
                     "shizuku": lambda: calls.append("shizuku") or FakeBackend(),
                     "oem": lambda: calls.append("oem") or FakeBackend()}
        for name in ("shizuku", "oem", "unknown"):
            with self.subTest(name=name), self.assertRaises(backends.BackendUnavailable):
                backends.create_backend(name, factories)
        with self.assertRaises(backends.BackendUnavailable):
            backends.create_backend("accessibility", {})
        self.assertEqual(calls, [])

    def test_android_mcp_runs_status_observe_and_action_through_protocol_fake(self):
        backend = FakeBackend()
        server = mcp.AndroidMCP(backend)
        status = json.loads(server.call_tool("android_status", {})["content"][0]["text"])
        self.assertEqual(status["backend"], "fixture")
        observed = server.call_tool("android_observe", {"includeTree": True, "includeScreenshot": True})
        tree = json.loads(observed["content"][0]["text"])["tree"]
        self.assertEqual(tree["snapshotId"], "snap-fake")
        image = next(block for block in observed["content"] if block["type"] == "image")
        self.assertTrue(base64.b64decode(image["data"], validate=True).startswith(mcp.PNG_SIGNATURE))
        action = server.call_tool("android_action", {"action": "tap_node", "args": {
            "nodeId": "0.1", "snapshotId": "snap-fake"}})
        self.assertIn('"accepted":true', action["content"][0]["text"])
        self.assertEqual([name for name, _ in backend.calls], ["status", "tree", "screenshot", "tap_node"])

    def test_cli_rejects_unimplemented_backend_before_bridge_or_credentials(self):
        output, errors = io.BytesIO(), io.StringIO()
        with patch.object(mcp, "AndroidBridge", side_effect=AssertionError("credentials/backend must not initialize")) as bridge, \
                patch.object(mcp.sys, "stdin", io.BytesIO()), patch.object(mcp.sys, "stdout", output), \
                patch.object(mcp.sys, "stderr", errors):
            result = mcp.main(["--backend", "shizuku", "--state-dir", "/missing/private/state"])
        self.assertEqual(result, 2)
        self.assertEqual(output.getvalue(), b"")
        self.assertIn("not implemented", errors.getvalue())
        bridge.assert_not_called()


if __name__ == "__main__":
    unittest.main()
