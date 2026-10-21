"""Report tool argument shapes from one explicitly selected local test session.

Does not print message bodies, model reasoning, credentials, or other sessions.
"""
import argparse
import json
import os
from pathlib import Path
import uuid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("thread_id", type=lambda value: str(uuid.UUID(value)))
    args = parser.parse_args()
    folder = Path(os.environ.get("CODEX_HOME", "/root/phone-harness/state/codex")) / "sessions"
    matches = list(folder.rglob("*" + args.thread_id + "*.jsonl"))
    result = {"sessionCount": len(matches), "spawnCalls": []}
    for path in matches:
        with path.open(encoding="utf-8") as source:
            for line in source:
                if '"function_call"' not in line and '"custom_tool_call"' not in line:
                    continue
                record = json.loads(line)
                payload = record.get("payload", {})
                name = payload.get("name", "")
                if "spawn" not in name.lower():
                    continue
                raw = payload.get("arguments", payload.get("input", {}))
                try:
                    body = json.loads(raw) if isinstance(raw, str) else raw
                except ValueError:
                    body = {}
                if not isinstance(body, dict):
                    continue
                shape = {"name": name, "keys": list(body)}
                for key in ("message", "prompt", "task"):
                    value = body.get(key)
                    if isinstance(value, str):
                        shape[key] = {"length": len(value), "mathTask": "17" in value and "25" in value,
                                      "reverseTask": "LOCAL" in value}
                result["spawnCalls"].append(shape)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
