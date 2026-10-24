#!/usr/bin/env python3
"""MCP stdio adapter for submitting and following DAXQO long-running tasks."""
from __future__ import annotations

import argparse
import json
import re
import urllib.error
import urllib.request
from pathlib import Path
import sys
from typing import Any, BinaryIO
from urllib.parse import urlencode

from android_mcp_server import AndroidBridge, BridgeError

DEFAULT_APP = Path("/root/phone-harness")
TASK_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.I)
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$", re.ASCII)
MAX_PROMPT = 50000
MAX_TITLE = 300
MAX_TASK_RESPONSE = 8 * 1024 * 1024
MAX_REQUEST_LINE = 1024 * 1024


class TaskBridge(AndroidBridge):
    """Reuse AndroidBridge's safe capability loading and loopback opener."""

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        url_path, _, query = path.partition("?")
        status_path = re.fullmatch(r"/api/assistant/tasks/" + TASK_ID_RE.pattern[1:-1], url_path)
        message_path = re.fullmatch(r"/api/tasks/" + TASK_ID_RE.pattern[1:-1] + r"/messages", url_path)
        stop_path = re.fullmatch(r"/api/tasks/" + TASK_ID_RE.pattern[1:-1] + r"/stop", url_path)
        status_query = re.fullmatch(r"offset=([0-9]+)&limit=([0-9]+)", query)
        if status_query:
            try: status_offset, status_limit = map(int, status_query.groups())
            except ValueError: status_query = None
            else:
                if status_offset < 0 or not 1 <= status_limit <= 8000: status_query = None
        allowed = ((method == "POST" and path == "/api/tasks") or
                   (method == "GET" and status_path is not None and status_query is not None) or
                   (method == "POST" and message_path is not None) or
                   (method == "POST" and stop_path is not None))
        if not allowed:
            raise BridgeError("invalid_request", "Unsupported DAXQO task route")
        raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8") if body is not None else None
        request = urllib.request.Request(self.base + url_path + ("?" + query if query else ""), data=raw, method=method, headers={
            "Host": f"127.0.0.1:{self.port}", "Authorization": "Bearer " + self.capability,
            "Accept": "application/json",
            **({"Content-Type": "application/json; charset=utf-8"} if raw is not None else {}),
        })
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                if response.status < 200 or response.status >= 300:
                    raise BridgeError("http_error", f"DAXQO task service returned HTTP {response.status}")
                data = response.read(MAX_TASK_RESPONSE + 1)
        except urllib.error.HTTPError as exc:
            if 300 <= exc.code < 400:
                raise BridgeError("redirect_rejected", "DAXQO task service redirects are not allowed") from None
            raise BridgeError("http_error", f"DAXQO task service returned HTTP {exc.code}") from None
        except (TimeoutError, urllib.error.URLError) as exc:
            if isinstance(exc, TimeoutError) or isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, TimeoutError):
                raise BridgeError("timeout", "DAXQO request timed out; delivery may be unknown. Keep the same requestId before retrying") from None
            raise BridgeError("unavailable", "DAXQO task service is unavailable") from None
        except OSError:
            raise BridgeError("unavailable", "DAXQO task service is unavailable") from None
        if len(data) > MAX_TASK_RESPONSE:
            raise BridgeError("response_too_large", "DAXQO task response exceeded the 8 MiB safe size limit")
        try:
            value = json.loads(data or b"{}")
        except (ValueError, UnicodeError):
            raise BridgeError("invalid_response", "DAXQO task service returned invalid JSON") from None
        if not isinstance(value, dict):
            raise BridgeError("invalid_response", "DAXQO task service returned an invalid response")
        if response.status >= 400:
            # Never forward server diagnostics, which can include user supplied text.
            raise BridgeError("http_error", f"DAXQO task service returned HTTP {response.status}")
        return value


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BridgeError("invalid_args", f"{label} must be an object")
    return value


def _request_id(value: Any) -> str:
    if not isinstance(value, str) or not (TASK_ID_RE.fullmatch(value) or REQUEST_ID_RE.fullmatch(value)):
        raise BridgeError("invalid_args", "requestId must be a stable UUID or 8 to 128 safe characters")
    return value


def _task_id(value: Any) -> str:
    if not isinstance(value, str) or not TASK_ID_RE.fullmatch(value):
        raise BridgeError("invalid_args", "taskId must be a task UUID")
    return value.lower()


def _text(value: Any, field: str, maximum: int, *, nonempty: bool = True) -> str:
    if not isinstance(value, str) or (nonempty and not value.strip()) or len(value) > maximum:
        raise BridgeError("invalid_args", f"{field} must be a non-empty string of at most {maximum} characters")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise BridgeError("invalid_args", f"{field} must contain valid Unicode") from None
    return value


