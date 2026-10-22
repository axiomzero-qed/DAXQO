"""Signed, replay-resistant DAXQO remote command payloads."""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import re
from typing import Any

UTC = dt.timezone.utc
MAX_AGE_SECONDS = 600
MAX_FUTURE_SECONDS = 120
MAX_REPORT_JSON = 8000
NONCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$", re.ASCII)
EMAIL_LOCAL_RE = re.compile(r"^[a-z0-9.!#$%&'*+/=?^_`{|}~-]+$", re.ASCII)
EMAIL_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$", re.ASCII)
BASE_FIELDS = {"v", "action", "contact", "nonce", "sentAt", "signature"}
ACTION_FIELDS = {
    "submit": {"prompt", "title"},
    "status": {"taskId"},
    "stop": {"taskId"},
    "result": {"report"},
}


def _json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                          allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise ValueError("Invalid remote command payload") from None


def canonical(payload: dict) -> bytes:
    if not isinstance(payload, dict):
        raise ValueError("Invalid remote command payload")
    return _json_bytes({key: value for key, value in payload.items() if key != "signature"})


def _secret_bytes(secret: str) -> bytes:
    if not isinstance(secret, str) or not secret or len(secret) > 512:
        raise ValueError("Invalid remote command signature")
    try:
        return secret.encode("utf-8")
    except UnicodeError:
        raise ValueError("Invalid remote command signature") from None


def sign(payload: dict, secret: str) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Invalid remote command payload")
    result = dict(payload)
    result.pop("signature", None)
    digest = hmac.new(_secret_bytes(secret), canonical(result), hashlib.sha256).hexdigest()
    result["signature"] = digest
    return result


def normalize_email(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 254 or value != value.strip() or not value.isascii():
        raise ValueError("Invalid remote command contact")
    normalized = value.lower()
    if value != normalized or value.count("@") != 1:
        raise ValueError("Invalid remote command contact")
    local, domain = normalized.rsplit("@", 1)
    if (not local or len(local) > 64 or local.startswith(".") or local.endswith(".") or ".." in local
            or not EMAIL_LOCAL_RE.fullmatch(local) or not EMAIL_DOMAIN_RE.fullmatch(domain)):
        raise ValueError("Invalid remote command contact")
    return normalized


def _validate_timestamp(value: Any, now: dt.datetime | None, *, check_window: bool = True) -> None:
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        raise ValueError("Invalid remote command timestamp")
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        parsed = parsed.astimezone(UTC)
    except (ValueError, OverflowError):
        raise ValueError("Invalid remote command timestamp") from None
    if not check_window:
        return
    try:
        if now is None:
            current = dt.datetime.now(UTC)
        elif isinstance(now, dt.datetime) and now.tzinfo is not None and now.utcoffset() is not None:
            current = now.astimezone(UTC)
        else:
            raise ValueError
        delta = (current - parsed).total_seconds()
    except (ValueError, OverflowError):
        raise ValueError("Invalid remote command timestamp") from None
    if delta > MAX_AGE_SECONDS or delta < -MAX_FUTURE_SECONDS:
        raise ValueError("Remote command timestamp is outside the allowed window")


def _validate_fields(payload: dict) -> None:
    if type(payload.get("v")) is not int or payload["v"] != 1:
        raise ValueError("Unsupported remote command version")
    action = payload.get("action")
    if not isinstance(action, str) or action not in ACTION_FIELDS:
        raise ValueError("Unsupported remote command action")
    allowed = BASE_FIELDS | ACTION_FIELDS[action]
    if any(not isinstance(key, str) or key not in allowed for key in payload):
        raise ValueError("Unexpected remote command field")
    for field in ("v", "action", "contact", "nonce", "sentAt", "signature"):
        if field not in payload:
            raise ValueError("Missing remote command field")
    normalize_email(payload["contact"])
    nonce = payload["nonce"]
    if not isinstance(nonce, str) or not NONCE_RE.fullmatch(nonce):
        raise ValueError("Invalid remote command nonce")
    _validate_timestamp(payload["sentAt"], None, check_window=False)
    if action == "submit":
        prompt = payload.get("prompt")
        if not isinstance(prompt, str) or not 1 <= len(prompt) <= 8000:
            raise ValueError("Invalid remote command prompt")
        title = payload.get("title")
        if title is not None and (not isinstance(title, str) or len(title) > 100):
            raise ValueError("Invalid remote command title")
    elif action == "status":
        if "taskId" in payload and not _valid_task_id(payload["taskId"]):
            raise ValueError("Invalid remote command task ID")
    elif action == "stop":
        if not _valid_task_id(payload.get("taskId")):
            raise ValueError("Remote stop requires a valid task ID")
    elif action == "result":
        report = payload.get("report")
        if not isinstance(report, dict) or len(_json_bytes(report)) > MAX_REPORT_JSON:
            raise ValueError("Invalid remote command report")
    signature = payload.get("signature")
    if not isinstance(signature, str) or not re.fullmatch(r"[0-9a-f]{64}", signature):
        raise ValueError("Invalid remote command signature")


def _valid_task_id(value: Any) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= 200 and value.isascii() and all(
        char.isalnum() or char in "-_" for char in value)


def verify(payload: dict, secret: str, now: dt.datetime | None = None) -> dict:
    """Validate a remote command and return the payload without its signature."""
    if not isinstance(payload, dict):
        raise ValueError("Invalid remote command payload")
    _validate_fields(payload)
    _validate_timestamp(payload["sentAt"], now)
    try:
        expected = hmac.new(_secret_bytes(secret), canonical(payload), hashlib.sha256).hexdigest()
    except ValueError:
        raise ValueError("Invalid remote command signature") from None
    if not hmac.compare_digest(payload["signature"], expected):
        raise ValueError("Invalid remote command signature")
    return {key: value for key, value in payload.items() if key != "signature"}
