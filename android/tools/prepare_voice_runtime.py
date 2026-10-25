"""Prepare pinned sherpa-onnx Android runtime and Chinese streaming CTC assets."""
from __future__ import annotations

import argparse
import hashlib
import io
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
import tarfile
import tempfile
import urllib.request
import zipfile


REPO_ROOT = Path(__file__).resolve().parents[2]
CACHE_ENV = "DAXQO_VOICE_CACHE"
AAR_NAME = "sherpa-onnx-2.0.0.aar"
AAR_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/v2.0.0/sherpa-onnx-2.0.0.aar"
AAR_SHA256 = "f0f8716982d27cb4ce695e3934018b1342d9d3d6fdb6ef1f1658fe8bad50b4fc"
AAR_SIZE = 49_236_482
AAR_MAX_SIZE = 60_000_000
MODEL_NAME = "sherpa-onnx-streaming-zipformer-small-ctc-zh-int8-2025-04-01"
MODEL_ARCHIVE_NAME = MODEL_NAME + ".tar.bz2"
MODEL_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/" + MODEL_ARCHIVE_NAME
MODEL_SHA256 = "b3b309f7ce4a737195fcc6963ea19b0653a7d3401580af5ae0d3e284cbb71f0b"
MODEL_ARCHIVE_SIZE = 21_264_113
MODEL_ARCHIVE_MAX_SIZE = 32_000_000
MODEL_FILES = {
    "model.int8.onnx": 26_342_340,
    "tokens.txt": 13_366,
    "bbpe.model": 255_180,
}
MAX_ARCHIVE_MEMBERS = 64
MAX_UNPACKED_BYTES = 40_000_000
MAX_MEMBER_BYTES = 30_000_000
LICENSE_DIR = Path(__file__).resolve().parent / "licenses"
LICENSE_ASSETS = {
    "sherpa-onnx-v2.0.0-LICENSE.txt": "sherpa-onnx/LICENSE.txt",
    "zipformer-zh-model-LICENSE.txt": "zipformer-zh-model/LICENSE.txt",
    "onnxruntime-v1.28.2-LICENSE.txt": "onnxruntime/LICENSE.txt",
    "onnxruntime-v1.28.2-ThirdPartyNotices.txt": "onnxruntime/ThirdPartyNotices.txt",
}

MODEL_LICENSE_NOTE = """sherpa-onnx streaming Zipformer small CTC Chinese INT8 model
Model files: model.int8.onnx, tokens.txt, bbpe.model
Model archive: {model_url}
Archive SHA-256: {model_sha256}
License: Apache-2.0. The upstream checkpoint repository declares Apache-2.0:
https://huggingface.co/csukuangfj/icefall-streaming-zipformer-small-ctc-zh-2025-04-01
Full sherpa-onnx license: licenses/sherpa-onnx/LICENSE.txt
Full model license text: licenses/zipformer-zh-model/LICENSE.txt
ONNX Runtime 1.28.2 MIT license and third-party notices: licenses/onnxruntime/
Model usage documentation:
https://k2-fsa.github.io/sherpa/onnx/pretrained_models/online-ctc/zipformer-ctc-models.html
""".format(model_url=MODEL_URL, model_sha256=MODEL_SHA256)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_file(path: Path, *, expected_size: int, expected_sha256: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Pinned voice asset is missing or not a regular file: {path}")
    actual_size = path.stat().st_size
    if actual_size != expected_size:
        raise ValueError(f"Pinned voice asset size mismatch for {path.name}: expected {expected_size}, got {actual_size}")
    actual_hash = sha256_file(path)
    if actual_hash != expected_sha256:
        raise ValueError(f"Pinned voice asset SHA-256 mismatch for {path.name}")


def fetch_pinned(url: str, destination: Path, *, expected_size: int,
                 expected_sha256: str, max_size: int) -> Path:
    """Fetch once to a temporary sibling; cache entries are always verified."""
    if destination.exists() or destination.is_symlink():
        verify_file(destination, expected_size=expected_size, expected_sha256=expected_sha256)
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".part")
    if partial.exists() or partial.is_symlink():
        if partial.is_dir() and not partial.is_symlink():
            raise ValueError(f"Refusing non-file partial download path: {partial}")
        partial.unlink()
    digest = hashlib.sha256()
    size = 0
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "DAXQO-voice-runtime/1"})
        with urllib.request.urlopen(request, timeout=90) as response, partial.open("xb") as output:
            length = response.headers.get("Content-Length")
            if length and int(length) > max_size:
                raise ValueError(f"Download exceeds configured size limit: {url}")
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_size:
                    raise ValueError(f"Download exceeds configured size limit: {url}")
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if size != expected_size:
            raise ValueError(f"Pinned download size mismatch: expected {expected_size}, got {size}")
        if digest.hexdigest() != expected_sha256:
            raise ValueError("Pinned download SHA-256 mismatch")
        partial.replace(destination)
    except Exception:
        try:
            partial.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    verify_file(destination, expected_size=expected_size, expected_sha256=expected_sha256)
    return destination


