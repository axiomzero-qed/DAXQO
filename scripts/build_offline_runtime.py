#!/usr/bin/env python3
"""Assemble an offline Android Linux runtime bundle without network access."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import lzma
import os
from pathlib import Path, PurePosixPath
import stat
import tarfile
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parent.parent
ROOTFS_SHA256 = "b68ef0444ca56ce4f00ae7e8ccfb0c553ffbc92e4a1b14ed7dd8aa18c6d68652"
CODEX_SHA256 = "dc6e7c4d6de71c23a0f60d05e2577ff7eef615b76f22630d58ad384b84763522"
CODEX_VERSION = "0.163.0"
CODEX_PREFIX = "package/vendor/aarch64-unknown-linux-musl/"
EXCLUDED_TOP = ("dev", "proc", "sys", "run", "tmp", "root", "home")
EXCLUDED_EXACT = {
    "etc/machine-id", "etc/hostname", "var/lib/dbus/machine-id", "var/lib/cloud", "var/log", "var/cache", "var/tmp",
    "var/lib/systemd/random-seed", "etc/ssh/ssh_host_rsa_key",
    "etc/ssh/ssh_host_rsa_key.pub", "etc/ssh/ssh_host_ecdsa_key",
    "etc/ssh/ssh_host_ecdsa_key.pub", "etc/ssh/ssh_host_ed25519_key",
    "etc/ssh/ssh_host_ed25519_key.pub", "etc/shadow", "etc/shadow-",
    "etc/gshadow", "etc/gshadow-", "etc/NetworkManager/system-connections",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _decompress_to(source: Path, destination: Path, opener) -> None:
    """Spool a compressed tar once so repeated member reads use seekable plain tar."""
    with opener(source, "rb") as src, destination.open("wb") as dst:
        while chunk := src.read(1024 * 1024):
            dst.write(chunk)


def safe_name(name: str) -> str:
    if "\\" in name:
        raise ValueError(f"unsafe archive path contains a backslash: {name!r}")
    name = name.removeprefix("./").lstrip("/")
    path = PurePosixPath(name)
    if not name or path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise ValueError(f"unsafe archive path: {name!r}")
    return path.as_posix()


def excluded(path: str) -> bool:
    parts = PurePosixPath(path).parts
    if not parts:
        return True
    if parts[0] in EXCLUDED_TOP:
        return True
    if path in EXCLUDED_EXACT:
        return True
    if path.startswith(("var/log/", "var/cache/", "var/lib/cloud/", "var/tmp/")):
        return True
    if path.startswith("etc/ssh/ssh_host_"):
        return True
    if path.startswith("etc/NetworkManager/system-connections/"):
        return True
    return False


def add_tree(entries: dict, source: Path, destination: str, predicate=None) -> None:
    if not source.is_dir() or source.is_symlink():
        raise ValueError(f"expected a directory: {source}")
    for path in sorted(source.rglob("*")):
        rel = path.relative_to(source).as_posix()
        if "__pycache__" in Path(rel).parts or path.suffix in (".pyc", ".pyo"):
            continue
        if predicate and not predicate(path, rel):
            continue
        target = f"{destination.rstrip('/')}/{rel}"
        if path.is_symlink():
            entries[target] = {"kind": "symlink", "target": os.readlink(path), "mode": stat.S_IMODE(path.lstat().st_mode)}
        elif path.is_dir():
            entries[target] = {"kind": "dir", "mode": stat.S_IMODE(path.stat().st_mode)}
        elif path.is_file():
            entries[target] = {"kind": "disk", "source": path, "mode": stat.S_IMODE(path.stat().st_mode)}


def add_dir(entries: dict, name: str, mode: int = 0o755) -> None:
    entries.setdefault(name, {"kind": "dir", "mode": mode})


def _tar_entries(rootfs: Path, allow_test_inputs: bool, scratch: Path):
    actual = sha256_file(rootfs)
    if not allow_test_inputs and actual != ROOTFS_SHA256:
        raise ValueError(f"rootfs SHA-256 mismatch: {actual}")
    entries: dict = {}
    plain_tar = scratch / "rootfs.tar"
    _decompress_to(rootfs, plain_tar, lzma.open)
    archive = tarfile.open(plain_tar, "r:")
    try:
        members = archive.getmembers()
        by_name = {}
        for member in members:
            if member.name in (".", "./", "/"):
                continue
            # A small number of Ubuntu systemd unit names contain literal backslashes.
            # They are irrelevant inside PRoot and violate the installed-index path contract.
            if "\\" in member.name:
                continue
            name = safe_name(member.name)
            if excluded(name):
                continue
            if member.isdir():
                by_name[name] = {"kind": "dir", "mode": member.mode & 0o777}
            elif member.isfile():
                by_name[name] = {"kind": "tar", "archive": archive, "member": member, "mode": member.mode & 0o777}
            elif member.issym():
                # Links are metadata only; absolute Linux links are intentionally supported.
                by_name[name] = {"kind": "symlink", "target": member.linkname, "mode": member.mode & 0o777}
            elif member.islnk():
                target = safe_name(member.linkname)
                by_name[name] = {"kind": "hardlink", "target": target, "mode": member.mode & 0o777}
            # Devices, FIFOs, sockets and other special nodes never enter the bundle.
        entries.update(by_name)
        return entries, len(members), archive
    except BaseException:
        archive.close()
        raise


def _add_codex(entries: dict, codex: Path, allow_test_inputs: bool, scratch: Path):
    actual = sha256_file(codex)
    if not allow_test_inputs and actual != CODEX_SHA256:
        raise ValueError(f"Codex SHA-256 mismatch: {actual}")
    plain_tar = scratch / "codex.tar"
    _decompress_to(codex, plain_tar, gzip.open)
    archive = tarfile.open(plain_tar, "r:")
    try:
        found = False
        for member in archive.getmembers():
            raw = member.name
            if not raw.startswith(CODEX_PREFIX):
                continue
            rel = raw[len(CODEX_PREFIX):]
            if not rel:
                continue
            rel = safe_name(rel)
            dest = f"opt/daxqo/runtime/codex-{CODEX_VERSION}/{rel}"
            if member.isdir():
                entries[dest] = {"kind": "dir", "mode": member.mode & 0o777}
            elif member.isfile():
                entries[dest] = {"kind": "codex", "archive": archive, "member": member, "mode": member.mode & 0o777}
            elif member.issym():
                entries[dest] = {"kind": "symlink", "target": member.linkname, "mode": member.mode & 0o777}
            elif member.islnk():
                entries[dest] = {"kind": "hardlink", "target": f"opt/daxqo/runtime/codex-{CODEX_VERSION}/{safe_name(member.linkname.removeprefix(CODEX_PREFIX))}", "mode": member.mode & 0o777}
            if rel == "bin/codex":
                found = True
        if not found:
            raise ValueError("Codex archive is missing bin/codex under the expected ARM64 package layout")
    except BaseException:
        archive.close()
        raise
    return actual, archive


def _source_backend(entries: dict, source: Path) -> None:
    backend = source / "backend"
    if not backend.is_dir():
        raise ValueError(f"source repository has no backend directory: {source}")
    for path in sorted(backend.rglob("*")):
        rel = path.relative_to(backend).as_posix()
        if "__pycache__" in Path(rel).parts or path.suffix in (".pyc", ".pyo"):
            continue
        if path.is_symlink():
            continue
        if path.is_dir():
            if rel == "static" or rel.startswith("static/"):
                entries[f"opt/daxqo/{rel}"] = {"kind": "dir", "mode": stat.S_IMODE(path.stat().st_mode)}
        elif path.is_file() and (path.suffix == ".py" or path.name == "WORKSPACE_AGENTS.md" or rel.startswith("static/") or (path.name.endswith(".json") and "models" in path.name)):
            entries[f"opt/daxqo/{rel}"] = {"kind": "disk", "source": path, "mode": stat.S_IMODE(path.stat().st_mode)}
    for name in ("remote_email_mcp.py", "assistant_client.py", "install_assistant_skill.py"):
        path = source / "scripts" / name
        if path.is_file() and not path.is_symlink():
            entries[f"opt/daxqo/{name}"] = {"kind": "disk", "source": path, "mode": stat.S_IMODE(path.stat().st_mode)}
    license_file = source / "LICENSE"
    if license_file.is_file():
        entries["opt/daxqo/LICENSE"] = {"kind": "disk", "source": license_file, "mode": 0o644}


def _runtime_files(entries: dict, motor: Path, collector: Path, mail: Path) -> None:
    add_tree(entries, motor, "opt/daxqo/motor", lambda p, r: p.is_dir() or p.suffix == ".py")
    add_tree(entries, collector, "opt/daxqo/trajectory", lambda p, r: p.is_dir() or p.suffix == ".py")
    add_tree(entries, mail, "opt/daxqo/runtime/mail-libs")
    for license_file, target in ((motor.parent / "LICENSE", "opt/daxqo/motor/LICENSE"), (collector.parent / "LICENSE", "opt/daxqo/trajectory/LICENSE")):
        if license_file.is_file():
            entries[target] = {"kind": "disk", "source": license_file, "mode": 0o644}
    runtime_json = {"version": CODEX_VERSION, "binary": f"/opt/daxqo/runtime/codex-{CODEX_VERSION}/bin/codex", "persistent_runtime": f"/opt/daxqo/runtime/codex-{CODEX_VERSION}"}
    entries["opt/daxqo/runtime.json"] = {"kind": "bytes", "data": (json.dumps(runtime_json, indent=2) + "\n").encode(), "mode": 0o644}
    for name, mode in (("dev", 0o755), ("proc", 0o555), ("sys", 0o555), ("run", 0o755), ("tmp", 0o1777), ("root", 0o700), ("home", 0o755), ("var/tmp", 0o1777), ("var/log", 0o755), ("var/cache", 0o755), ("opt/daxqo/state", 0o755), ("opt/daxqo/workspace", 0o755), ("opt/daxqo/update-stage", 0o755)):
        add_dir(entries, name, mode)
    entries["etc/resolv.conf"] = {"kind": "bytes", "data": b"# DAXQO fallback DNS; Android LinkProperties may replace this file.\nnameserver 1.1.1.1\nnameserver 223.5.5.5\n", "mode": 0o644}


def _open_data(entry: dict):
    kind = entry["kind"]
    if kind == "disk":
        return entry["source"].open("rb")
    if kind in ("tar", "codex"):
        stream = entry["archive"].extractfile(entry["member"])
        if stream is None:
            raise ValueError(f"cannot read archive member {entry['member'].name}")
        return stream
    if kind == "bytes":
        return io.BytesIO(entry["data"])
    raise ValueError(f"entry is not a file: {kind}")


def _write_bundle(entries: dict, output_zip: Path, version: str) -> tuple[dict, dict]:
    # Populate parents so conventional filesystem extraction creates an intact tree.
    for name in list(entries):
        parent = PurePosixPath(name).parent
        while parent.as_posix() not in (".", ""):
            entries.setdefault(parent.as_posix(), {"kind": "dir", "mode": 0o755})
            parent = parent.parent
    index_entries = []
    unpacked = 0
    file_count = 0
    source_digest = hashlib.sha256()
    with tempfile.TemporaryFile() as spool:
        with zipfile.ZipFile(spool, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as zf:
            for name in sorted(entries):
                entry = entries[name]
                kind = entry["kind"]
                if kind == "dir":
                    index_entries.append({"path": name, "type": "dir", "mode": entry.get("mode", 0o755), "size": 0, "sha256": "", "target": ""})
                elif kind in ("symlink", "hardlink"):
                    typ = kind
                    index_entries.append({"path": name, "type": typ, "mode": entry.get("mode", 0o777 if typ == "symlink" else 0o644), "size": 0, "sha256": "", "target": entry["target"]})
                else:
                    digest = hashlib.sha256()
                    size = 0
                    info = zipfile.ZipInfo(name)
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.external_attr = (stat.S_IFREG | entry.get("mode", 0o644)) << 16
                    with zf.open(info, "w") as out, _open_data(entry) as src:
                        while True:
                            chunk = src.read(1024 * 1024)
                            if not chunk:
                                break
                            out.write(chunk)
                            digest.update(chunk)
                            size += len(chunk)
                    unpacked += size
                    file_count += 1
                    file_hash = digest.hexdigest()
                    if entry["kind"] == "disk":
                        encoded_path = name.encode("utf-8")
                        source_digest.update(len(encoded_path).to_bytes(4, "big"))
                        source_digest.update(encoded_path)
                        source_digest.update(bytes.fromhex(file_hash))
                    index_entries.append({"path": name, "type": "file", "mode": entry.get("mode", 0o644), "size": size, "sha256": file_hash, "target": ""})
            index = {"schema": "daxqo.rootfs/1", "version": version, "entries": index_entries}
            index_bytes = json.dumps(index, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(index_bytes) > 16 * 1024 * 1024:
                raise ValueError("bundle index exceeds the 16 MiB limit")
            # A second pass writes the index as the first ZIP member without holding file data in memory.
            zf.close()
        spool.seek(0)
        with zipfile.ZipFile(output_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as final_zip:
            info = zipfile.ZipInfo("bundle-index.json")
            info.compress_type = zipfile.ZIP_DEFLATED
            final_zip.writestr(info, index_bytes)
            with zipfile.ZipFile(spool, "r") as staged:
                for info in staged.infolist():
                    with staged.open(info) as src, final_zip.open(info, "w") as dst:
                        while chunk := src.read(1024 * 1024):
                            dst.write(chunk)
    return index, {"unpackedBytes": unpacked, "fileCount": len(index_entries), "regularFileCount": file_count, "sourceFilesSha256": source_digest.hexdigest()}


def build(args) -> dict:
    rootfs, codex = Path(args.rootfs), Path(args.codex)
    source = Path(args.source).resolve()
    for path in (rootfs, codex, Path(args.motor_source), Path(args.collector_source), Path(args.mail_libs)):
        if not path.exists():
            raise ValueError(f"input does not exist: {path}")
    rootfs_hash = sha256_file(rootfs)
    with tempfile.TemporaryDirectory(prefix="daxqo-offline-runtime-") as scratch_dir:
        scratch = Path(scratch_dir)
        entries, _, root_archive = _tar_entries(rootfs, args.allow_test_inputs, scratch)
        codex_archive = None
        try:
            codex_hash, codex_archive = _add_codex(entries, codex, args.allow_test_inputs, scratch)
            _source_backend(entries, source)
            _runtime_files(entries, Path(args.motor_source), Path(args.collector_source), Path(args.mail_libs))
            out = Path(args.output_dir).resolve() / "assets" / "runtime"
            out.mkdir(parents=True, exist_ok=True)
            archive = out / "rootfs.zip"
            _, totals = _write_bundle(entries, archive, args.version)
            result = {"schema": "daxqo.runtime/1", "version": args.version, "abi": "arm64-v8a", "archive": "runtime/rootfs.zip", "sha256": sha256_file(archive), "archiveBytes": archive.stat().st_size, "unpackedBytes": totals["unpackedBytes"], "fileCount": totals["fileCount"], "regularFileCount": totals["regularFileCount"], "sources": {"rootfs_sha256": rootfs_hash, "codex_sha256": codex_hash, "packed_source_files_sha256": totals["sourceFilesSha256"]}}
            manifest_path = out / "manifest.json"
            manifest_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
            return result
        finally:
            root_archive.close()
            if codex_archive is not None:
                codex_archive.close()


def cache_is_current(args) -> bool:
    """Check selected source inputs only; never scan unrelated workspace data."""
    try:
        output = Path(args.output_dir) / "assets/runtime"
        manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        if (manifest.get("version") != args.version or not (output / "rootfs.zip").is_file()
                or (output / "rootfs.zip").stat().st_size != manifest.get("archiveBytes")):
            return False
        if not args.allow_test_inputs and (manifest["sources"]["rootfs_sha256"] != ROOTFS_SHA256
                or manifest["sources"]["codex_sha256"] != CODEX_SHA256):
            return False
        selected = {}
        _source_backend(selected, Path(args.source))
        _runtime_files(selected, Path(args.motor_source), Path(args.collector_source), Path(args.mail_libs))
        digest = hashlib.sha256()
        for name, entry in sorted(selected.items()):
            if entry["kind"] != "disk":
                continue
            path = name.encode("utf-8")
            digest.update(len(path).to_bytes(4, "big"))
            digest.update(path)
            digest.update(bytes.fromhex(sha256_file(entry["source"])))
        return digest.hexdigest() == manifest["sources"]["packed_source_files_sha256"]
    except (OSError, ValueError, KeyError):
        return False


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--rootfs", required=True)
    p.add_argument("--codex", required=True)
    p.add_argument("--motor-source", required=True)
    p.add_argument("--collector-source", required=True)
    p.add_argument("--mail-libs", required=True)
    p.add_argument("--output-dir", required=True)
    p.add_argument("--source", default=str(ROOT))
    p.add_argument("--version", default=(ROOT / "VERSION").read_text(encoding="utf-8").strip())
    p.add_argument("--allow-test-inputs", action="store_true", help="Allow synthetic tiny archives for tests only")
    p.add_argument("--check-current", action="store_true", help="Check whether the cached bundle contains these exact sources")
    return p


def main() -> int:
    try:
        args = parser().parse_args()
        if args.check_current:
            current = cache_is_current(args)
            print(json.dumps({"current": current}))
            return 0 if current else 3
        result = build(args)
    except (OSError, ValueError, tarfile.TarError, zipfile.BadZipFile) as exc:
        parser().error(str(exc))
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
