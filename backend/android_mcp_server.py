#!/usr/bin/env python3
"""MCP stdio adapter for the paired, loopback-only DAXQO Android bridge."""
from __future__ import annotations

import argparse
import base64
import binascii
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import urllib.error
import urllib.request
import uuid
from typing import Any, BinaryIO

DEFAULT_PORT = 18883
MAX_RESPONSE = 5 * 1024 * 1024
MAX_LINE = 1024 * 1024
HTTP_TIMEOUT = 15.0
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
ACTION_NAMES = ("wake", "open_app", "tap", "swipe", "long_press", "input", "back", "home")
SENSITIVE_KEY = re.compile(r"(?i)(?:capability|authorization|access[_-]?token|refresh[_-]?token|api[_-]?key|secret|password|credential)")


class BridgeError(Exception):
    def __init__(self, code: str, message: str, approval: dict | None = None):
        super().__init__(message)
        self.code = code
        self.approval = _public_value(approval) if isinstance(approval, dict) else None


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _public_value(value: Any, *, key: str = "") -> Any:
    """Bound and redact bridge data before exposing it to an agent."""
    if SENSITIVE_KEY.search(key):
        if key.lower() == "password" and isinstance(value, bool):
            return value
        return "[redacted]"
    if isinstance(value, dict):
        return {str(k)[:120]: _public_value(v, key=str(k)) for k, v in list(value.items())[:5000]}
    if isinstance(value, list):
        return [_public_value(item) for item in value[:5000]]
    if isinstance(value, str):
        return value[:20000]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:1000]