def verify_aar(path: Path) -> None:
    verify_file(path, expected_size=AAR_SIZE, expected_sha256=AAR_SHA256)
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            required = {
                "AndroidManifest.xml",
                "classes.jar",
                "jni/arm64-v8a/libsherpa-onnx-jni.so",
                "jni/arm64-v8a/libonnxruntime.so",
                "jni/armeabi-v7a/libsherpa-onnx-jni.so",
                "jni/x86/libsherpa-onnx-jni.so",
                "jni/x86_64/libsherpa-onnx-jni.so",
            }
            missing = required - names
            if missing:
                raise ValueError("Pinned sherpa-onnx AAR is missing required files: " + ", ".join(sorted(missing)))
            if archive.testzip() is not None:
                raise ValueError("Pinned sherpa-onnx AAR has a corrupt ZIP member")
            with zipfile.ZipFile(io.BytesIO(archive.read("classes.jar"))) as classes:
                class_names = set(classes.namelist())
                if "com/k2fsa/sherpa/onnx/OnlineRecognizer.class" not in class_names:
                    raise ValueError("Pinned sherpa-onnx AAR has no OnlineRecognizer class")
                if "com/k2fsa/sherpa/onnx/OnlineStream.class" not in class_names:
                    raise ValueError("Pinned sherpa-onnx AAR has no OnlineStream class")
                if any(name.startswith("kotlin/jvm/internal/") for name in class_names):
                    raise ValueError("Unexpected Kotlin runtime classes bundled in sherpa-onnx AAR")
    except zipfile.BadZipFile as error:
        raise ValueError("Pinned sherpa-onnx AAR is not a valid ZIP archive") from error


def _copy_aar_legal_files(aar_path: Path, destination: Path) -> list[str]:
    """Preserve any upstream license/notice documents embedded in the AAR."""
    legal_names = {"license", "license.txt", "notice", "notice.txt", "thirdpartynotices.txt"}
    copied: list[str] = []
    total = 0
    with zipfile.ZipFile(aar_path) as archive:
        for info in archive.infolist():
            if info.is_dir() or PurePosixPath(info.filename).name.lower() not in legal_names:
                continue
            normalized = _normalized_member_name(info.filename)
            if info.file_size > 1_000_000:
                raise ValueError("AAR embedded license/notice exceeds its size limit")
            total += info.file_size
            if total > 4_000_000:
                raise ValueError("AAR embedded legal documents exceed their size limit")
            target = destination / "sherpa-onnx-aar" / normalized
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open("xb") as output:
                shutil.copyfileobj(source, output, length=64 * 1024)
            copied.append(normalized)
    return copied


def stage_license_assets(destination: Path) -> None:
    for source_name, target_name in LICENSE_ASSETS.items():
        source = LICENSE_DIR / source_name
        if source.is_symlink() or not source.is_file():
            raise ValueError(f"Required third-party license text is missing: {source}")
        content = source.read_bytes()
        if not content or len(content) > 1_000_000:
            raise ValueError(f"Required third-party license text is empty or too large: {source}")
        target = destination / target_name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


