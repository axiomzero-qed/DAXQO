"""Opt-in USB regression: only edits the isolated io.daxqo.inputfixture app."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "io.daxqo.inputfixture"


class Client:
    def __init__(self):
        self.process = subprocess.Popen(
            [sys.executable, "-u", str(ROOT / "scripts/device_client.py")], cwd=ROOT,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", env={**os.environ, "DAXQO_PAIRING_SOURCE": "android-debug"})
        if not json.loads(self.process.stdout.readline()).get("ready"):
            raise RuntimeError("Device client unavailable")

    def request(self, **command):
        self.process.stdin.write(json.dumps(command, ensure_ascii=True) + "\n")
        self.process.stdin.flush()
        value = json.loads(self.process.stdout.readline())
        if "error_type" in value or "http_status" in value:
            raise RuntimeError("Device request failed: " + str(value.get("error_type", value.get("http_status"))))
        return value

    def action(self, action, args=None, ident=None):
        return self.request(target="android", path="/api/device/execute", body={
            "id": ident or str(uuid.uuid4()), "action": action,
            "args": {"expectedPackage": PACKAGE, **(args or {})}})

    def tree(self):
        response = self.action("tree")
        if not response.get("ok"):
            raise RuntimeError("Observe failed: " + str(response.get("error", {}).get("code")))
        return response["result"]

    def close(self):
        if self.process.poll() is None:
            self.process.stdin.write('{"action":"exit"}\n')
            self.process.stdin.flush()
            self.process.wait(timeout=10)


def main():
    if not os.environ.get("DAXQO_SERIAL"):
        raise SystemExit("Set DAXQO_SERIAL to the explicitly selected test phone.")
    adb = [os.environ.get("DAXQO_ADB", str(ROOT / ".tools/android-sdk/platform-tools/adb.exe")),
           "-s", os.environ["DAXQO_SERIAL"]]
    client = Client()
    report = []
    destination = ROOT / "dist/input-debug/device-regression.json"
    try:
        status = client.request(path="/api/status")
        if status.get("activeTaskId") or status.get("activeTaskIds"):
            raise RuntimeError("An owner task is active; testing stopped without changing it.")
        native = client.request(target="android", path="/api/device/status")
        if not native.get("cursorInputMethodSupported"):
            raise RuntimeError("Install the updated DAXQO APK with the input-method bridge first.")
        cases = [
            ("native-middle", "中文", "insert", "甲中文乙", False),
            ("native-selection", "中文", "insert", "甲中文丙", False),
            ("native-emoji", "中文", "insert", "A😀中文B", False),
            ("web-middle", "中文", "insert", "甲中文乙", False),
            ("web-contenteditable", "中文", "insert", "甲中文乙", False),
            ("native-middle", "中文\n测试😀", "insert", "甲中文\n测试😀乙", True),
            ("native-middle", "中文\n测试😀", "replace", "中文\n测试😀", True),
        ]
        for scenario, text, mode, expected, addressed in cases:
            subprocess.run(adb + ["shell", "am", "start", "-S", "-W", "-n", PACKAGE + "/.MainActivity",
                                  "--es", "scenario", scenario], check=True, capture_output=True, timeout=20)
            deadline = time.monotonic() + 8
            observed = None
            while time.monotonic() < deadline:
                observed = client.tree()
                targets = [n for n in observed["nodes"] if n.get("editable") and n.get("focused")]
                if targets:
                    break
                time.sleep(.2)
            if not targets:
                raise AssertionError("Fixture editor not focused: " + scenario)
            args = {"text": text, "mode": mode}
            if addressed:
                args.update(nodeId=targets[0]["id"], snapshotId=observed["snapshotId"])
            ident = str(uuid.uuid4())
            started = time.monotonic()
            response = client.action("input", args, ident)
            if not response.get("ok"):
                raise AssertionError({"scenario": scenario, "response": response})
            result = response["result"]
            if mode == "insert" and result.get("method") != "input_connection":
                raise AssertionError("The native Unicode input connection was not used: " + scenario)
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                after = client.tree()
                if any(n.get("text") == expected for n in after["nodes"]):
                    break
                time.sleep(.15)
            else:
                visible = [{k: n.get(k) for k in ("id", "text", "editable", "focused", "selectionStart", "selectionEnd")}
                           for n in after["nodes"] if n.get("editable") or n.get("focused")]
                raise AssertionError({"scenario": scenario, "expected": expected, "editors": visible})
            # A repeated HTTP action ID must not commit the same characters twice.
            if client.action("input", args, ident) != response:
                raise AssertionError("Action deduplication changed the response")
            after = client.tree()
            assert any(n.get("text") == expected for n in after["nodes"]), "Duplicate input was applied"
            if addressed:
                stale = client.action("input", {**args, "text": "不得写入"})
                assert not stale.get("ok") and stale["error"]["code"] == "stale_snapshot", stale
                assert any(n.get("text") == expected for n in client.tree()["nodes"])
            item = {"scenario": scenario, "mode": mode, "addressed": addressed,
                    "method": result["method"], "text": expected,
                    "elapsedMs": round((time.monotonic() - started) * 1000), "passed": True}
            report.append(item)
            print(json.dumps(item, ensure_ascii=True), flush=True)
    finally:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        client.close()
    print("PASS: real-device Unicode input, selection, WebView, action deduplication and stale targets")


if __name__ == "__main__":
    main()
