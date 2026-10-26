import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
import zipfile
from argparse import Namespace


SCRIPT = Path(__file__).with_name("build_offline_runtime.py")
spec = importlib.util.spec_from_file_location("build_offline_runtime", SCRIPT)
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def add_tar_file(tar, name, data, mode=0o644):
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = mode
    tar.addfile(info, io.BytesIO(data))


class OfflineRuntimeBundleTests(unittest.TestCase):
    def test_synthetic_bundle_index_first_and_no_runtime_state(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            rootfs = root / "rootfs.tar.xz"
            codex = root / "codex.tgz"
            with tarfile.open(rootfs, "w:xz") as tar:
                add_tar_file(tar, "./etc/os-release", b"Ubuntu\n")
                add_tar_file(tar, "./etc/machine-id", b"owner-id")
                add_tar_file(tar, "./usr/lib/systemd/system/systemd\\x2dexample.slice", b"unused unit")
                add_tar_file(tar, "./root/.profile", b"private")
                add_tar_file(tar, "./var/log/boot.log", b"log")
                add_tar_file(tar, "./usr/bin/tool", b"#!tool", 0o755)
                link = tarfile.TarInfo("./bin")
                link.type = tarfile.SYMTYPE
                link.linkname = "/usr/bin"
                tar.addfile(link)
            with tarfile.open(codex, "w:gz") as tar:
                add_tar_file(tar, "package/vendor/aarch64-unknown-linux-musl/bin/codex", b"codex", 0o755)
                add_tar_file(tar, "package/vendor/aarch64-unknown-linux-musl/codex-path/rg", b"rg", 0o755)
            source = root / "source"
            (source / "backend" / "static" / "nested").mkdir(parents=True)
            (source / "backend" / "server.py").write_text("print('server')\n")
            (source / "backend" / "models-a.json").write_text("{}\n")
            (source / "backend" / "static" / "nested" / "index.html").write_text("ok")
            (source / "scripts").mkdir()
            (source / "scripts" / "assistant_client.py").write_text("# client\n")
            (source / "scripts" / "other.py").write_text("# excluded\n")
            (source / "LICENSE").write_text("DAXQO license\n")
            motor = root / "motor"
            collector = root / "collector"
            mail = root / "mail"
            for folder, filename in ((motor, "engine.py"), (collector, "collector.py"), (mail, "dkim.py")):
                folder.mkdir()
                (folder / filename).write_text("# included\n")
            (root / "LICENSE").write_text("component license\n")
            (motor / "__pycache__").mkdir()
            (motor / "__pycache__" / "engine.cpython-312.pyc").write_bytes(b"cache")
            args = Namespace(rootfs=str(rootfs), codex=str(codex), source=str(source), motor_source=str(motor), collector_source=str(collector), mail_libs=str(mail), output_dir=str(root / "out"), version="1.0.1", allow_test_inputs=True)
            result = builder.build(args)
            self.assertTrue(builder.cache_is_current(args))
            (source / ".tools").mkdir()
            (source / ".tools/private-test-data").write_text("must not be scanned or packaged")
            self.assertTrue(builder.cache_is_current(args))
            second = builder.build(args)
            self.assertEqual(result["sha256"], second["sha256"])
            original = (source / "backend/server.py").read_text()
            (source / "backend/server.py").write_text("print('changed source')\n")
            self.assertFalse(builder.cache_is_current(args))
            (source / "backend/server.py").write_text(original)
            self.assertTrue(builder.cache_is_current(args))
            archive = root / "out" / "assets" / "runtime" / "rootfs.zip"
            self.assertEqual(result["schema"], "daxqo.runtime/1")
            with zipfile.ZipFile(archive) as zf:
                self.assertEqual(zf.namelist()[0], "bundle-index.json")
                index = json.loads(zf.read("bundle-index.json"))
                self.assertEqual(index["schema"], "daxqo.rootfs/1")
                paths = {entry["path"]: entry for entry in index["entries"]}
                self.assertEqual(result["fileCount"], len(index["entries"]))
                self.assertEqual(result["regularFileCount"], sum(item["type"] == "file" for item in index["entries"]))
                self.assertEqual(paths["bin"]["type"], "symlink")
                self.assertEqual(paths["bin"]["target"], "/usr/bin")
                self.assertIn("opt/daxqo/static/nested/index.html", paths)
                self.assertIn("opt/daxqo/motor/engine.py", paths)
                self.assertIn("opt/daxqo/trajectory/collector.py", paths)
                self.assertIn("opt/daxqo/runtime/mail-libs/dkim.py", paths)
                self.assertIn("opt/daxqo/runtime/codex-0.163.0/codex-path/rg", paths)
                self.assertIn("dev", paths)
                self.assertIn("proc", paths)
                self.assertIn("var/tmp", paths)
                self.assertIn("opt/daxqo/state", paths)
                self.assertIn("opt/daxqo/workspace", paths)
                self.assertIn("opt/daxqo/update-stage", paths)
                self.assertIn("opt/daxqo/motor/LICENSE", paths)
                self.assertIn("opt/daxqo/trajectory/LICENSE", paths)
                self.assertIn("opt/daxqo/LICENSE", paths)
                self.assertIn("opt/daxqo/assistant_client.py", paths)
                self.assertNotIn("opt/daxqo/scripts/other.py", paths)
                self.assertFalse(any("__pycache__" in p or p.endswith(".pyc") for p in paths))
                self.assertFalse(any("machine-id" in p or p.startswith("root/") or "boot.log" in p for p in paths))
                self.assertFalse(any("\\" in p for p in paths))
                self.assertNotIn("bin", zf.namelist())  # links are represented by the index only
                self.assertEqual(zf.read("etc/os-release"), b"Ubuntu\n")
                self.assertEqual(zf.read("etc/resolv.conf"), b"# DAXQO fallback DNS; Android LinkProperties may replace this file.\nnameserver 1.1.1.1\nnameserver 223.5.5.5\n")
                self.assertEqual(zf.read("opt/daxqo/runtime/codex-0.163.0/bin/codex"), b"codex")

    def test_rejects_unpinned_inputs_without_test_flag(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            rootfs = root / "root.tar.xz"
            with tarfile.open(rootfs, "w:xz") as tar:
                add_tar_file(tar, "etc/os-release", b"test")
            with tempfile.TemporaryDirectory() as scratch:
                with self.assertRaisesRegex(ValueError, "rootfs SHA-256 mismatch"):
                    builder._tar_entries(rootfs, allow_test_inputs=False, scratch=Path(scratch))

    def test_rejects_backslashes_in_non_rootfs_archive_names(self):
        with self.assertRaisesRegex(ValueError, "backslash"):
            builder.safe_name("opt/daxqo/bad\\name.py")


if __name__ == "__main__":
    unittest.main()
