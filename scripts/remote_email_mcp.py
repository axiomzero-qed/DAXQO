#!/usr/bin/env python3
"""MCP stdio client that sends signed DAXQO commands by SMTP over SSL."""
from __future__ import annotations

import argparse
import datetime as dt
from email.message import EmailMessage
from email.utils import make_msgid
import json
import os
from pathlib import Path
import re
import smtplib
import ssl
import stat
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from remote_protocol import normalize_email, sign, verify  # noqa: E402

MAX_LINE = 1024 * 1024
CONFIG_KEYS = {"deviceEmail", "contactEmail", "contactSecret", "smtpHost", "smtpPort", "username", "password"}


def load_config(path: str | os.PathLike, *, require_smtp: bool) -> dict:
    config_path = Path(path)
    try:
        if config_path.is_symlink() or not config_path.is_file():
            raise ValueError("Invalid configuration file")
        mode = stat.S_IMODE(config_path.stat().st_mode)
        if os.name != "nt" and mode & 0o077:
            raise ValueError("Configuration file must have mode 600")
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except ValueError:
        raise
    except Exception:
        raise ValueError("Unable to read configuration file") from None
    if not isinstance(data, dict) or set(data) - CONFIG_KEYS:
        raise ValueError("Invalid configuration fields")
    try:
        data["deviceEmail"] = normalize_email(data.get("deviceEmail"))
        data["contactEmail"] = normalize_email(data.get("contactEmail"))
    except ValueError:
        raise ValueError("Invalid configured email address") from None
    secret = data.get("contactSecret")
    if not isinstance(secret, str) or not 32 <= len(secret) <= 512 or not secret.isascii():
        raise ValueError("Invalid configured contact secret")
    smtp_fields = {"smtpHost", "smtpPort", "username", "password"}
    present = smtp_fields & set(data)
    if require_smtp or present:
        if present != smtp_fields:
            raise ValueError("SMTP configuration is incomplete")
        host, username, password = data["smtpHost"], data["username"], data["password"]
        if (not isinstance(host, str) or not host or len(host) > 253 or not host.isascii()
                or any(ch in host for ch in "\r\n/\\")
                or type(data["smtpPort"]) is not int or data["smtpPort"] != 465
                or not isinstance(username, str) or not username or len(username) > 254
                or any(ch in username for ch in "\r\n")
                or not isinstance(password, str) or not password or len(password) > 2048
                or any(ch in password for ch in "\r\n")):
            raise ValueError("Invalid SMTP configuration")
    return data


def make_payload(config: dict, action: str, fields: dict, *, now=None, nonce=None) -> dict:
    clock = now or dt.datetime.now(dt.timezone.utc)
    if clock.tzinfo is None or clock.utcoffset() is None:
        raise ValueError("Invalid clock")
    payload = {
        "v": 1,
        "action": action,
        "contact": config["contactEmail"],
        "nonce": nonce or uuid.uuid4().hex,
        "sentAt": clock.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        **fields,
    }
    signed = sign(payload, config["contactSecret"])
    verify(signed, config["contactSecret"], now=clock)
    return signed


