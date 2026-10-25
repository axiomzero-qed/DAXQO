"""Offline tests for the pinned voice-runtime preparation path."""
from __future__ import annotations

import hashlib
import importlib.util
from io import BytesIO
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "android" / "tools" / "prepare_voice_runtime.py"
spec = importlib.util.spec_from_file_location("voice_runtime_prepare_tests", SCRIPT)
voice = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(voice)


def make_model_archive(path: Path, *, include_required: bool = True, extra_member: str | None = None,
                       symlink: bool = False) -> tuple[int, str]:
    files = {"model.int8.onnx": b"model", "tokens.txt": b"tokens", "bbpe.model": b"bpe"}
    with tarfile.open(path, "w:bz2") as archive:
        for name, content in files.items():
            if not include_required and name == "model.int8.onnx":
                continue
            info = tarfile.TarInfo(f"./tiny-model/{name}")
            info.size = len(content)
            archive.addfile(info, BytesIO(content))
        if extra_member is not None:
            info = tarfile.TarInfo(extra_member)
            if symlink:
                info.type = tarfile.SYMTYPE
                info.linkname = "tiny-model/tokens.txt"
                archive.addfile(info)
            else:
                info.size = 1
                archive.addfile(info, BytesIO(b"x"))
    raw = path.read_bytes()
    return len(raw), hashlib.sha256(raw).hexdigest()


def make_aar(path: Path, *, embedded_license: bool = False) -> tuple[int, str]:
    classes = BytesIO()
    with zipfile.ZipFile(classes, "w") as jar:
        jar.writestr("com/k2fsa/sherpa/onnx/OnlineRecognizer.class", b"class")
        jar.writestr("com/k2fsa/sherpa/onnx/OnlineStream.class", b"class")
    with zipfile.ZipFile(path, "w") as aar:
        aar.writestr("AndroidManifest.xml", b"manifest")
        aar.writestr("classes.jar", classes.getvalue())
        for abi in ("arm64-v8a", "armeabi-v7a", "x86", "x86_64"):
            aar.writestr(f"jni/{abi}/libsherpa-onnx-jni.so", b"jni")
            if abi in ("arm64-v8a", "armeabi-v7a"):
                aar.writestr(f"jni/{abi}/libonnxruntime.so", b"ort")
        if embedded_license:
            aar.writestr("META-INF/LICENSE", b"Upstream AAR license text")
    raw = path.read_bytes()
    return len(raw), hashlib.sha256(raw).hexdigest()


