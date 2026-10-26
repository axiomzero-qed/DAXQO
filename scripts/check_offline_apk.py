#!/usr/bin/env python3
"""Statically verify that an APK contains the complete pinned DAXQO V1 offline runtime."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_offline_runtime import CODEX_SHA256, ROOTFS_SHA256  # noqa: E402

RUNTIME_MANIFEST = "assets/runtime/manifest.json"
ROOTFS_ARCHIVE = "assets/runtime/rootfs.zip"
NATIVE_MANIFEST = "assets/runtime/native-manifest.json"
ADDONS_MANIFEST = "assets/addons/native-manifest.json"
NATIVE_LIBS = (
    "lib/arm64-v8a/libproot.so",
    "lib/arm64-v8a/libproot_loader.so",
    "lib/arm64-v8a/libtalloc.so",
    "lib/arm64-v8a/libandroid-shmem.so",
)
VOICE_ASSETS = (
    "assets/voice/model.int8.onnx",
    "assets/voice/tokens.txt",
    "assets/voice/bbpe.model",
    "assets/voice/NOTICE.txt",
)
HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)
USER_STATE_ROOTS = (
    "opt/daxqo/state", "opt/daxqo/workspace", "opt/daxqo/update-stage",
    "root", "home",
)
PRIVATE_CONFIG_NAMES = {
    "provider.json", "capability", "capability.json", "local_capability",
    "api_key", "api-key", "apikey", "api_key.json", "api-key.json",
    "history.json", "conversation-history.json", "conversations.json", "messages.json",
}


class CheckError(ValueError):
    pass


def fail(message: str) -> None:
    raise CheckError(message)


def _read_json_member(archive: zipfile.ZipFile, name: str, limit: int = 4 * 1024 * 1024) -> dict:
    try:
        info = archive.getinfo(name)
    except KeyError:
        fail("required APK metadata is missing")
    if info.file_size > limit:
        fail("APK metadata exceeds the size limit")
    try:
        with archive.open(info) as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            fail("APK metadata exceeds the size limit")
        value = json.loads(data.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, zipfile.BadZipFile):
        fail("APK metadata is invalid")
    if not isinstance(value, dict):
        fail("APK metadata has an invalid shape")
    return value


def _sha256_stream(stream, destination: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with destination.open("wb") as out:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
            out.write(chunk)
    return digest.hexdigest(), size


def _valid_hash(value) -> bool:
    return isinstance(value, str) and HEX_SHA256.fullmatch(value) is not None


def _validate_runtime_manifest(manifest: dict) -> tuple[str, int, int, int, int]:
    expected_version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    if (manifest.get("schema") != "daxqo.runtime/1" or manifest.get("version") != expected_version
            or manifest.get("abi") != "arm64-v8a" or manifest.get("archive") != "runtime/rootfs.zip"):
        fail("offline runtime manifest identity is invalid")
    digest = manifest.get("sha256")
    size, unpacked, count = manifest.get("archiveBytes"), manifest.get("unpackedBytes"), manifest.get("fileCount")
    if not _valid_hash(digest) or type(size) is not int or size < 1 or type(unpacked) is not int or unpacked < 0 or type(count) is not int or count < 1:
        fail("offline runtime manifest values are invalid")
    sources = manifest.get("sources")
    if not isinstance(sources, dict):
        fail("offline runtime source provenance is missing")
    rootfs_source, codex_source = sources.get("rootfs_sha256"), sources.get("codex_sha256")
    if not _valid_hash(rootfs_source) or not _valid_hash(codex_source):
        fail("offline runtime source provenance is invalid")
    if rootfs_source.lower() != ROOTFS_SHA256 or codex_source.lower() != CODEX_SHA256:
        fail("offline runtime is not built from the pinned rootfs and Codex inputs")
    if not _valid_hash(sources.get("packed_source_files_sha256")):
        fail("offline runtime source checksum is missing or invalid")
    return digest.lower(), size, unpacked, count, manifest.get("regularFileCount", -1)


def _entry_path(path) -> str:
    if not isinstance(path, str) or not path or path.startswith("/") or "\\" in path or "\0" in path:
        fail("rootfs index contains an invalid path")
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        fail("rootfs index contains an invalid path")
    return path


def _forbidden(path: str, kind: str) -> bool:
    lowered = path.casefold()
    parts = lowered.split("/")
    basename = parts[-1]
    if basename == "provider.json" or basename.startswith("harness.sqlite"):
        return True
    if parts[0] in ("root", "home") and len(parts) > 1:
        return True
    for scope in USER_STATE_ROOTS[:3]:
        if lowered.startswith(scope + "/"):
            return True
    # Similar names exist in normal system packages (for example,
    # /etc/security/capability.conf and libhistory.so). Restrict checks to
    # DAXQO-owned configuration/state names and user-data locations.
    if parts[:2] == ["opt", "daxqo"] and (
        basename in PRIVATE_CONFIG_NAMES or basename.endswith((".sqlite", ".sqlite3"))
        or basename.endswith(".db")
    ):
        return True
    return False


def _validate_bundle(bundle: Path, manifest: dict) -> tuple[int, int, set[str]]:
    expected_sha, expected_bytes, expected_unpacked, expected_count, expected_regular = _validate_runtime_manifest(manifest)
    if bundle.stat().st_size != expected_bytes:
        fail("offline runtime archive size does not match its manifest")
    actual_sha = hashlib.sha256()
    with bundle.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            actual_sha.update(chunk)
    if actual_sha.hexdigest() != expected_sha:
        fail("offline runtime archive checksum does not match its manifest")

    try:
        with zipfile.ZipFile(bundle) as zf:
            infos = zf.infolist()
            if not infos or infos[0].filename != "bundle-index.json" or infos[0].is_dir() or infos[0].file_size > 16 * 1024 * 1024:
                fail("offline runtime index is missing or not first")
            names = [item.filename for item in infos]
            if len(names) != len(set(names)):
                fail("offline runtime archive contains duplicate entries")
            with zf.open(infos[0]) as stream:
                index = json.loads(stream.read(16 * 1024 * 1024 + 1).decode("utf-8"))
            if (not isinstance(index, dict) or index.get("schema") != "daxqo.rootfs/1"
                    or index.get("version") != manifest["version"] or not isinstance(index.get("entries"), list)):
                fail("offline rootfs index identity is invalid")
            entries = index["entries"]
            if len(entries) != expected_count:
                fail("offline runtime fileCount does not match its index")
            by_path = {}
            regular_bytes = 0
            regular_count = 0
            for row in entries:
                if not isinstance(row, dict):
                    fail("offline rootfs index entry is invalid")
                path = _entry_path(row.get("path"))
                kind = row.get("type")
                size = row.get("size")
                if path == "bundle-index.json" or path in by_path or kind not in ("file", "dir", "symlink", "hardlink"):
                    fail("offline rootfs index contains duplicate or invalid entries")
                if type(size) is not int or size < 0:
                    fail("offline rootfs index contains invalid sizes")
                if kind == "file":
                    if not _valid_hash(row.get("sha256")):
                        fail("offline rootfs file checksum metadata is invalid")
                    regular_bytes += size
                    regular_count += 1
                by_path[path] = row
                if _forbidden(path, kind):
                    fail("offline rootfs contains user data or credential paths")
            if regular_bytes != expected_unpacked:
                fail("offline runtime unpackedBytes does not match its index")
            if expected_regular != -1 and (type(expected_regular) is not int or regular_count != expected_regular):
                fail("offline runtime regularFileCount does not match its index")
            expected_zip_files = {path for path, row in by_path.items() if row["type"] == "file"}
            actual_zip_files = set(names[1:])
            if actual_zip_files != expected_zip_files:
                fail("offline runtime ZIP members do not match its index")
    except (OSError, zipfile.BadZipFile, RuntimeError, UnicodeError, json.JSONDecodeError):
        fail("offline runtime archive is corrupt")

    required = {
        "embedded_runtime": "opt/daxqo/embedded_runtime.py",
        "backend": "opt/daxqo/server.py",
        "python312": "usr/bin/python3.12",
        "ca_bundle": "etc/ssl/certs/ca-certificates.crt",
        "motor": "opt/daxqo/motor/__init__.py",
        "collector": "opt/daxqo/trajectory/__init__.py",
    }
    for label, path in required.items():
        if path not in by_path or by_path[path]["type"] != "file":
            fail("offline rootfs is missing a required runtime component")
    if not any(path.startswith("usr/lib/python3.12/") and "/lib-dynload/_ssl" in path and path.endswith(".so")
               and row["type"] == "file" for path, row in by_path.items()):
        fail("offline rootfs is missing Python SSL support")
    if not any(path.startswith("usr/lib/python3.12/") and "/lib-dynload/_sqlite3" in path and path.endswith(".so")
               and row["type"] == "file" for path, row in by_path.items()):
        fail("offline rootfs is missing Python SQLite support")
    if not any(path.startswith("opt/daxqo/runtime/mail-libs/") and path.endswith("/dkim/__init__.py") and row["type"] == "file"
               for path, row in by_path.items()):
        fail("offline rootfs is missing DKIM support")
    if not any(path.startswith("opt/daxqo/runtime/mail-libs/") and path.endswith("/dns/__init__.py") and row["type"] == "file"
               for path, row in by_path.items()):
        fail("offline rootfs is missing DNS support")
    if not any(path.endswith("/bin/codex") and path.startswith("opt/daxqo/runtime/codex-") and row["type"] == "file"
               for path, row in by_path.items()):
        fail("offline rootfs is missing Codex")
    if not any(path.endswith("/codex-path/rg") and path.startswith("opt/daxqo/runtime/codex-") and row["type"] == "file"
               for path, row in by_path.items()):
        fail("offline rootfs is missing ripgrep")
    return expected_bytes, expected_unpacked, set(by_path)


def _validate_native_manifest(apk: zipfile.ZipFile) -> None:
    manifest = _read_json_member(apk, NATIVE_MANIFEST)
    if manifest.get("schema") != "daxqo.native-runtime/1" or manifest.get("abi") != "arm64-v8a":
        fail("native runtime manifest identity is invalid")
    files = manifest.get("files")
    if not isinstance(files, dict):
        fail("native runtime manifest files are missing")
    for path in NATIVE_LIBS:
        name = path.rsplit("/", 1)[-1]
        record = files.get(name)
        if (not isinstance(record, dict) or not _valid_hash(record.get("sha256"))
                or type(record.get("bytes")) is not int or record["bytes"] <= 0):
            fail("native runtime manifest is incomplete")
        try:
            info = apk.getinfo(path)
        except KeyError:
            fail("APK is missing a required arm64 native library")
        if info.file_size != record["bytes"]:
            fail("APK native library size does not match its manifest")
        digest = hashlib.sha256()
        with apk.open(info) as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != record["sha256"].lower():
            fail("APK native library checksum does not match its manifest")


def _validate_addons_manifest(apk: zipfile.ZipFile) -> None:
    manifest = _read_json_member(apk, ADDONS_MANIFEST)
    sources = manifest.get("sources")
    if manifest.get("schema") != "daxqo.native-addons/1" or not isinstance(sources, dict):
        fail("Collector/Motor native manifest is invalid")
    for component in ("collector", "motor"):
        item = sources.get(component)
        if not isinstance(item, dict) or not re.fullmatch(r"[0-9a-f]{40}", str(item.get("revision", "")), re.I):
            fail("Collector/Motor source provenance is incomplete")
        file_hashes = item.get("files")
        if not isinstance(file_hashes, dict) or not file_hashes or any(not _valid_hash(v) for v in file_hashes.values()):
            fail("Collector/Motor source manifest is incomplete")
    generated = manifest.get("generated")
    if (not isinstance(generated, dict) or any(not _valid_hash(value) for value in generated.values())
            or not any("TrajectoryRecorder.java" in name for name in generated)
            or not any("MotorGate.java" in name for name in generated)
            or not any("MotorEvents.java" in name for name in generated)):
        fail("Collector/Motor generated native sources are incomplete")


def check_apk(apk_path: Path) -> dict:
    apk_path = apk_path.expanduser().resolve()
    if not apk_path.is_file():
        fail("APK path is not a file")
    try:
        with zipfile.ZipFile(apk_path) as apk:
            names = apk.namelist()
            if len(names) != len(set(names)):
                fail("APK contains duplicate ZIP members")
            required = (RUNTIME_MANIFEST, ROOTFS_ARCHIVE, *NATIVE_LIBS, *VOICE_ASSETS)
            if any(name not in names for name in required):
                fail("APK is missing required offline runtime or voice assets")
            if not any(name.startswith("assets/voice/licenses/") and not name.endswith("/") for name in names):
                fail("APK is missing voice model licenses")
            runtime_manifest = _read_json_member(apk, RUNTIME_MANIFEST)
            _validate_native_manifest(apk)
            _validate_addons_manifest(apk)
            with tempfile.TemporaryDirectory(prefix="daxqo-apk-check-") as temp_dir:
                bundle_path = Path(temp_dir) / "rootfs.zip"
                try:
                    with apk.open(ROOTFS_ARCHIVE) as stream:
                        archive_sha, archive_size = _sha256_stream(stream, bundle_path)
                except (OSError, zipfile.BadZipFile, RuntimeError):
                    fail("APK rootfs asset cannot be read")
                expected_sha, expected_size, _, _, _ = _validate_runtime_manifest(runtime_manifest)
                if archive_sha != expected_sha or archive_size != expected_size:
                    fail("APK rootfs asset does not match its runtime manifest")
                bundle_bytes, unpacked_bytes, paths = _validate_bundle(bundle_path, runtime_manifest)
            apk_total = apk_path.stat().st_size
            return {
                "ready": True,
                "apkBytes": apk_total,
                "rootfsArchiveBytes": bundle_bytes,
                "rootfsUnpackedBytes": unpacked_bytes,
                "rootfsEntryCount": len(paths),
                "components": ["arm64-rootfs", "python-3.12", "codex", "motor", "collector", "mail-dkim-dns", "offline-voice", "native-prerequisites"],
            }
    except CheckError:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError):
        fail("APK is not a readable ZIP package")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apk", required=True, type=Path, help="APK to inspect")
    args = parser.parse_args(argv)
    try:
        result = check_apk(args.apk)
    except CheckError as exc:
        print(json.dumps({"ready": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