class RemoteEmailMCP:
    def __init__(self, config: dict, smtp_factory=None):
        self.config = config
        self.smtp_factory = smtp_factory or smtplib.SMTP_SSL

    def send(self, action: str, fields: dict) -> dict:
        payload = make_payload(self.config, action, fields)
        message = EmailMessage()
        message["From"] = self.config["contactEmail"]
        message["To"] = self.config["deviceEmail"]
        message["Subject"] = "DAXQO/1 " + payload["nonce"]
        message["Message-ID"] = make_msgid()
        message.set_content(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
        try:
            with self.smtp_factory(
                self.config["smtpHost"], 465,
                context=ssl.create_default_context(), timeout=20,
            ) as smtp:
                smtp.login(self.config["username"], self.config["password"])
                smtp.send_message(message)
        except Exception:
            raise ValueError("SMTP send failed; delivery and task completion are unconfirmed") from None
        return {
            "nonce": payload["nonce"],
            "messageId": message["Message-ID"],
            "delivery": "sent",
            "notice": "SMTP accepted the message; delivery and task completion are unconfirmed.",
        }

    def call(self, name: str, args: dict) -> dict:
        if not isinstance(args, dict):
            raise ValueError("Tool arguments must be an object")
        if name == "remote_submit":
            if set(args) - {"prompt", "title"}:
                raise ValueError("Unexpected tool argument")
            fields = {"prompt": args.get("prompt")}
            if "title" in args:
                fields["title"] = args["title"]
            return self.send("submit", fields)
        if name == "remote_status":
            if set(args) - {"taskId"}:
                raise ValueError("Unexpected tool argument")
            fields = {"taskId": args["taskId"]} if "taskId" in args else {}
            return self.send("status", fields)
        if name == "remote_stop":
            if set(args) != {"taskId"}:
                raise ValueError("remote_stop requires taskId")
            return self.send("stop", {"taskId": args["taskId"]})
        raise ValueError("Unknown remote email tool")


TOOLS = [
    {"name": "remote_submit", "description": "Email a signed task request to the configured DAXQO device. Success means only that SMTP accepted the message.", "inputSchema": {"type": "object", "properties": {"prompt": {"type": "string", "minLength": 1, "maxLength": 8000}, "title": {"type": "string", "maxLength": 100}}, "required": ["prompt"], "additionalProperties": False}},
    {"name": "remote_status", "description": "Email a signed request for device/task status.", "inputSchema": {"type": "object", "properties": {"taskId": {"type": "string", "maxLength": 200}}, "additionalProperties": False}},
    {"name": "remote_stop", "description": "Email a signed request to stop one task.", "inputSchema": {"type": "object", "properties": {"taskId": {"type": "string", "minLength": 1, "maxLength": 200}}, "required": ["taskId"], "additionalProperties": False}},
]


def _rpc_response(req: dict, result=None, error=None) -> dict | None:
    if "id" not in req:  # JSON-RPC notification
        return None
    response = {"jsonrpc": "2.0", "id": req["id"]}
    if error is not None:
        response["error"] = error
    else:
        response["result"] = result
    return response


def serve_stdio(client: RemoteEmailMCP, input_stream=None, output_stream=None) -> None:
    source = input_stream or sys.stdin
    sink = output_stream or sys.stdout
    for line in source:
        if len(line.encode("utf-8")) > MAX_LINE:
            continue
        req = None
        try:
            req = json.loads(line)
            if not isinstance(req, dict) or req.get("jsonrpc") != "2.0" or not isinstance(req.get("method"), str):
                raise ValueError("Invalid JSON-RPC request")
            method = req["method"]
            params = req.get("params") or {}
            if not isinstance(params, dict):
                raise ValueError("Invalid JSON-RPC params")
            if method == "initialize":
                result = {"protocolVersion": params.get("protocolVersion", "2024-11-05"), "capabilities": {"tools": {}}, "serverInfo": {"name": "daxqo-remote-email", "version": "0.1.0"}}
            elif method == "notifications/initialized":
                continue
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                name, arguments = params.get("name"), params.get("arguments", {})
                try:
                    output = client.call(name, arguments)
                    result = {"content": [{"type": "text", "text": json.dumps(output, ensure_ascii=False)}], "isError": False}
                except ValueError as exc:
                    result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
            else:
                if "id" not in req:
                    continue
                response = _rpc_response(req, error={"code": -32601, "message": "Method not found"})
                sink.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
                sink.flush()
                continue
            response = _rpc_response(req, result=result)
        except Exception:
            if req is None:
                continue
            error = {"code": -32600, "message": "Invalid request"}
            if isinstance(req, dict):
                response = _rpc_response(req, error=error)
            else:
                response = {"jsonrpc": "2.0", "id": None, "error": error}
        if response is not None:
            sink.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
            sink.flush()


def _print_command(config: dict, args) -> None:
    fields = {}
    if args.command == "submit":
        fields["prompt"] = args.prompt
        if args.title is not None:
            fields["title"] = args.title
    elif args.command in {"status", "stop"}:
        if args.task_id is not None:
            fields["taskId"] = args.task_id
    elif args.command == "result":
        fields["report"] = json.loads(args.report)
    payload = make_payload(config, args.command, fields)
    sys.stdout.write(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--print-command", choices=("submit", "status", "stop", "result"))
    parser.add_argument("--prompt")
    parser.add_argument("--title")
    parser.add_argument("--task-id")
    parser.add_argument("--report", default="{}")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config, require_smtp=args.print_command is None)
        if args.print_command:
            args.command = args.print_command
            _print_command(config, args)
        else:
            serve_stdio(RemoteEmailMCP(config))
        return 0
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
