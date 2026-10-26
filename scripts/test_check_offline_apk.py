import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import zipfile


SCRIPT = Path(__file__).with_name("check_offline_apk.py")
SPEC = importlib.util.spec_from_file_location("check_offline_apk", SCRIPT)
check_offline_apk = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check_offline_apk)


def build_apk(path: Path, *, root_hash: str, codex_hash: str, bad_index_count: bool = False,
              omit_component: str = "") -> None:
    files = {
        "opt/daxqo/embedded_runtime.py": b"runtime",
        "opt/daxqo/server.py": b"server",
        "usr/bin/python3.12": b"python",
        "usr/lib/python3.12/lib-dynload/_ssl.cpython-312-aarch64-linux-gnu.so": b"ssl",
        "usr/lib/python3.12/lib-dynload/_sqlite3.cpython-312-aarch64-linux-gnu.so": b"sqlite",
        "etc/ssl/certs/ca-certificates.crt": b"ca",
        "opt/daxqo/motor/__init__.py": b"motor",
        "opt/daxqo/trajectory/__init__.py": b"collector",
        "opt/daxqo/runtime/mail-libs/dkim/__init__.py": b"dkim",
        "opt/daxqo/runtime/mail-libs/dns/__init__.py": b"dns",
        "opt/daxqo/runtime/codex-0.163.0/bin/codex": b"codex",
        "opt/daxqo/runtime/codex-0.163.0/codex-path/rg": b"rg",
    }
    if omit_component:
        files.pop(omit_component)
    entries = [{"path": name, "type": "file", "mode": 420, "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(), "target": ""} for name, data in files.items()]
    version = check_offline_apk.ROOT.joinpath("VERSION").read_text(encoding="utf-8").strip()
    index = {"schema": "daxqo.rootfs/1", "version": version, "entries": entries}
    unpacked = sum(len(value) for value in files.values())
    with tempfile.TemporaryDirectory() as td:
        bundle = Path(td) / "rootfs.zip"
        with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("bundle-index.json", json.dumps(index))
            for name, data in files.items():
                zf.writestr(name, data)
        archive_sha = hashlib.sha256(bundle.read_bytes()).hexdigest()
        runtime = {"schema": "daxqo.runtime/1", "version": version, "abi": "arm64-v8a",
                   "archive": "runtime/rootfs.zip", "sha256": archive_sha,
                   "archiveBytes": bundle.stat().st_size, "unpackedBytes": unpacked,
                   "fileCount": len(entries) - (1 if bad_index_count else 0), "regularFileCount": len(entries),
                   "sources": {"rootfs_sha256": root_hash, "codex_sha256": codex_hash,
                               "packed_source_files_sha256": "c" * 64}}
        native_libs = {name.rsplit("/", 1)[-1]: name.encode("ascii") for name in check_offline_apk.NATIVE_LIBS}
        native_manifest = {"schema": "daxqo.native-runtime/1", "abi": "arm64-v8a",
                           "files": {name: {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
                                     for name, data in native_libs.items()}}
        addons = {"schema": "daxqo.native-addons/1", "sources": {
            component: {"revision": "1" * 40, "files": {"source.py": "2" * 64}}
            for component in ("collector", "motor")}, "generated": {
                "TrajectoryRecorder.java": "1" * 64, "MotorGate.java": "2" * 64, "MotorEvents.java": "3" * 64}}
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as apk:
            apk.writestr(check_offline_apk.RUNTIME_MANIFEST, json.dumps(runtime))
            apk.write(bundle, check_offline_apk.ROOTFS_ARCHIVE)
            apk.writestr(check_offline_apk.NATIVE_MANIFEST, json.dumps(native_manifest))
            apk.writestr(check_offline_apk.ADDONS_MANIFEST, json.dumps(addons))
            for name, data in native_libs.items():
                apk.writestr("lib/arm64-v8a/" + name, data)
            for name in check_offline_apk.VOICE_ASSETS:
                apk.writestr(name, b"voice")
            apk.writestr("assets/voice/licenses/model.txt", b"license")


class OfflineApkCheckTests(unittest.TestCase):
    def test_system_capability_and_history_names_are_not_user_state(self):
        self.assertFalse(check_offline_apk._forbidden("etc/security/capability.conf", "file"))
        self.assertFalse(check_offline_apk._forbidden("usr/lib/libhistory.so", "file"))
        self.assertFalse(check_offline_apk._forbidden("usr/share/doc/libhistory/changelog", "file"))
        self.assertFalse(check_offline_apk._forbidden("root", "dir"))

    def test_user_provider_and_persisted_state_are_rejected(self):
        self.assertTrue(check_offline_apk._forbidden("opt/daxqo/state/provider.json", "file"))
        self.assertTrue(check_offline_apk._forbidden("opt/daxqo/state/harness.sqlite3", "file"))
        self.assertTrue(check_offline_apk._forbidden("root/.config/daxqo/provider.json", "file"))

    def test_complete_pinned_bundle_is_reported_without_payload_output(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "v1.apk"
            root_hash, codex_hash = "a" * 64, "b" * 64
            build_apk(path, root_hash=root_hash, codex_hash=codex_hash)
            old_root, old_codex = check_offline_apk.ROOTFS_SHA256, check_offline_apk.CODEX_SHA256
            try:
                check_offline_apk.ROOTFS_SHA256 = root_hash
                check_offline_apk.CODEX_SHA256 = codex_hash
                result = check_offline_apk.check_apk(path)
            finally:
                check_offline_apk.ROOTFS_SHA256 = old_root
                check_offline_apk.CODEX_SHA256 = old_codex
            self.assertTrue(result["ready"])
            self.assertEqual(result["rootfsEntryCount"], 12)
            self.assertIn("motor", result["components"])

    def test_rejects_synthetic_unpinned_rootfs_and_codex_sources(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "synthetic.apk"
            build_apk(path, root_hash="a" * 64, codex_hash="b" * 64)
            with self.assertRaisesRegex(check_offline_apk.CheckError, "pinned rootfs and Codex"):
                check_offline_apk.check_apk(path)

    def test_rejects_manifest_index_count_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "bad-count.apk"
            root_hash, codex_hash = "a" * 64, "b" * 64
            build_apk(path, root_hash=root_hash, codex_hash=codex_hash, bad_index_count=True)
            old_root, old_codex = check_offline_apk.ROOTFS_SHA256, check_offline_apk.CODEX_SHA256
            try:
                check_offline_apk.ROOTFS_SHA256 = root_hash
                check_offline_apk.CODEX_SHA256 = codex_hash
                with self.assertRaisesRegex(check_offline_apk.CheckError, "fileCount"):
                    check_offline_apk.check_apk(path)
            finally:
                check_offline_apk.ROOTFS_SHA256 = old_root
                check_offline_apk.CODEX_SHA256 = old_codex

    def test_rejects_missing_runtime_component(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "incomplete.apk"
            root_hash, codex_hash = "a" * 64, "b" * 64
            build_apk(path, root_hash=root_hash, codex_hash=codex_hash, omit_component="opt/daxqo/motor/__init__.py")
            old_root, old_codex = check_offline_apk.ROOTFS_SHA256, check_offline_apk.CODEX_SHA256
            try:
                check_offline_apk.ROOTFS_SHA256 = root_hash
                check_offline_apk.CODEX_SHA256 = codex_hash
                with self.assertRaisesRegex(check_offline_apk.CheckError, "required runtime component"):
                    check_offline_apk.check_apk(path)
            finally:
                check_offline_apk.ROOTFS_SHA256 = old_root
                check_offline_apk.CODEX_SHA256 = old_codex


if __name__ == "__main__":
    unittest.main()
