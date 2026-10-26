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
import urllib.parse
import urllib.request
import uuid
from typing import Any, BinaryIO
from phone_backends import BACKENDS, DEFAULT_BACKEND, BackendUnavailable, PhoneBackend, create_backend

DEFAULT_PORT = 18883
MAX_RESPONSE = 5 * 1024 * 1024
MAX_LINE = 1024 * 1024
HTTP_TIMEOUT = 15.0
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
ACTION_NAMES = ("wake", "open_app", "tap_node", "tap", "swipe", "long_press", "input", "back", "home")
COMPACT_TREE_LIMIT = 4800
SENSITIVE_KEY = re.compile(r"(?i)(?:capability|authorization|access[_-]?token|refresh[_-]?token|api[_-]?key|secret|password|credential)")


class BridgeError(Exception):
    def __init__(self, code: str, message: str, approval: dict | None = None, action_id: str | None = None):
        super().__init__(message)
        self.code = code
        self.approval = _public_value(approval) if isinstance(approval, dict) else None
        self.action_id = action_id


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


def _safe_tree(value: Any, compact: bool = True) -> dict[str, Any]:
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
            safe_node.pop("selectionStart", None)
            safe_node.pop("selectionEnd", None)
        if not compact:
            safe_nodes.append(safe_node)
            continue
        focused = safe_node.get("focused") is True
        bounds = safe_node.get("bounds")
        width, height = result.get("width"), result.get("height")
        bounds_valid = isinstance(bounds, list) and len(bounds) == 4 and \
            isinstance(width, (int, float)) and not isinstance(width, bool) and \
            isinstance(height, (int, float)) and not isinstance(height, bool)
        if bounds_valid:
            try: left, top, right, bottom = (float(v) for v in bounds)
            except (TypeError, ValueError): bounds_valid = False
        if bounds_valid and not focused and (
                safe_node.get("visible") is False or right <= 0 or bottom <= 0 or left >= width or
                top >= height or right <= left or bottom <= top):
            continue
        if not bounds_valid and not focused:
            continue
        label_key = "text" if safe_node.get("text") else "description" if safe_node.get("description") else None
        if not label_key and not focused and not any(safe_node.get(flag) is True for flag in ("clickable", "editable", "scrollable")):
            continue
        compact_node = {"id": safe_node.get("id"), "parentId": safe_node.get("parentId"),
                        label_key: safe_node[label_key] if label_key else None}
        if not label_key: compact_node.pop(None, None)
        if bounds_valid: compact_node["bounds"] = bounds
        for flag in ("clickable", "editable", "scrollable"):
            if safe_node.get(flag) is True: compact_node[flag] = True
        if focused: compact_node["focused"] = True
        if safe_node.get("editable") is True and safe_node.get("password") is not True:
            for key in ("selectionStart", "selectionEnd"):
                value = safe_node.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and value >= -1:
                    compact_node[key] = value
        safe_nodes.append(compact_node)
    result["nodes"] = safe_nodes
    if len(nodes) > len(safe_nodes):
        result["truncated"] = True
    if compact:
        original = result.get("truncated") is True
        result["truncated"] = original
        while safe_nodes and len(json.dumps(result, ensure_ascii=False, separators=(",", ":"))) > COMPACT_TREE_LIMIT:
            removable = next((index for index in range(len(safe_nodes) - 1, -1, -1)
                              if safe_nodes[index].get("focused") is not True), None)
            if removable is None:
                break
            safe_nodes.pop(removable)
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
                                  ("POST", "/api/device/execute"), ("POST", "/api/trajectory/feedback"))
        if method == "GET" and path.startswith("/api/device/apps"):
            parts = urllib.parse.urlsplit(path)
            try:
                query = urllib.parse.parse_qs(parts.query, keep_blank_values=True, strict_parsing=True, max_num_fields=2)
                route = (parts.path == "/api/device/apps" and not parts.fragment and not parts.netloc
                         and set(query) <= {"query", "limit"} and all(len(v) == 1 for v in query.values()))
            except ValueError:
                route = False
        if method == "GET" and path == "/api/sms/status":
            route = True
        if method == "POST" and path in ("/api/sms/config", "/api/sms/query", "/api/sms/send"):
            route = self._valid_sms_body(path, body)
        if method == "GET" and re.fullmatch(r"/api/sms/sends/[A-Za-z0-9][A-Za-z0-9._-]{0,127}", path):
            route = True
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

    @staticmethod
    def _valid_sms_body(path: str, body: Any) -> bool:
        if not isinstance(body, dict): return False
        def integer(value, minimum=0):
            return isinstance(value, int) and not isinstance(value, bool) and value >= minimum
        def mobile(value):
            if not isinstance(value, str): return False
            return bool(re.fullmatch(r"\+86(1[3-9][0-9]{9})", value))
        def short_text(value):
            if not isinstance(value, str) or not value: return False
            try: return len(value.encode("utf-16-le", errors="strict")) // 2 <= 70
            except UnicodeEncodeError: return False
        if path == "/api/sms/config":
            if set(body) == {"enabled"} and body["enabled"] is False: return True
            return (set(body) == {"enabled", "subscriptionId"} and type(body["enabled"]) is bool
                    and integer(body["subscriptionId"]))
        if path == "/api/sms/query":
            if set(body) - {"since", "afterId", "limit", "senders"} or not {"since", "afterId", "limit"} <= set(body): return False
            if not integer(body["since"]) or not integer(body["afterId"]): return False
            if not integer(body["limit"], 1) or body["limit"] > 50: return False
            senders = body.get("senders")
            return senders is None or (isinstance(senders, list) and len(senders) <= 50
                                       and all(mobile(item) for item in senders))
        if path == "/api/sms/send":
            if set(body) != {"subscriptionId", "to", "text", "clientRequestId"}: return False
            text, request_id = body["text"], body["clientRequestId"]
            return (integer(body["subscriptionId"]) and mobile(body["to"])
                    and short_text(text)
                    and isinstance(request_id, str) and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", request_id)))
        return False

    def status(self) -> dict[str, Any]:
        return _public_value(self.request("GET", "/api/device/status"))

    def apps(self, query: str = "", limit: int = 80) -> dict[str, Any]:
        if not isinstance(query, str) or len(query) > 80 or isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise BridgeError("invalid_args", "App query must be at most 80 characters and limit 1..200")
        return _public_value(self.request("GET", "/api/device/apps?" + urllib.parse.urlencode({"query": query, "limit": limit})))

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
            raise BridgeError(code[:80], message[:500], error.get("approval"), response.get("trajectory_action_id"))
        result = response.get("result")
        if not isinstance(result, dict):
            return {"result": _public_value(result)}
        public = _public_value(result)
        if response.get("trajectory_action_id") == request_id:
            public["trajectory_action_id"] = request_id
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
    {"name": "android_read_sms", "description": "Read SMS messages from the paired Android phone. This exposes private message content; use only when the user asks for it. An optional sender limits results to one Chinese mobile number. Does not send messages or create tasks.",
     "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
     "inputSchema": {"type": "object", "properties": {
         "sender": {"type": "string", "description": "Optional mainland China mobile number: 11 digits, 86 prefix, +86, or 0086"},
         "since": {"type": "integer", "minimum": 0, "description": "Optional received-at cutoff in Unix milliseconds"},
         "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 10}}, "additionalProperties": False}},
    {"name": "android_apps", "description": "Find already-installed, enabled launcher apps by name or package before opening a store or browser. Read-only metadata; no accounts, phone number, private content or location. An unavailable/truncated catalog is not proof an app is absent.",
     "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string", "maxLength": 80}, "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 80}}, "additionalProperties": False}},
    {"name": "android_status", "description": "Read phone/control readiness, policy, device and installed appStores (OEM first, third-party last), with preferredAppStore when available. Read once initially, then only when readiness changes. Does not change device state.",
     "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "android_observe", "description": "Read the current compact UI tree, including focus/selection and original screen width/height. Reuse the latest observation; normally one read after a UI change suffices. Add a screenshot only for visual ambiguity/canvas controls, requesting tree and image together if needed. compact=false exposes the full tree for missing metadata. Password text is removed. Does not change device state.",
     "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
     "inputSchema": {"type": "object", "properties": {
         "includeTree": {"type": "boolean", "default": True},
         "includeScreenshot": {"type": "boolean", "default": False},
         "compact": {"type": "boolean", "default": True},
         "expectedPackage": {"type": "string", "maxLength": 255}}, "additionalProperties": False}},
    {"name": "android_action", "description": "One phone action; observe its actual result. Prefer tap_node with latest args.nodeId+args.snapshotId. tap requires args.x and args.y in original screen pixel coordinates; long_press uses the same. swipe: args.x1,y1,x2,y2,durationMs=350 (50..2000); move UP inside the list to reveal LOWER rows. After two unchanged swipes adjust once, then change approach. open_app requires args.packageName (prefer status.preferredAppStore for installs). input: complete Unicode string in one args.text call, mode=insert at cursor/selection by default or replace for the whole field; never type Chinese key by key. Full mode attempts authorized actions directly. On approval_required report action+approval ID and end the turn; owner GUI/authenticated email resumes it. Never self-approve, change policy or bypass actual Android restrictions. wake supports non-secure locks only. After timeout observe before another write; never blindly replay it.",
     "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True},
     "inputSchema": {"type": "object", "properties": {
         "action": {"type": "string", "enum": list(ACTION_NAMES)},
         "args": {"type": "object", "properties": {
             "nodeId": {"type": "string", "description": "tap_node/input: requires matching snapshotId"},
             "snapshotId": {"type": "string"},
             "x": {"type": "number"}, "y": {"type": "number"},
             "x1": {"type": "number"}, "y1": {"type": "number"},
             "x2": {"type": "number"}, "y2": {"type": "number"},
             "durationMs": {"type": "integer", "description": "swipe: 50..2000, default 350; long_press: 500..2000, default 700"},
             "packageName": {"type": "string", "description": "open_app: an observed/installed package"},
             "text": {"type": "string", "description": "input: complete Unicode string, max 8000 UTF-16 units"},
             "mode": {"type": "string", "enum": ["insert", "replace"], "default": "insert"}}},
         "expectedPackage": {"type": "string", "maxLength": 255,
                             "description": "Expected CURRENT foreground before the action, not its destination. Omit for open_app; use args.packageName for the app to open."}},
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
    if action == "tap_node":
        node_id, snapshot_id = result.get("nodeId"), result.get("snapshotId")
        if not isinstance(node_id, str) or not node_id or len(node_id) > 255 or \
                not isinstance(snapshot_id, str) or not snapshot_id or len(snapshot_id) > 255:
            raise BridgeError("invalid_args", "tap_node requires non-empty nodeId and snapshotId")
        if any(key in result for key in ("x", "y", "x1", "y1", "x2", "y2")):
            raise BridgeError("invalid_args", "tap_node uses nodeId/snapshotId, not coordinates")
    elif action in ("tap", "long_press"):
        number("x"); number("y")
    elif action == "swipe":
        for name in ("x1", "y1", "x2", "y2"): number(name)
        duration = result.get("durationMs", 350)
        if isinstance(duration, bool) or not isinstance(duration, int) or not 50 <= duration <= 2000:
            raise BridgeError("invalid_args", "swipe durationMs must be between 50 and 2000")
        result["durationMs"] = duration
    elif action == "input":
        text = result.get("text")
        if not isinstance(text, str):
            raise BridgeError("invalid_args", "input text must be a Unicode string")
        try:
            utf16_units = len(text.encode("utf-16-le", errors="strict")) // 2
        except UnicodeEncodeError:
            raise BridgeError("invalid_args", "input text must contain valid Unicode") from None
        if utf16_units > 8000:
            raise BridgeError("invalid_args", "input text must be at most 8000 UTF-16 code units")
        mode = result.get("mode", "insert")
        if not isinstance(mode, str) or mode not in ("insert", "replace"):
            raise BridgeError("invalid_args", "input mode must be 'insert' or 'replace'")
        result["mode"] = mode
        if "nodeId" in result or "snapshotId" in result:
            node_id, snapshot_id = result.get("nodeId"), result.get("snapshotId")
            if not isinstance(node_id, str) or not node_id or len(node_id) > 255 or \
                    not isinstance(snapshot_id, str) or not snapshot_id or len(snapshot_id) > 255:
                raise BridgeError("invalid_args", "input nodeId requires a matching non-empty snapshotId; both must be at most 255 characters")
    elif action == "open_app":
        package = result.get("packageName")
        if not isinstance(package, str) or not re.fullmatch(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+", package):
            raise BridgeError("invalid_args", "packageName must be a valid Android package")
    return result


def _normalize_sms_sender(value: Any) -> str:
    if not isinstance(value, str):
        raise BridgeError("invalid_args", "sender must be a Chinese mainland mobile number")
    raw = value.strip()
    match = re.fullmatch(r"(?:\+86|0086|86)?(1[3-9][0-9]{9})", raw)
    if not match:
        raise BridgeError("invalid_args", "sender must be a Chinese mainland mobile number")
    return "+86" + match.group(1)


class AndroidMCP:
    def __init__(self, bridge: PhoneBackend):
        self.bridge = bridge

    @staticmethod
    def _text(value: Any) -> dict[str, str]:
        return {"type": "text", "text": json.dumps(_public_value(value), ensure_ascii=False, separators=(",", ":"))}

    def _tool_result(self, contents: list[dict[str, Any]], *, is_error: bool = False) -> dict[str, Any]:
        return {"content": contents, "isError": is_error}

    def call_tool(self, name: str, args: Any) -> dict[str, Any]:
        if not isinstance(args, dict):
            raise BridgeError("invalid_args", "Tool arguments must be an object")
        if name == "android_apps":
            return self._tool_result([self._text(self.bridge.apps(args.get("query", ""), args.get("limit", 80)))])
        if name == "android_status":
            return self._tool_result([self._text(self.bridge.status())])
        if name == "android_read_sms":
            if set(args) - {"sender", "since", "limit"}:
                raise BridgeError("invalid_args", "Unsupported SMS query argument")
            sender = args.get("sender")
            if sender is not None:
                sender = _normalize_sms_sender(sender)
            since = args.get("since", 0)
            limit = args.get("limit", 10)
            if isinstance(since, bool) or not isinstance(since, int) or since < 0:
                raise BridgeError("invalid_args", "since must be a non-negative millisecond timestamp")
            if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
                raise BridgeError("invalid_args", "limit must be between 1 and 20")
            request = {"since": since, "afterId": 0, "limit": limit}
            if sender: request["senders"] = [sender]
            result = self.bridge.request("POST", "/api/sms/query", request)
            return self._tool_result([self._text(result)])
        if name == "android_observe":
            include_tree = args.get("includeTree", True)
            include_screenshot = args.get("includeScreenshot", False)
            compact = args.get("compact", True)
            if not isinstance(include_tree, bool) or not isinstance(include_screenshot, bool):
                raise BridgeError("invalid_args", "includeTree/includeScreenshot must be booleans")
            if not isinstance(compact, bool): raise BridgeError("invalid_args", "compact must be a boolean")
            if not include_tree and not include_screenshot:
                raise BridgeError("invalid_args", "Request a tree, screenshot, or both")
            expected = args.get("expectedPackage")
            if expected is not None and (not isinstance(expected, str) or not expected or len(expected) > 255):
                raise BridgeError("invalid_args", "expectedPackage must be a short package name")
            content: list[dict[str, Any]] = []
            if include_tree:
                tree = self.bridge.execute("tree", {**({"expectedPackage": expected} if expected else {})})
                content.append(self._text({"tree": _safe_tree(tree, compact=compact)}))
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
                if exc.action_id is not None: error["trajectory_action_id"] = exc.action_id
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
    parser.add_argument("--backend", choices=[spec.id for spec in BACKENDS], default=DEFAULT_BACKEND,
                        help="Phone capability adapter; only accessibility is currently implemented")
    args = parser.parse_args(argv)
    try:
        bridge = create_backend(args.backend, {
            DEFAULT_BACKEND: lambda: AndroidBridge(args.state_dir, args.port)})
    except BackendUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (BridgeError, ValueError):
        return 2
    return AndroidMCP(bridge).serve(sys.stdin.buffer, sys.stdout.buffer)


if __name__ == "__main__":
    raise SystemExit(main())


# Optional trajectory feedback (embedded for existing update allowlists).
"""Optional public model-decision metadata. No chain of thought or credentials.

Native action IDs returned in MCP results provide exact correlation even for
concurrent tasks. There is no ambient 'current task' shared by MCP processes.
The bounded background queue cannot block the existing app-server callback.
"""
import json
import queue
import re
import threading
from pathlib import Path
from typing import Any

_queue: queue.Queue = queue.Queue(maxsize=64)
_lock = threading.Lock()
_thread: threading.Thread | None = None
_SENSITIVE = re.compile(r'(?i)(password|token|secret|api.?key|验证码|密码|银行卡|聊天|支付)')


def _safe(value: Any, key='') -> Any:
    if _SENSITIVE.search(key) or key in ('text', 'image', 'data'):
        return '[REDACTED]'
    if isinstance(value, dict):
        return {str(k): _safe(v, str(k)) for k, v in list(value.items())[:40]}
    if isinstance(value, list):
        return [_safe(v) for v in value[:20]]
    if isinstance(value, str):
        if _SENSITIVE.search(value): return '[REDACTED]'
        return re.sub(r'(?i)([\w.+-]+@[\w.-]+\.[a-z]{2,}|(?<!\d)\d{6,}(?!\d)|sk-[\w-]+)', '[REDACTED]', value[:1000])
    return value


def _work():
    from android_mcp_server import AndroidBridge
    while True:
        state, body = _queue.get()
        try:
            AndroidBridge(Path(state), timeout=2).request('POST', '/api/trajectory/feedback', body)
        except Exception:
            pass  # Recorder absence/failure never fails a running task.
        finally:
            _queue.task_done()


def observe_engine_event(state: Path, task: dict, params: dict) -> None:
    global _thread
    item = params.get('item') or {}
    content = (item.get('result') or {}).get('content') or []
    ids = []
    for entry in content:
        if not isinstance(entry, dict) or entry.get('type') != 'text': continue
        try:
            value = json.loads(entry.get('text', ''))
        except (ValueError, TypeError):
            continue
        for candidate in (value, value.get('error', {}), value.get('tree', {})):
            ident = candidate.get('trajectory_action_id') if isinstance(candidate, dict) else None
            if isinstance(ident, str) and re.fullmatch(r'[0-9a-f-]{36}', ident): ids.append(ident)
    # Candidate lists and confidence are not invented when the provider omits them.
    arguments = item.get('arguments') or {}
    if isinstance(arguments, str):
        try: arguments = json.loads(arguments)
        except ValueError: arguments = {}
    decision = {'source': 'deepseek_agent', 'model': task.get('model'), 'tool_call_id': item.get('id'),
                'tool': item.get('tool'), 'selected_action': _safe(arguments),
                'candidate_actions': [], 'confidence': None}
    body_task = {'id': task['id'], 'user_task': _safe(task.get('prompt', '')),
                 'subgoal': _safe(task.get('_trajectory_subgoal', '')), 'stage': task.get('status', 'running'), 'source': 'agent'}
    for ident in set(ids):
        try:
            _queue.put_nowait((str(state), {'action_id': ident, 'task': body_task, 'decision': decision,
                            'verification': {'status': 'unverified', 'source': 'agent_claim'}}))
        except queue.Full:
            return
    if ids:
        with _lock:
            if _thread is None or not _thread.is_alive():
                _thread = threading.Thread(target=_work, name='trajectory-feedback', daemon=True)
                _thread.start()