class FakeResponse(BytesIO):
    def __init__(self, body: bytes, content_length: str | None = None):
        super().__init__(body)
        self.headers = {} if content_length is None else {"Content-Length": content_length}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class VoiceRuntimePreparationTests(unittest.TestCase):
    def test_pinned_assets_are_exact_and_aars_expose_expected_runtime_classes(self):
        self.assertEqual(voice.AAR_SIZE, 49_236_482)
        self.assertEqual(voice.AAR_SHA256, "f0f8716982d27cb4ce695e3934018b1342d9d3d6fdb6ef1f1658fe8bad50b4fc")
        self.assertEqual(voice.MODEL_ARCHIVE_SIZE, 21_264_113)
        self.assertEqual(voice.MODEL_SHA256, "b3b309f7ce4a737195fcc6963ea19b0653a7d3401580af5ae0d3e284cbb71f0b")
        with tempfile.TemporaryDirectory() as temp:
            aar = Path(temp) / "test.aar"
            size, digest = make_aar(aar)
            with patch.object(voice, "AAR_SIZE", size), patch.object(voice, "AAR_SHA256", digest):
                voice.verify_aar(aar)

    def test_cached_download_must_match_expected_hash_and_size(self):
        with tempfile.TemporaryDirectory() as temp:
            cached = Path(temp) / "pinned.bin"
            cached.write_bytes(b"trusted")
            expected_hash = hashlib.sha256(b"trusted").hexdigest()
            self.assertEqual(voice.fetch_pinned("unused", cached, expected_size=7,
                                                expected_sha256=expected_hash, max_size=10), cached)
            cached.write_bytes(b"altered")
            with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
                voice.fetch_pinned("unused", cached, expected_size=7,
                                   expected_sha256=expected_hash, max_size=10)

    def test_download_is_size_limited_and_verified_before_cache_install(self):
        body = b"verified payload"
        digest = hashlib.sha256(body).hexdigest()
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "asset.bin"
            with patch.object(voice.urllib.request, "urlopen", return_value=FakeResponse(body, str(len(body)))):
                voice.fetch_pinned("https://example.invalid/pinned", target, expected_size=len(body),
                                   expected_sha256=digest, max_size=len(body))
            self.assertEqual(target.read_bytes(), body)

            oversized = Path(temp) / "oversized.bin"
            with patch.object(voice.urllib.request, "urlopen", return_value=FakeResponse(body, "100")):
                with self.assertRaisesRegex(ValueError, "size limit"):
                    voice.fetch_pinned("https://example.invalid/oversized", oversized, expected_size=len(body),
                                       expected_sha256=digest, max_size=len(body))
            self.assertFalse(oversized.exists())
            self.assertFalse(oversized.with_name(oversized.name + ".part").exists())

    def test_cache_override_environment_is_honored(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.dict(os.environ, {voice.CACHE_ENV: temp}):
                self.assertEqual(voice.default_cache_dir(), Path(temp).resolve())

    def test_license_bundle_contains_full_texts_and_preserves_aar_notices(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            aar = root / "test.aar"
            size, digest = make_aar(aar, embedded_license=True)
            with patch.object(voice, "AAR_SIZE", size), patch.object(voice, "AAR_SHA256", digest):
                voice.verify_aar(aar)
                copied = voice._copy_aar_legal_files(aar, root / "licenses")
            self.assertEqual(copied, ["META-INF/LICENSE"])
            self.assertEqual((root / "licenses/sherpa-onnx-aar/META-INF/LICENSE").read_bytes(),
                             b"Upstream AAR license text")
            staged = root / "voice"
            voice.stage_license_assets(staged / "licenses")
            expected = {
                "sherpa-onnx/LICENSE.txt": b"Apache License",
                "zipformer-zh-model/LICENSE.txt": b"Apache License",
                "onnxruntime/LICENSE.txt": b"MIT License",
                "onnxruntime/ThirdPartyNotices.txt": b"THIRD PARTY SOFTWARE NOTICES",
            }
            for relative, prefix in expected.items():
                with self.subTest(license=relative):
                    self.assertTrue((staged / "licenses" / relative).read_bytes().lstrip().startswith(prefix))

    def test_model_extraction_selects_only_expected_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive_path = root / "model.tar.bz2"
            size, digest = make_model_archive(archive_path, extra_member="tiny-model/test_wavs/sample.wav")
            files = {"model.int8.onnx": 5, "tokens.txt": 6, "bbpe.model": 3}
            with patch.multiple(voice, MODEL_NAME="tiny-model", MODEL_FILES=files,
                                MODEL_ARCHIVE_SIZE=size, MODEL_SHA256=digest):
                output = root / "out"
                voice.extract_model_archive(archive_path, output)
            self.assertEqual({path.name for path in output.iterdir()}, set(files))
            self.assertEqual((output / "model.int8.onnx").read_bytes(), b"model")

    def test_model_extraction_rejects_traversal_links_and_incomplete_models(self):
        files = {"model.int8.onnx": 5, "tokens.txt": 6, "bbpe.model": 3}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.multiple(voice, MODEL_NAME="tiny-model", MODEL_FILES=files):
                for name, symlink in (("tiny-model/../escape", False), ("tiny-model/link", True)):
                    archive_path = root / ("bad-" + str(symlink) + ".tar.bz2")
                    size, digest = make_model_archive(archive_path, extra_member=name, symlink=symlink)
                    with patch.object(voice, "MODEL_ARCHIVE_SIZE", size), patch.object(voice, "MODEL_SHA256", digest):
                        with self.subTest(member=name), self.assertRaises(ValueError):
                            voice.extract_model_archive(archive_path, root / ("out-" + str(symlink)))

                archive_path = root / "incomplete.tar.bz2"
                size, digest = make_model_archive(archive_path, include_required=False)
                with patch.object(voice, "MODEL_ARCHIVE_SIZE", size), patch.object(voice, "MODEL_SHA256", digest):
                    with self.assertRaisesRegex(ValueError, "missing required asset"):
                        voice.extract_model_archive(archive_path, root / "incomplete")


if __name__ == "__main__":
    unittest.main()