def _normalized_member_name(name: str) -> str:
    if "\\" in name:
        raise ValueError("Voice model archive contains a Windows-style path")
    path = PurePosixPath(name)
    if path.is_absolute() or PureWindowsPath(name).drive:
        raise ValueError("Voice model archive contains an absolute path")
    parts = [part for part in path.parts if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise ValueError("Voice model archive contains a path traversal")
    return "/".join(parts)


def extract_model_archive(archive_path: Path, destination: Path) -> None:
    """Extract only the three pinned model assets after validating every member."""
    verify_file(archive_path, expected_size=MODEL_ARCHIVE_SIZE, expected_sha256=MODEL_SHA256)
    try:
        archive = tarfile.open(archive_path, mode="r:bz2")
    except (tarfile.TarError, OSError) as error:
        raise ValueError("Pinned voice model is not a valid bzip2 tar archive") from error
    with archive:
        members = archive.getmembers()
        if len(members) > MAX_ARCHIVE_MEMBERS:
            raise ValueError("Voice model archive contains too many members")
        normalized: dict[str, tarfile.TarInfo] = {}
        unpacked_total = 0
        for member in members:
            name = _normalized_member_name(member.name)
            if name in normalized:
                raise ValueError("Voice model archive contains duplicate paths")
            normalized[name] = member
            if not (member.isdir() or member.isfile()):
                raise ValueError("Voice model archive contains a link or special file")
            if member.isfile():
                if member.size < 0 or member.size > MAX_MEMBER_BYTES:
                    raise ValueError("Voice model archive member exceeds its size limit")
                unpacked_total += member.size
                if unpacked_total > MAX_UNPACKED_BYTES:
                    raise ValueError("Voice model archive exceeds its unpacked size limit")
        expected_prefix = MODEL_NAME + "/"
        for filename, expected_size in MODEL_FILES.items():
            member = normalized.get(expected_prefix + filename)
            if member is None or not member.isfile():
                raise ValueError(f"Voice model archive is missing required asset: {filename}")
            if member.size != expected_size:
                raise ValueError(f"Voice model asset size mismatch for {filename}")
        destination.mkdir(parents=True, exist_ok=True)
        for filename in MODEL_FILES:
            member = normalized[expected_prefix + filename]
            source = archive.extractfile(member)
            if source is None:
                raise ValueError(f"Unable to read model archive member: {filename}")
            target = destination / filename
            with source, target.open("xb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
            if target.stat().st_size != MODEL_FILES[filename]:
                raise ValueError(f"Extracted voice model asset has an unexpected size: {filename}")


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    try:
        if temporary.exists() or temporary.is_symlink():
            if temporary.is_dir() and not temporary.is_symlink():
                raise ValueError(f"Refusing non-file output temp path: {temporary}")
            temporary.unlink()
        shutil.copyfile(source, temporary)
        temporary.replace(destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def default_cache_dir() -> Path:
    override = os.environ.get(CACHE_ENV)
    return Path(override).expanduser().resolve() if override else REPO_ROOT / ".tools" / "voice-runtime"


def prepare(cache_dir: Path, output_dir: Path) -> tuple[Path, Path]:
    cache_dir = cache_dir.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    aar_cache = fetch_pinned(AAR_URL, cache_dir / AAR_NAME, expected_size=AAR_SIZE,
                             expected_sha256=AAR_SHA256, max_size=AAR_MAX_SIZE)
    verify_aar(aar_cache)
    model_cache = fetch_pinned(MODEL_URL, cache_dir / MODEL_ARCHIVE_NAME,
                               expected_size=MODEL_ARCHIVE_SIZE, expected_sha256=MODEL_SHA256,
                               max_size=MODEL_ARCHIVE_MAX_SIZE)
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="prepare-voice-", dir=output_dir) as temp:
        temporary_root = Path(temp)
        temporary_assets = temporary_root / "assets" / "voice"
        extract_model_archive(model_cache, temporary_assets)
        stage_license_assets(temporary_assets / "licenses")
        aar_legal = _copy_aar_legal_files(aar_cache, temporary_assets / "licenses")
        aar_note = "" if not aar_legal else "\nAAR-embedded legal files copied to voice/licenses/sherpa-onnx-aar/: " + ", ".join(aar_legal) + "\n"
        (temporary_assets / "NOTICE.txt").write_text(
            MODEL_LICENSE_NOTE + "\nBundled license files are in licenses/.\n" + aar_note,
            encoding="utf-8", newline="\n")
        for filename in MODEL_FILES:
            _atomic_copy(temporary_assets / filename, output_dir / "assets" / "voice" / filename)
        _atomic_copy(temporary_assets / "NOTICE.txt", output_dir / "assets" / "voice" / "NOTICE.txt")
        shutil.copytree(temporary_assets / "licenses", output_dir / "assets" / "voice" / "licenses", dirs_exist_ok=True)
        _atomic_copy(aar_cache, output_dir / "libs" / "sherpa-onnx.aar")
    verify_aar(output_dir / "libs" / "sherpa-onnx.aar")
    for filename, size in MODEL_FILES.items():
        target = output_dir / "assets" / "voice" / filename
        if target.stat().st_size != size:
            raise ValueError(f"Generated voice asset verification failed: {filename}")
    return output_dir / "libs" / "sherpa-onnx.aar", output_dir / "assets" / "voice"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, default=default_cache_dir(),
                        help=f"Verified download cache (default: ${CACHE_ENV} or repo .tools/voice-runtime)")
    parser.add_argument("--output-dir", type=Path,
                        default=REPO_ROOT / "android" / "app" / "build" / "generated" / "voice",
                        help="Generated AAR/assets directory")
    args = parser.parse_args(argv)
    aar, assets = prepare(args.cache_dir, args.output_dir)
    print(f"Prepared pinned voice runtime: {aar}")
    print(f"Prepared verified Chinese streaming model assets: {assets}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
