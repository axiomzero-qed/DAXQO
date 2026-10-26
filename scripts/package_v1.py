#!/usr/bin/env python3
"""Create a reproducible source snapshot of DAXQO V1 and its two native extensions."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
DEFAULT_OUTPUT = ROOT / "dist" / f"v{VERSION}" / f"DAXQO-{VERSION}-source.zip"
ARCHIVE_ROOT = f"DAXQO-{VERSION}"
EXCLUDED_DIRS = {
    ".git", ".tools", ".deps", "dist", "evidence", "state", "workspace", "build", ".gradle",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".venv", "venv", "node_modules",
    "site-packages", "target", "runtime-cache", "mail-libs", "device-data", "device_state", "data", "datasets",
    "credentials", "secrets", ".ssh", ".android", "private-data", "private_data", "user-data", "user_data",
    "history", "logs", "recordings", "captures", "screenshots",
}
EXCLUDED_NAMES = {
    "provider.json", "capability", "capability.json", "local_capability", "harness.sqlite",
    "harness.sqlite3", "credentials.json", "secrets.json", "id_rsa", "id_ed25519", "authorized_keys",
    "api_key", "api-key", "apikey", "api_key.json", "api-key.json", "access_token.json",
    "refresh_token.json", "token.json",
}
EXCLUDED_SUFFIXES = {
    ".apk", ".aab", ".keystore", ".jks", ".p12", ".pfx", ".sqlite", ".sqlite3", ".db",
    ".pyc", ".pyo", ".class", ".dex", ".so", ".aar", ".lockb",
}
EXCLUDED_PREFIXES = (".env",)
EXCLUDED_EXTENDED_KEYS = (".pem", ".key")


class PackageError(ValueError):
    pass


def _git(root: Path, *args: str) -> bytes:
    try:
        return subprocess.run(["git", "-C", str(root), *args], check=True, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise PackageError("cannot read a source repository") from exc


def _repo_root(path: Path) -> Path:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except OSError as exc:
        raise PackageError("source repository does not exist") from exc
    raw = _git(resolved, "rev-parse", "--show-toplevel").decode("utf-8", "surrogateescape").strip()
    top = Path(raw).resolve(strict=True)
    if top != resolved:
        raise PackageError("source path must be the repository root")
    return top


def _paths(root: Path) -> list[str]:
    raw = _git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    paths = []
    for item in raw.split(b"\0"):
        if not item:
            continue
        path = item.decode("utf-8", "surrogateescape")
        if "\\" in path:
            raise PackageError("repository contains a non-POSIX source path")
        pure = PurePosixPath(path)
        if pure.is_absolute() or not pure.parts or any(part in ("", ".", "..") for part in pure.parts):
            raise PackageError("repository contains an unsafe source path")
        if _excluded(path):
            continue
        paths.append(pure.as_posix())
    return sorted(set(paths), key=lambda value: value.encode("utf-8", "surrogateescape"))


def _excluded(path: str) -> bool:
    parts = path.casefold().split("/")
    name = parts[-1]
    if any(part in EXCLUDED_DIRS for part in parts[:-1]):
        return True
    if name in EXCLUDED_NAMES or any(name.startswith(prefix) for prefix in EXCLUDED_PREFIXES):
        return True
    if any(term in name for term in ("api_key", "api-key", "apikey", "credential", "secret")):
        return True
    if Path(name).suffix.casefold() in EXCLUDED_SUFFIXES or name.endswith(EXCLUDED_EXTENDED_KEYS):
        return True
    if path.casefold().startswith("android/app/src/main/assets/runtime/rootfs"):
        return True
    if path.casefold().endswith(("/harness.sqlite-wal", "/harness.sqlite-shm")):
        return True
    return False


def _indexed_modes(root: Path) -> dict[str, int]:
    result = {}
    raw = _git(root, "ls-files", "--stage", "-z")
    for row in raw.split(b"\0"):
        if not row or b"\t" not in row:
            continue
        metadata, path = row.split(b"\t", 1)
        mode = metadata.split(b" ", 1)[0]
        if mode in (b"100644", b"100755", b"120000"):
            result[path.decode("utf-8", "surrogateescape")] = int(mode, 8)
    return result


def _mode_from_git(relative: str, filesystem_mode: int, indexed_modes: dict[str, int]) -> int:
    # Git's executable bit is portable and more stable than Windows' stat mode.
    mode = indexed_modes.get(relative)
    if mode == 0o100755:
        return 0o755
    if mode == 0o100644:
        return 0o644
    if mode == 0o120000:
        return 0o777
    return stat.S_IMODE(filesystem_mode) & 0o777 or 0o644


def _lstat_repo_path(root: Path, relative: str) -> os.stat_result:
    current = root
    parts = PurePosixPath(relative).parts
    for part in parts[:-1]:
        current = current / part
        try:
            metadata = current.lstat()
        except FileNotFoundError as exc:
            raise exc
        if not stat.S_ISDIR(metadata.st_mode):
            raise PackageError("source path traverses a non-directory component")
    return (current / parts[-1]).lstat()


def _resolve_link(root: Path, link_path: str, target: str, included: set[str]) -> None:
    if not target or target.startswith("/") or "\\" in target or "\0" in target:
        raise PackageError("source symlink target is unsafe")
    pending = target.split("/")
    stack = list(PurePosixPath(link_path).parent.parts)
    seen = set()
    while pending:
        part = pending.pop(0)
        if part in ("", "."):
            continue
        if part == "..":
            if not stack:
                raise PackageError("source symlink escapes its repository")
            stack.pop()
            continue
        stack.append(part)
        candidate = "/".join(stack)
        try:
            metadata = _lstat_repo_path(root, candidate)
        except FileNotFoundError as exc:
            raise PackageError("source symlink points to a missing file") from exc
        if stat.S_ISLNK(metadata.st_mode):
            if candidate in seen:
                raise PackageError("source symlink cycle detected")
            seen.add(candidate)
            nested = os.readlink(root / Path(*PurePosixPath(candidate).parts))
            if not isinstance(nested, str):
                nested = os.fsdecode(nested)
            if nested.startswith("/") or "\\" in nested or "\0" in nested:
                raise PackageError("source symlink target is unsafe")
            stack.pop()
            pending = nested.split("/") + pending
        elif not stat.S_ISREG(metadata.st_mode) and not stat.S_ISDIR(metadata.st_mode):
            raise PackageError("source symlink resolves to a special file")
    resolved = "/".join(stack)
    try:
        final = _lstat_repo_path(root, resolved)
    except (FileNotFoundError, IndexError) as exc:
        raise PackageError("source symlink points to a missing file") from exc
    if not stat.S_ISREG(final.st_mode) or resolved not in included:
        raise PackageError("source symlink must resolve to an included source file")


def _repo_files(root: Path, archive_prefix: str) -> tuple[list[dict], dict]:
    listed = _paths(root)
    selected = set(listed)
    indexed_modes = _indexed_modes(root)
    files = []
    for relative in listed:
        try:
            metadata = _lstat_repo_path(root, relative)
        except FileNotFoundError:
            # Tracked paths deleted in the working tree are part of the dirty snapshot as deletions.
            continue
        if stat.S_ISLNK(metadata.st_mode):
            target = os.readlink(root / Path(*PurePosixPath(relative).parts))
            if not isinstance(target, str):
                target = os.fsdecode(target)
            _resolve_link(root, relative, target, selected)
            payload = target.encode("utf-8", "surrogateescape")
            files.append({"root": root, "relative": relative, "archive": f"{archive_prefix}/{relative}",
                          "type": "symlink", "mode": 0o777, "size": len(payload),
                          "sha256": hashlib.sha256(payload).hexdigest(), "target": target})
        elif stat.S_ISREG(metadata.st_mode):
            digest = hashlib.sha256()
            size = 0
            with (root / Path(*PurePosixPath(relative).parts)).open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
                    size += len(chunk)
            after = _lstat_repo_path(root, relative)
            if (metadata.st_size, metadata.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise PackageError("source file changed while preparing the manifest")
            files.append({"root": root, "relative": relative, "archive": f"{archive_prefix}/{relative}",
                          "type": "file", "mode": _mode_from_git(relative, metadata.st_mode, indexed_modes),
                          "size": size, "sha256": digest.hexdigest()})
        else:
            raise PackageError("source repository contains a non-regular special file")
    revision = _git(root, "rev-parse", "HEAD").decode("ascii", "strict").strip()
    dirty = bool(_git(root, "status", "--porcelain", "--untracked-files=all"))
    return files, {"revision": revision, "dirty": dirty,
                   "snapshot": "Current working files: tracked edits and non-ignored untracked sources included; deletions respected; ignored caches, generated output, credentials and device state excluded."}


def _zip_info(name: str, mode: int, kind: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.create_system = 3
    info.compress_type = zipfile.ZIP_DEFLATED
    file_type = stat.S_IFLNK if kind == "symlink" else stat.S_IFREG
    info.external_attr = (file_type | mode) << 16
    info.flag_bits |= 0x800
    return info


def package_v1(motor_source: Path, collector_source: Path, output: Path, main_root: Path = ROOT) -> dict:
    main_root = _repo_root(main_root)
    motor_root = _repo_root(motor_source)
    collector_root = _repo_root(collector_source)
    if len({main_root, motor_root, collector_root}) != 3:
        raise PackageError("the three source repositories must be distinct")

    repo_specs = (("main", main_root, ARCHIVE_ROOT),
                  ("motor", motor_root, f"{ARCHIVE_ROOT}/deps/DAXQO-Motor"),
                  ("collector", collector_root, f"{ARCHIVE_ROOT}/deps/DAXQO-Trajectory-Collector"))
    file_rows = []
    repositories = {}
    for key, root, prefix in repo_specs:
        files, provenance = _repo_files(root, prefix)
        file_rows.extend(files)
        repositories[key] = {"archive_root": prefix, **provenance}
    file_rows.sort(key=lambda row: row["archive"].encode("utf-8", "surrogateescape"))
    manifest = {"schema": "daxqo.source-snapshot/1", "version": VERSION,
                "archive_root": ARCHIVE_ROOT, "repositories": repositories,
                "files": [{key: row[key] for key in ("archive", "type", "mode", "size", "sha256")}
                          for row in file_rows],
                "exclusions": "No .git metadata, ignored files, local tool caches, generated build/release output, credentials, APKs, keystores, device evidence, or runtime state."}
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")

    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".daxqo-source-", suffix=".zip", dir=output.parent, delete=False) as tmp:
            temp_path = Path(tmp.name)
        with zipfile.ZipFile(temp_path, "w", compression=zipfile.ZIP_DEFLATED,
                             compresslevel=9, allowZip64=True) as archive:
            manifest_info = _zip_info(f"{ARCHIVE_ROOT}/SOURCE-MANIFEST.json", 0o644, "file")
            archive.writestr(manifest_info, manifest_bytes)
            for row in file_rows:
                info = _zip_info(row["archive"], row["mode"], row["type"])
                if row["type"] == "symlink":
                    target = os.readlink(row["root"] / Path(*PurePosixPath(row["relative"]).parts))
                    if not isinstance(target, str):
                        target = os.fsdecode(target)
                    if target != row["target"]:
                        raise PackageError("source symlink changed while preparing the archive")
                    archive.writestr(info, target.encode("utf-8", "surrogateescape"))
                    continue
                digest = hashlib.sha256()
                count = 0
                path = row["root"] / Path(*PurePosixPath(row["relative"]).parts)
                with path.open("rb") as source, archive.open(info, "w") as destination:
                    while chunk := source.read(1024 * 1024):
                        digest.update(chunk)
                        count += len(chunk)
                        destination.write(chunk)
                if count != row["size"] or digest.hexdigest() != row["sha256"]:
                    raise PackageError("source file changed while writing the archive")
        temp_path.replace(output)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    return {"ready": True, "output": str(output), "bytes": output.stat().st_size,
            "sourceFiles": len(file_rows), "repositories": repositories}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--motor-source", required=True, type=Path, help="DAXQO Motor repository root")
    parser.add_argument("--collector-source", required=True, type=Path, help="Trajectory Collector repository root")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Output source ZIP path")
    args = parser.parse_args(argv)
    try:
        result = package_v1(args.motor_source, args.collector_source, args.output)
    except (PackageError, OSError) as exc:
        print(json.dumps({"ready": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