TOOL_DEFINITIONS = [
    {"name": "daxqo_submit", "description": "Submit a complete long-running task to DAXQO. Returns as soon as it is accepted; it does not wait for completion. Keep and reuse the same requestId after a timeout because delivery may be unknown.",
     "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": True},
     "inputSchema": {"type": "object", "properties": {"prompt": {"type": "string", "minLength": 1, "maxLength": MAX_PROMPT}, "title": {"type": "string", "maxLength": MAX_TITLE}, "requestId": {"type": "string", "minLength": 8, "maxLength": 128}}, "required": ["prompt", "requestId"], "additionalProperties": False}},
    {"name": "daxqo_status", "description": "Read a compact public task status. Result text is returned in chunks; use resultNextOffset as the offset to fetch the next chunk. Does not return the submitted prompt or full event history.",
     "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
     "inputSchema": {"type": "object", "properties": {"taskId": {"type": "string"}, "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 8000}}, "required": ["taskId"], "additionalProperties": False}},
    {"name": "daxqo_continue", "description": "Send a follow-up message to an existing task. Supply a stable requestId and reuse it after any uncertain timeout.",
     "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
     "inputSchema": {"type": "object", "properties": {"taskId": {"type": "string"}, "message": {"type": "string", "minLength": 1, "maxLength": MAX_PROMPT}, "requestId": {"type": "string", "minLength": 8, "maxLength": 128}, "retry": {"type": "boolean", "default": False, "description": "Retry the same requestId only; the server retries it only when prior delivery is confirmed safe."}}, "required": ["taskId", "message", "requestId"], "additionalProperties": False}},
    {"name": "daxqo_stop", "description": "Request that DAXQO stop the specified task and report the server-confirmed task status.",
     "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False},
     "inputSchema": {"type": "object", "properties": {"taskId": {"type": "string"}}, "required": ["taskId"], "additionalProperties": False}},
]


class AssistantMCP:
    def __init__(self, bridge: AndroidBridge):
        self.bridge = bridge

    @staticmethod
    def _content(value: Any) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False, separators=(",", ":"))}], "isError": False}

    def call_tool(self, name: str, args: Any) -> dict[str, Any]:
        args = _object(args, "Tool arguments")
        if name == "daxqo_submit":
            if set(args) - {"prompt", "title", "requestId"}:
                raise BridgeError("invalid_args", "Unsupported daxqo_submit argument")
            prompt = _text(args.get("prompt"), "prompt", MAX_PROMPT)
            request_id = _request_id(args.get("requestId"))
            body: dict[str, Any] = {"prompt": prompt, "clientRequestId": request_id}
            if "title" in args:
                body["title"] = _text(args["title"], "title", MAX_TITLE, nonempty=False)
            value = self.bridge.request("POST", "/api/tasks", body)
            task = value.get("task") if isinstance(value.get("task"), dict) else {}
            task_id = task.get("id")
            if not isinstance(task_id, str) or not TASK_ID_RE.fullmatch(task_id):
                raise BridgeError("invalid_response", "DAXQO returned an invalid task identifier")
            # Deliberately return only the receipt. Do not echo the submitted prompt.
            return self._content({"taskId": task_id, "status": task.get("status", "queued"), "accepted": True})
        if name == "daxqo_status":
            if set(args) - {"taskId", "offset", "limit"}:
                raise BridgeError("invalid_args", "Unsupported daxqo_status argument")
            task_id = _task_id(args.get("taskId"))
            offset = args.get("offset", 0)
            limit = args.get("limit", 2000)
            if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
                raise BridgeError("invalid_args", "offset must be a non-negative integer")
            if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 8000:
                raise BridgeError("invalid_args", "limit must be between 1 and 8000")
            raw = self.bridge.request("GET", f"/api/assistant/tasks/{task_id}?{urlencode({'offset': offset, 'limit': limit})}")
            title = raw.get("title", "")
            status = raw.get("status", "unknown")
            if not isinstance(title, str) or not isinstance(status, str):
                raise BridgeError("invalid_response", "DAXQO task status returned invalid public fields")
            public = {"taskId": task_id, "title": title[:MAX_TITLE], "status": status[:80]}
            if isinstance(raw.get("error"), str):
                public["error"] = raw["error"][:1000]
            result = raw.get("result")
            if result is not None:
                if (not isinstance(result, dict) or not isinstance(result.get("text"), str)
                        or isinstance(result.get("offset"), bool) or not isinstance(result.get("offset"), int)
                        or isinstance(result.get("totalLength"), bool) or not isinstance(result.get("totalLength"), int)
                        or not isinstance(result.get("truncated"), bool)):
                    raise BridgeError("invalid_response", "DAXQO task status returned an invalid result page")
                public["result"] = {key: result[key] for key in ("text", "offset", "totalLength", "truncated", "nextOffset") if key in result}
            else:
                public["result"] = None
            detail = raw.get("activity") if isinstance(raw.get("activity"), dict) else {}
            public["activity"] = {key: detail[key] for key in ("phase", "elapsedSeconds", "quietSeconds", "lastEventAt")
                                  if isinstance(detail.get(key), (str, int, float)) and not isinstance(detail.get(key), bool)}
            progress = raw.get("progress") if isinstance(raw.get("progress"), list) else []
            public["progress"] = [{key: item[key] for key in ("phase", "createdAt")
                                   if isinstance(item, dict) and isinstance(item.get(key), str)}
                                  for item in progress[-5:] if isinstance(item, dict) and isinstance(item.get("phase"), str)]
            if isinstance(raw.get("resultLength"), int) and not isinstance(raw.get("resultLength"), bool):
                public["resultLength"] = raw["resultLength"]
            if "resultNextOffset" in raw and (raw["resultNextOffset"] is None or isinstance(raw["resultNextOffset"], int) and not isinstance(raw["resultNextOffset"], bool)):
                public["resultNextOffset"] = raw["resultNextOffset"]
            # Intentionally no prompt, event history, live message, agents, context, or provider data.
            return self._content(public)
        if name == "daxqo_continue":
            if set(args) - {"taskId", "message", "requestId", "retry"}:
                raise BridgeError("invalid_args", "Unsupported daxqo_continue argument")
            task_id = _task_id(args.get("taskId"))
            message = _text(args.get("message"), "message", MAX_PROMPT)
            request_id = _request_id(args.get("requestId"))
            retry = args.get("retry", False)
            if not isinstance(retry, bool):
                raise BridgeError("invalid_args", "retry must be a boolean")
            value = self.bridge.request("POST", f"/api/tasks/{task_id}/messages", {
                "text": message, "clientMessageId": request_id, "retry": retry})
            task = value.get("task") if isinstance(value.get("task"), dict) else {}
            return self._content({"taskId": task_id, "status": task.get("status", "unknown"), "delivery": value.get("delivery", "accepted")})
        if name == "daxqo_stop":
            if set(args) - {"taskId"}:
                raise BridgeError("invalid_args", "Unsupported daxqo_stop argument")
            task_id = _task_id(args.get("taskId"))
            value = self.bridge.request("POST", f"/api/tasks/{task_id}/stop", {})
            task = value.get("task") if isinstance(value.get("task"), dict) else {}
            return self._content({"taskId": task_id, "status": task.get("status", "unknown"), "stopped": task.get("status") == "stopped"})
        raise BridgeError("unknown_tool", "Unknown DAXQO task tool")

    def dispatch(self, message: Any) -> dict[str, Any] | None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return {"jsonrpc": "2.0", "id": message.get("id") if isinstance(message, dict) else None,
                    "error": {"code": -32600, "message": "Invalid JSON-RPC request"}}
        method, ident = message.get("method"), message.get("id")
        if ident is not None and type(ident) not in (int, str):
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request id"}}
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
                      "serverInfo": {"name": "daxqo-assistant-handoff", "version": "0.6.1"}}
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
                result = {"content": [{"type": "text", "text": json.dumps({"ok": False, "error": {"code": exc.code, "message": str(exc)}}, ensure_ascii=False)}], "isError": True}
        else:
            return {"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": "Method not found"}}
        return {"jsonrpc": "2.0", "id": ident, "result": result}

    def serve(self, source: BinaryIO, sink: BinaryIO) -> int:
        while True:
            line = source.readline(MAX_REQUEST_LINE + 1)
            if not line:
                return 0
            if len(line) > MAX_REQUEST_LINE:
                # Drain the entire rejected frame. A JSON-looking suffix of an
                # oversized line must never be executed as another request.
                while line and not line.endswith(b"\n"):
                    line = source.readline(MAX_REQUEST_LINE + 1)
                response = {"jsonrpc": "2.0", "id": None,
                            "error": {"code": -32600, "message": "Request line too large"}}
                sink.write((json.dumps(response, separators=(",", ":")) + "\n").encode("utf-8"))
                sink.flush()
                continue
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
    parser.add_argument("--app", type=Path, default=DEFAULT_APP)
    args = parser.parse_args(argv)
    try:
        bridge = TaskBridge(args.app / "state", 18880)
    except (BridgeError, ValueError):
        return 2
    return AssistantMCP(bridge).serve(sys.stdin.buffer, sys.stdout.buffer)


if __name__ == "__main__":
    raise SystemExit(main())
