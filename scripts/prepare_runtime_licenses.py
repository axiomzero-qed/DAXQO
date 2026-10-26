#!/usr/bin/env python3
"""Fetch pinned runtime source archives and stage their license notices."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import tempfile
import urllib.request
import zipfile
import tarfile

ROOT = Path(__file__).resolve().parent.parent
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
SOURCES = {
    "proot": {
        "filename": "proot-v5.1.107.101.zip",
        "url": "https://github.com/termux/proot/archive/refs/tags/v5.1.107.101.zip",
        "sha256": "a66743f50c7b0bac1913b83bf55e139d679c80a0ca2808eec76432a65640017f",
        "license_member": "proot-5.1.107.101/COPYING",
        "license_path": "licenses/proot/COPYING",
        "license": "GPL-2.0-or-later",
    },
    "libtalloc": {
        "filename": "talloc-2.5.0.tar.gz",
        "url": "https://www.samba.org/ftp/talloc/talloc-2.5.0.tar.gz",
        "sha256": "912afa237510ae542a7733998eb18a12bcda35ab6729c8e2ddb43e8d0ebab007",
        "license_member": "talloc-2.5.0/LICENSE",
        "license_path": "licenses/libtalloc/LICENSE",
        "license": "LGPL-3.0-or-later (library source)",
    },
    "libandroid-shmem": {
        "filename": "libandroid-shmem-v0.7.tar.gz",
        "url": "https://github.com/termux/libandroid-shmem/archive/refs/tags/v0.7.tar.gz",
        "sha256": "1e5ff8459bc0a8c229dd8a94b27d119987e09ef3414331c2b5ebfff20b98e867",
        "license_member": "libandroid-shmem-0.7/LICENSE",
        "license_path": "licenses/libandroid-shmem/LICENSE",
        "license": "BSD-3-Clause",
    },
}
CODEX_FILES = {
    "codex-LICENSE": {
        "url": "https://raw.githubusercontent.com/openai/codex/rust-v0.163.0/LICENSE",
        "sha256": "d17f227e4df5da1600391338865ce0f3055211760a36688f816941d58232d8dc",
        "output": "licenses/codex/LICENSE",
    },
    "codex-NOTICE": {
        "url": "https://raw.githubusercontent.com/openai/codex/rust-v0.163.0/NOTICE",
        "sha256": "9d71575ecfd9a843fc1677b0efb08053c6ba9fd686a0de1a6f5382fd3c220915",
        "output": "licenses/codex/NOTICE",
    },
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch_pinned(cache: Path, filename: str, url: str, expected: str, opener) -> Path:
    path = cache / filename
    if path.is_file() and sha256_file(path) == expected:
        return path
    partial = path.with_suffix(path.suffix + ".download")
    try:
        with opener.open(url, timeout=60) as response, partial.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
        actual = sha256_file(partial)
        if actual != expected:
            raise ValueError(f"SHA-256 mismatch for {filename}: {actual}")
        partial.replace(path)
        return path
    finally:
        partial.unlink(missing_ok=True)


def safe_member(name: str) -> None:
    path = PurePosixPath(name)
    if path.is_absolute() or "\\" in name or any(part in ("", ".", "..") for part in path.parts):
        raise ValueError(f"unsafe source archive member: {name}")


def extract_license(archive: Path, member_name: str, output: Path) -> None:
    safe_member(member_name)
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as source:
            info = source.getinfo(member_name)
            if info.file_size > 1024 * 1024:
                raise ValueError(f"license file is unexpectedly large: {member_name}")
            data = source.read(info)
    else:
        with tarfile.open(archive, "r:gz") as source:
            member = source.getmember(member_name)
            if not member.isfile() or member.size > 1024 * 1024:
                raise ValueError(f"invalid license member: {member_name}")
            stream = source.extractfile(member)
            if stream is None:
                raise ValueError(f"cannot read license member: {member_name}")
            data = stream.read()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(data)


def copy_python_notice(rootfs_zip: Path, licenses: Path) -> dict:
    # Ubuntu's package copyright file carries Python's PSF terms and Debian notices.
    member = "usr/share/doc/python3.12/copyright"
    with zipfile.ZipFile(rootfs_zip) as bundle:
        index = json.loads(bundle.read("bundle-index.json"))
        files = {entry["path"] for entry in index["entries"] if entry["type"] == "file"}
        if member not in files:
            raise ValueError(f"Ubuntu Python package notice is missing from rootfs: {member}")
        data = bundle.read(member)
    target = licenses / "python" / "copyright"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return {"path": "licenses/python/copyright", "source_member": member,
            "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def prepare(args) -> dict:
    cache = Path(args.source_dir).resolve()
    output = Path(args.output_dir).resolve()
    release = Path(args.release_dir).resolve()
    rootfs_zip = Path(args.rootfs_zip).resolve()
    cache.mkdir(parents=True, exist_ok=True)
    licenses = output / "assets" / "runtime" / "licenses"
    licenses.mkdir(parents=True, exist_ok=True)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    sources = {}
    for name, item in SOURCES.items():
        archive = fetch_pinned(cache, item["filename"], item["url"], item["sha256"], opener)
        extract_license(archive, item["license_member"], output / "assets" / "runtime" / item["license_path"])
        sources[name] = {"url": item["url"], "sha256": item["sha256"], "filename": item["filename"],
                         "license": item["license"], "license_member": item["license_member"],
                         "license_sha256": sha256_file(output / "assets" / "runtime" / item["license_path"])}
    codex_files = {}
    for filename, item in CODEX_FILES.items():
        path = fetch_pinned(cache, filename, item["url"], item["sha256"], opener)
        target = output / "assets" / "runtime" / item["output"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        codex_files[filename] = {"url": item["url"], "sha256": item["sha256"], "output": item["output"]}
    python_notice = copy_python_notice(rootfs_zip, licenses)
    manifest = {"schema": "daxqo.runtime-sources/1", "codex": {"version": "0.163.0", "license": "Apache-2.0", "files": codex_files},
                "python": {"version": "3.12", "license": "PSF-2.0; Ubuntu package notices", **python_notice},
                "native_runtime": sources,
                "ubuntu": {"rootfs_archive": "runtime/rootfs.zip", "notices": "Package-specific copyright files remain under /usr/share/doc/*/copyright in the bundled Ubuntu rootfs."}}
    manifest_path = licenses / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    release.mkdir(parents=True, exist_ok=True)
    archive_path = release / f"DAXQO-{VERSION}-runtime-sources.zip"
    with tempfile.NamedTemporaryFile(prefix="runtime-sources-", suffix=".zip", dir=release, delete=False) as tmp:
        temp_path = Path(tmp.name)
    try:
        with zipfile.ZipFile(temp_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
            for name, item in SOURCES.items():
                bundle.write(cache / item["filename"], f"sources/{item['filename']}")
            bundle.write(cache / "codex-LICENSE", "licenses/codex/LICENSE")
            bundle.write(cache / "codex-NOTICE", "licenses/codex/NOTICE")
            bundle.write(manifest_path, "SOURCES-MANIFEST.json")
        temp_path.replace(archive_path)
    finally:
        temp_path.unlink(missing_ok=True)
    result = {"license_manifest": str(manifest_path), "source_bundle": str(archive_path),
              "source_bundle_bytes": archive_path.stat().st_size,
              "source_bundle_sha256": sha256_file(archive_path), "sources": sources,
              "codex_files": codex_files, "python_notice": python_notice}
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", default=str(ROOT / ".tools" / "offline-inputs" / "runtime-sources"))
    parser.add_argument("--output-dir", default=str(ROOT / ".tools" / "offline-runtime"))
    parser.add_argument("--release-dir", default=str(ROOT / "dist" / f"v{VERSION}"))
    parser.add_argument("--rootfs-zip", default=str(ROOT / ".tools" / "offline-runtime" / "assets" / "runtime" / "rootfs.zip"))
    args = parser.parse_args()
    prepare(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
