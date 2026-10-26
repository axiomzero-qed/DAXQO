#!/usr/bin/env python3
"""Fetch the two checksum-pinned inputs used only while building the offline APK."""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
INPUTS = (
    ("noble-server-cloudimg-arm64-root.tar.xz",
     "https://mirrors.ustc.edu.cn/ubuntu-cloud-images/noble/20261017/noble-server-cloudimg-arm64-root.tar.xz",
     "b68ef0444ca56ce4f00ae7e8ccfb0c553ffbc92e4a1b14ed7dd8aa18c6d68652"),
    ("codex-0.163.0-linux-arm64.tgz",
     "https://registry.npmmirror.com/@openai/codex/-/codex-0.163.0-linux-arm64.tgz",
     "dc6e7c4d6de71c23a0f60d05e2577ff7eef615b76f22630d58ad384b84763522"),
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch_one(output_dir: Path, name: str, url: str, expected_sha256: str, opener) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / name
    if target.is_file() and not target.is_symlink() and sha256_file(target) == expected_sha256:
        return target
    partial_path = None
    try:
        with tempfile.NamedTemporaryFile(prefix=f".{name}.", suffix=".download", dir=output_dir, delete=False) as tmp:
            partial_path = Path(tmp.name)
            digest = hashlib.sha256()
            with opener.open(url, timeout=120) as response:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    tmp.write(chunk)
                    digest.update(chunk)
        if digest.hexdigest() != expected_sha256:
            raise ValueError(f"Checksum verification failed for pinned build input: {name}")
        partial_path.replace(target)
        return target
    finally:
        if partial_path is not None:
            partial_path.unlink(missing_ok=True)


def fetch_inputs(output_dir: Path, inputs=INPUTS, opener=None) -> list[Path]:
    output_dir = output_dir.expanduser().resolve()
    if opener is None:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return [fetch_one(output_dir, name, url, checksum, opener) for name, url, checksum in inputs]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist" / "deploy-cn" / "packages",
                        help="Verified build-input cache directory")
    args = parser.parse_args(argv)
    try:
        paths = fetch_inputs(args.output_dir)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print("Verified pinned offline build inputs:")
    for path in paths:
        print(f"  {path.name} ({path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
