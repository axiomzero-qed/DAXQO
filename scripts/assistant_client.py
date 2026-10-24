#!/usr/bin/env python3
"""Call the DAXQO assistant-handoff MCP server over stdio."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "backend" / "assistant_mcp_server.py"
DEFAULT_APP = Path("/root/phone-harness")
CLIENT_TIMEOUT = 25


class ClientFailure(Exception):
    def __init__(self, code: str, message: str, *, request_id: str | None = None):
        super().__init__(message)
        self.code = code
        self.request_id = request_id


def run_client(tool: str, arguments: dict, app: Path = DEFAULT_APP, *, popen_factory=subprocess.Popen) -> dict:
    if tool not in {"daxqo_submit", "daxqo_status", "daxqo_continue", "daxqo_stop"}:
        raise ClientFailure("invalid_tool", "Unsupported DAXQO task tool")
    if not isinstance(arguments, dict):
        raise ClientFailure("invalid_args", "args must be a JSON object")
    request_id = arguments.get("requestId") if isinstance(arguments.get("requestId"), str) else None
    app = Path(app)
    installed_server = app / "assistant_mcp_server.py"
    try:
        server = installed_server if installed_server.is_file() else SERVER
        if not server.is_file():
            raise ClientFailure("unavailable", "DAXQO MCP server file was not found in the app or development workspace")
    except OSError:
        raise ClientFailure("unavailable", "DAXQO MCP server path is not accessible; select an accessible installation with --app") from None
    payload = "\n".join((
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}, ensure_ascii=False),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}, ensure_ascii=False),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": tool, "arguments": arguments}}, ensure_ascii=False),
        "",
    ))
    try:
        process = popen_factory([sys.executable, str(server), "--app", str(app)], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8")
        stdout, _ = process.communicate(payload, timeout=CLIENT_TIMEOUT)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
            process.communicate(timeout=2)
        except Exception:
            pass
        raise ClientFailure("timeout", "MCP call timed out; delivery may be unknown. Keep and reuse the same requestId before retrying.", request_id=request_id) from None
    except OSError:
        raise ClientFailure("unavailable", "Could not start the local DAXQO MCP server") from None
    if process.returncode != 0:
        raise ClientFailure("unavailable", "Local DAXQO MCP server could not initialize; check the app path and pairing")
    try:
        replies = [json.loads(line) for line in stdout.splitlines() if line.strip()]
        response = next(item for item in replies if isinstance(item, dict) and item.get("id") == 2)
    except (ValueError, StopIteration):
        raise ClientFailure("invalid_response", "MCP server returned no valid tool response") from None
    if "error" in response:
        message = response.get("error", {}).get("message", "MCP request failed") if isinstance(response.get("error"), dict) else "MCP request failed"
        raise ClientFailure("mcp_error", str(message)[:400], request_id=request_id)
    result = response.get("result")
    if not isinstance(result, dict) or result.get("isError") is True:
        text = "MCP tool call failed"
        content = result.get("content") if isinstance(result, dict) else None
        if isinstance(content, list) and content and isinstance(content[0], dict):
            text = str(content[0].get("text", text))[:400]
        raise ClientFailure("tool_error", text, request_id=request_id)
    content = result.get("content")
    if not isinstance(content, list):
        raise ClientFailure("invalid_response", "MCP tool returned an invalid result")
    try:
        return json.loads(next(block["text"] for block in content if isinstance(block, dict) and block.get("type") == "text"))
    except (ValueError, StopIteration, KeyError):
        raise ClientFailure("invalid_response", "MCP tool returned no valid JSON result") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tool", choices=("daxqo_submit", "daxqo_status", "daxqo_continue", "daxqo_stop"))
    parser.add_argument("--args", required=True, help="Tool arguments as a JSON object")
    parser.add_argument("--app", type=Path, default=DEFAULT_APP)
    args = parser.parse_args(argv)
    try:
        value = json.loads(args.args)
        result = run_client(args.tool, value, args.app)
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0
    except (ValueError, ClientFailure) as exc:
        if isinstance(exc, ClientFailure):
            error = {"error": exc.code, "message": str(exc)}
            if exc.request_id is not None:
                error["requestId"] = exc.request_id
        else:
            error = {"error": "invalid_args", "message": "--args must contain a valid JSON object"}
        print(json.dumps(error, ensure_ascii=False, separators=(",", ":")))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