def _safe_tree(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BridgeError("invalid_response", "Android bridge returned an invalid accessibility tree")
    result = _public_value(value)
    nodes = result.get("nodes")
    if not isinstance(nodes, list):
        result["nodes"] = []
        result["truncated"] = True
        return result
    safe_nodes = []
    for node in nodes[:3000]:
        if not isinstance(node, dict):
            continue
        safe_node = dict(node)
        if safe_node.get("password") is True:
            safe_node["text"] = ""
            safe_node["description"] = ""
        safe_nodes.append(safe_node)
    result["nodes"] = safe_nodes
    if len(nodes) > len(safe_nodes):
        result["truncated"] = True
    return result


class AndroidBridge:
    def __init__(self, state_dir: Path, port: int = DEFAULT_PORT, *, opener=None, timeout: float = HTTP_TIMEOUT):
        if not 1 <= int(port) <= 65535:
            raise ValueError("port must be between 1 and 65535")
        self.state_dir = Path(state_dir).absolute()
        self.port = int(port)
        self.base = f"http://127.0.0.1:{self.port}"
        self.timeout = timeout
        self.opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirectHandler())
        self.capability = self._read_capability()

    def _read_capability(self) -> str:
        try:
            state_info = self.state_dir.lstat()
            if stat.S_ISLNK(state_info.st_mode) or not stat.S_ISDIR(state_info.st_mode):
                raise BridgeError("credentials_unavailable", "DAXQO capability file is unavailable")
            if hasattr(os, "getuid") and state_info.st_uid != os.getuid():
                raise BridgeError("credentials_unavailable", "DAXQO capability file is unavailable")
            path = self.state_dir / "capability"
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise BridgeError("credentials_unavailable", "DAXQO capability file is unavailable")
            if hasattr(os, "getuid") and info.st_uid != os.getuid():
                raise BridgeError("credentials_unavailable", "DAXQO capability file is unavailable")
            if sys.platform.startswith("linux") and stat.S_IMODE(info.st_mode) & 0o077:
                raise BridgeError("credentials_unavailable", "DAXQO capability file permissions are too broad")
            value = path.read_text(encoding="ascii").strip()
            if not 32 <= len(value) <= 512 or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
                raise BridgeError("credentials_unavailable", "DAXQO capability file is invalid")
            return value
        except BridgeError:
            raise
        except (OSError, UnicodeError):
            raise BridgeError("credentials_unavailable", "DAXQO capability file is unavailable") from None

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        route = (method, path) in (("GET", "/api/device/status"), ("GET", "/api/device/approvals"),
                                  ("POST", "/api/device/execute"))
        owner_decision = method == "POST" and re.fullmatch(
            r"/api/device/approvals/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", path)
        if not route and not owner_decision:
            raise BridgeError("invalid_request", "Unsupported Android bridge route")
        if owner_decision and (not isinstance(body, dict) or set(body) != {"decision"}
                               or body["decision"] not in ("accept", "decline")):
            raise BridgeError("invalid_request", "An owner approval requires accept or decline")
        raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8") if body is not None else None
        request = urllib.request.Request(self.base + path, data=raw, method=method, headers={
            "Host": f"127.0.0.1:{self.port}",
            "Authorization": "Bearer " + self.capability,
            "Accept": "application/json",
            **({"Content-Type": "application/json; charset=utf-8"} if raw is not None else {}),
        })
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                if response.status < 200 or response.status >= 300:
                    raise BridgeError("http_error", f"Android bridge returned HTTP {response.status}")
                data = response.read(MAX_RESPONSE + 1)
        except urllib.error.HTTPError as exc:
            if 300 <= exc.code < 400:
                raise BridgeError("redirect_rejected", "Android bridge redirects are not allowed") from None
            raise BridgeError("http_error", f"Android bridge returned HTTP {exc.code}") from None
        except (TimeoutError, urllib.error.URLError) as exc:
            if isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, TimeoutError):
                raise BridgeError("timeout", "Android bridge request timed out; delivery may be unknown") from None
            if isinstance(exc, TimeoutError):
                raise BridgeError("timeout", "Android bridge request timed out; delivery may be unknown") from None
            raise BridgeError("unavailable", "Android control bridge is unavailable; keep DAXQO open and check accessibility status") from None
        except OSError:
            raise BridgeError("unavailable", "Android control bridge is unavailable; keep DAXQO open and check accessibility status") from None
        if len(data) > MAX_RESPONSE:
            raise BridgeError("response_too_large", "Android bridge response exceeded 5 MiB")
        try:
            value = json.loads(data or b"{}")
        except (ValueError, UnicodeError):
            raise BridgeError("invalid_response", "Android bridge returned invalid JSON") from None
        if not isinstance(value, dict):
            raise BridgeError("invalid_response", "Android bridge returned an invalid response")
        return value

    def status(self) -> dict[str, Any]:
        return _public_value(self.request("GET", "/api/device/status"))

    def execute(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        request_id = str(uuid.uuid4())
        envelope = {"id": request_id, "action": action, "args": args}
        response = self.request("POST", "/api/device/execute", envelope)
        if response.get("id") != request_id:
            raise BridgeError("invalid_response", "Android bridge response ID did not match the request")
        if response.get("ok") is not True:
            error = response.get("error") if isinstance(response.get("error"), dict) else {}
            code = error.get("code") if isinstance(error.get("code"), str) else "action_rejected"
            message = error.get("message") if isinstance(error.get("message"), str) else "Android bridge rejected the action"
            raise BridgeError(code[:80], message[:500], error.get("approval"))
        result = response.get("result")
        if not isinstance(result, dict):
            return {"result": _public_value(result)}
        public = _public_value(result)
        if action == "screenshot":
            source_image = result.get("image")
            safe_image = public.get("image") if isinstance(public, dict) else None
            if (isinstance(source_image, dict) and isinstance(source_image.get("data"), str)
                    and isinstance(safe_image, dict)):
                # The HTTP body has already been bounded by MAX_RESPONSE. Keep the
                # base64 payload intact for MCP's later strict base64/PNG validation.
                safe_image["data"] = source_image["data"]
        return public


TOOL_DEFINITIONS = [
    {"name": "android_status", "description": "Read Android DAXQO accessibility/control readiness. Does not change device state.",
     "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "android_observe", "description": "Read the current accessibility tree and optionally capture a screenshot. Password-field text is removed. Does not change device state.",
     "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
     "inputSchema": {"type": "object", "properties": {
         "includeTree": {"type": "boolean", "default": True},
         "includeScreenshot": {"type": "boolean", "default": False},
         "expectedPackage": {"type": "string", "maxLength": 255}}, "additionalProperties": False}},
    {"name": "android_action", "description": "Perform one bounded Android action under the owner's phone policy. Arguments: open_app requires args.packageName (Android package name); tap requires args.x and args.y screen coordinates; input requires args.text. Full mode executes without an extra DAXQO consent gate. Confirm mode returns approval_required plus an approval ID for sensitive writes; report waiting for owner confirmation and end the turn, without asking the owner to operate the phone. Owner GUI or authenticated email handles consent and automatically resumes this task. Never approve yourself or change policy through other tools. Wake can dismiss a known non-secure swipe lock through Android's official API; verify actual state. Report actual system failures accurately; no secure-keyguard or protected-window bypass.",
     "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True},
     "inputSchema": {"type": "object", "properties": {
         "action": {"type": "string", "enum": list(ACTION_NAMES)},
         "args": {"type": "object"},
         "expectedPackage": {"type": "string", "maxLength": 255}},
         "required": ["action"], "additionalProperties": False}},
]


def _validate_action_args(action: str, args: Any, expected_package: Any) -> dict[str, Any]:
    if action not in ACTION_NAMES:
        raise BridgeError("invalid_args", "Unsupported Android action")
    if not isinstance(args, dict):
        raise BridgeError("invalid_args", "args must be an object")
    result = dict(args)
    if expected_package is not None:
        if not isinstance(expected_package, str) or not expected_package or len(expected_package) > 255:
            raise BridgeError("invalid_args", "expectedPackage must be a short package name")
        result["expectedPackage"] = expected_package
    def number(name):
        value = result.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 100000:
            raise BridgeError("invalid_args", f"{name} must be a screen coordinate")
    if action in ("tap", "long_press"):
        number("x"); number("y")
    elif action == "swipe":
        for name in ("x1", "y1", "x2", "y2"): number(name)
        duration = result.get("durationMs", 350)
        if isinstance(duration, bool) or not isinstance(duration, int) or not 1 <= duration <= 10000:
            raise BridgeError("invalid_args", "durationMs must be between 1 and 10000")
        result["durationMs"] = duration
    elif action == "input":
        text = result.get("text")
        if not isinstance(text, str) or len(text) > 16000:
            raise BridgeError("invalid_args", "input text must be a string of at most 16000 characters")
        if result.get("nodeId") is not None and (not isinstance(result.get("nodeId"), str) or not isinstance(result.get("snapshotId"), str)):
            raise BridgeError("invalid_args", "nodeId requires the matching snapshotId")
    elif action == "open_app":
        package = result.get("packageName")
        if not isinstance(package, str) or not re.fullmatch(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+", package):
            raise BridgeError("invalid_args", "packageName must be a valid Android package")
    return result


class AndroidMCP:
    def __init__(self, bridge: AndroidBridge):
        self.bridge = bridge

    @staticmethod
    def _text(value: Any) -> dict[str, str]:
        return {"type": "text", "text": json.dumps(_public_value(value), ensure_ascii=False, separators=(",", ":"))}

    def _tool_result(self, contents: list[dict[str, Any]], *, is_error: bool = False) -> dict[str, Any]:
        return {"content": contents, "isError": is_error}

    def call_tool(self, name: str, args: Any) -> dict[str, Any]:
        if not isinstance(args, dict):
            raise BridgeError("invalid_args", "Tool arguments must be an object")
        if name == "android_status":
            return self._tool_result([self._text(self.bridge.status())])
        if name == "android_observe":
            include_tree = args.get("includeTree", True)
            include_screenshot = args.get("includeScreenshot", False)
            if not isinstance(include_tree, bool) or not isinstance(include_screenshot, bool):
                raise BridgeError("invalid_args", "includeTree/includeScreenshot must be booleans")
            if not include_tree and not include_screenshot:
                raise BridgeError("invalid_args", "Request a tree, screenshot, or both")
            expected = args.get("expectedPackage")
            if expected is not None and (not isinstance(expected, str) or not expected or len(expected) > 255):
                raise BridgeError("invalid_args", "expectedPackage must be a short package name")
            content: list[dict[str, Any]] = []
            if include_tree:
                tree = self.bridge.execute("tree", {**({"expectedPackage": expected} if expected else {})})
                content.append(self._text({"tree": _safe_tree(tree)}))
            if include_screenshot:
                result = self.bridge.execute("screenshot", {**({"expectedPackage": expected} if expected else {})})
                image = result.get("image") if isinstance(result, dict) else None
                if not isinstance(image, dict) or image.get("mimeType") != "image/png" or not isinstance(image.get("data"), str):
                    raise BridgeError("invalid_response", "Android bridge returned an invalid PNG screenshot")
                try:
                    raw = base64.b64decode(image["data"], validate=True)
                except (ValueError, binascii.Error):
                    raise BridgeError("invalid_response", "Android bridge returned invalid screenshot data") from None
                if not raw.startswith(PNG_SIGNATURE) or len(raw) > MAX_RESPONSE:
                    raise BridgeError("invalid_response", "Android bridge returned an invalid or oversized PNG")
                width, height = image.get("width"), image.get("height")
                content.append(self._text({"screenshot": {"width": width, "height": height, "mimeType": "image/png"}}))
                content.append({"type": "image", "mimeType": "image/png", "data": base64.b64encode(raw).decode("ascii")})
            return self._tool_result(content)
        if name == "android_action":
            action = args.get("action")
            expected = args.get("expectedPackage")
            action_args = _validate_action_args(action, args.get("args", {}), expected)
            return self._tool_result([self._text(self.bridge.execute(action, action_args))])
        raise BridgeError("unknown_tool", "Unknown Android control tool")

    def dispatch(self, message: Any) -> dict[str, Any] | None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return {"jsonrpc": "2.0", "id": message.get("id") if isinstance(message, dict) else None,
                    "error": {"code": -32600, "message": "Invalid JSON-RPC request"}}
        method = message.get("method")
        ident = message.get("id")
        if not isinstance(method, str):
            return {"jsonrpc": "2.0", "id": ident, "error": {"code": -32600, "message": "Missing method"}}
        if ident is None:
            return None
        if method == "initialize":
            params = message.get("params") or {}
            version = params.get("protocolVersion") if isinstance(params, dict) else None
            if version not in (None, "2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"):
                return {"jsonrpc": "2.0", "id": ident, "error": {"code": -32602, "message": "Unsupported MCP protocol version"}}
            result = {"protocolVersion": version or "2024-11-05", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "daxqo-android-control", "version": "0.4.0"}}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOL_DEFINITIONS}
        elif method == "tools/call":
            params = message.get("params") or {}
            if not isinstance(params, dict) or not isinstance(params.get("name"), str):
                return {"jsonrpc": "2.0", "id": ident, "error": {"code": -32602, "message": "tools/call requires a tool name"}}
            try:
                result = self.call_tool(params["name"], params.get("arguments", {}))
            except BridgeError as exc:
                error = {"code": exc.code, "message": str(exc)}
                if exc.approval is not None: error["approval"] = exc.approval
                result = self._tool_result([self._text({"ok": False, "error": error})], is_error=True)
        else:
            return {"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": "Method not found"}}
        return {"jsonrpc": "2.0", "id": ident, "result": result}

    def serve(self, source: BinaryIO, sink: BinaryIO) -> int:
        while True:
            line = source.readline(MAX_LINE + 1)
            if not line:
                return 0
            if len(line) > MAX_LINE:
                while line and not line.endswith(b"\n"):
                    line = source.readline(MAX_LINE + 1)
                response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Request line too large"}}
            else:
                try:
                    request = json.loads(line)
                    response = self.dispatch(request)
                except (ValueError, UnicodeError):
                    response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
            if response is not None:
                sink.write((json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
                sink.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=Path(__file__).resolve().parent / "state")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    try:
        bridge = AndroidBridge(args.state_dir, args.port)
    except (BridgeError, ValueError):
        return 2
    return AndroidMCP(bridge).serve(sys.stdin.buffer, sys.stdout.buffer)


if __name__ == "__main__":
    raise SystemExit(main())
