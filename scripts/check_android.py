#!/usr/bin/env python3
"""Run host Java regressions and inspect an already-built debug APK.

Requires JDK 17 and Android SDK 35. These checks never connect to a phone.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess


# Preserve an ASCII subst drive when Windows builds live under a Unicode path.
ROOT = Path(__file__).absolute().parents[1]
ANDROID = ROOT / "android"


def run(command: list[str | Path], *, capture: bool = False) -> str:
    result = subprocess.run([str(item) for item in command], check=True,
                            stdout=subprocess.PIPE if capture else None,
                            stderr=subprocess.PIPE if capture else None,
                            encoding="utf-8", errors="replace")
    return (result.stdout or "").strip()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=("standard", "xm"), default="standard")
    parser.add_argument("--java-home", type=Path, default=os.environ.get("JAVA_HOME"))
    parser.add_argument("--sdk-root", type=Path,
                        default=os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT"))
    args = parser.parse_args()
    require(args.sdk_root is not None, "Set ANDROID_HOME or pass --sdk-root.")
    sdk = Path(args.sdk_root)
    suffix = ".exe" if os.name == "nt" else ""
    java = Path(args.java_home) / "bin" / ("java" + suffix) if args.java_home else shutil.which("java")
    javac = Path(args.java_home) / "bin" / ("javac" + suffix) if args.java_home else shutil.which("javac")
    require(bool(java and javac), "JDK 17 must be available.")
    android_jar = sdk / "platforms/android-35/android.jar"
    classes = ANDROID / "app/build/intermediates/javac/debug/compileDebugJavaWithJavac/classes"
    require((classes / "io/daxqo/app/ApprovalLedger.class").is_file(),
            "Run assembleDebug before the Android checks.")
    require(android_jar.is_file(), "Android SDK platform 35 is required.")
    output = ANDROID / "app/build/host-checks"
    output.mkdir(parents=True, exist_ok=True)
    source = ANDROID / "app/src/main/java/io/daxqo/app"
    tests = ANDROID / "tests"
    suites = (
        ("PlatformProfileRegression", [source / "platform/PlatformProfile.java"]),
        ("io.daxqo.app.NodeSnapshotRegression", [source / "NodeSnapshot.java"]),
        ("io.daxqo.app.TextInputRegression", [source / "TextInputEdit.java"]),
        ("io.daxqo.app.ApprovalLedgerRegression", []),
        ("io.daxqo.app.PassiveCallbacksRegression", []),
    )
    compile_path = os.pathsep.join(map(str, (classes, android_jar)))
    runtime_path = os.pathsep.join(map(str, (output, classes, android_jar)))
    for name, extra_sources in suites:
        run([javac, "-encoding", "UTF-8", "-cp", compile_path, "-d", output,
             *extra_sources, tests / (name.rsplit(".", 1)[-1] + ".java")])
        run([java, "-cp", runtime_path, name])

    apk = ANDROID / "app/build/outputs/apk/debug/app-debug.apk"
    tools = sdk / "build-tools/35.0.0"
    package = "io.daxqo.xm" if args.platform == "xm" else "io.daxqo.app"
    scheme = "daxqo-xm" if args.platform == "xm" else "daxqo"
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    if args.platform == "xm":
        version += "-xm"
    badging = run([tools / ("aapt" + suffix), "dump", "badging", apk], capture=True)
    require(re.search(r"^package: name='" + re.escape(package) + r"' versionCode='6' versionName='"
                      + re.escape(version) + "'", badging, re.M) is not None,
            "Packaged app ID/version does not match the selected platform.")
    manifest = run([tools / ("aapt2" + suffix), "dump", "xmltree", apk,
                    "--file", "AndroidManifest.xml"], capture=True)
    require(re.search(r'android:scheme[^\n]*="' + re.escape(scheme) + '"', manifest) is not None
            and re.search(r'android:host[^\n]*="connect"', manifest) is not None,
            "Packaged pairing link does not match the selected platform.")
    blocks = re.findall(r"(?ms)^\s*E: activity\b.*?(?=^\s*E: (?:activity|service|provider|receiver)\b|\Z)", manifest)
    wake = [block for block in blocks if re.search(r'android:name[^\n]*="io\.daxqo\.app\.WakeActivity"', block)]
    require(len(wake) == 1 and all(re.search(pattern, wake[0]) for pattern in (
        r'android:taskAffinity[^\n]*=""', r'android:exported[^\n]*=false',
        r'android:excludeFromRecents[^\n]*=true')),
        "Packaged WakeActivity must be private, task-isolated and absent from Recents.")
    analyzer = sdk / "cmdline-tools/latest/bin" / ("apkanalyzer.bat" if os.name == "nt" else "apkanalyzer")
    for name, expected in (("platform_id", args.platform), ("pairing_scheme", scheme),
                           ("shortcut_target_package", package)):
        value = run([analyzer, "resources", "value", apk, "--config", "default", "--type", "string",
                     "--name", name, "--package", package], capture=True)
        require(value == expected, f"Packaged {name} does not match {args.platform}.")
    print(f"PASS: {args.platform} APK identity, pairing, shortcuts and private wake activity")


if __name__ == "__main__":
    main()
