import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import zipfile


SCRIPT = Path(__file__).with_name("package_v1.py")
SPEC = importlib.util.spec_from_file_location("package_v1", SCRIPT)
package_v1_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(package_v1_module)


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def repo(root: Path, files: dict[str, bytes]) -> None:
    root.mkdir(parents=True)
    git(root, "init", "-q")
    git(root, "config", "user.name", "Source Snapshot Test")
    git(root, "config", "user.email", "snapshot@example.invalid")
    for relative, contents in files.items():
        target = root / Path(*relative.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(contents)
    git(root, "add", ".")
    git(root, "commit", "-qm", "fixture")


class PackageV1Tests(unittest.TestCase):
    def test_snapshot_contains_working_sources_and_hash_manifest_but_not_private_or_generated_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            main, motor, collector = root / "main", root / "motor", root / "collector"
            repo(main, {".gitignore": b"ignored-secret.json\n", "README.md": b"old readme\n",
                        "LICENSE": b"MIT", "THIRD_PARTY_NOTICES.md": b"notices",
                        "tests/fixtures/official.wav": b"wave fixture", "src/main.py": b"print('main')\n"})
            repo(motor, {"LICENSE": b"Motor license", "motor/engine.py": b"engine"})
            repo(collector, {"LICENSE": b"Collector license", "trajectory/collector.py": b"collector"})
            (main / "README.md").write_text("current dirty README\n", encoding="utf-8")
            current_readme = (main / "README.md").read_bytes()
            (main / "new_source.py").write_text("new non-ignored source\n", encoding="utf-8")
            for relative, data in {
                ".tools/local-token": b"ignored cache",
                "dist/old.apk": b"apk",
                "evidence/device.json": b"device data",
                "state/harness.sqlite3": b"database",
                "credentials/account.json": b"credential data",
                "provider.json": b"user config",
                "build/generated.py": b"build output",
                "data/captured-trace.json": b"device trace",
                "ignored-secret.json": b"ignored by git",
            }.items():
                target = main / Path(*relative.split("/"))
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)

            output = root / "delivery" / "source.zip"
            result = package_v1_module.package_v1(motor, collector, output, main_root=main)
            self.assertTrue(result["ready"])
            first = output.read_bytes()
            package_v1_module.package_v1(motor, collector, output, main_root=main)
            self.assertEqual(output.read_bytes(), first, "fixed timestamps and content produce byte-identical ZIPs")
            with zipfile.ZipFile(output) as archive:
                names = set(archive.namelist())
                prefix = f"DAXQO-{package_v1_module.VERSION}/"
                self.assertIn(prefix + "README.md", names)
                self.assertIn(prefix + "new_source.py", names)
                self.assertIn(prefix + "tests/fixtures/official.wav", names)
                self.assertIn(prefix + "THIRD_PARTY_NOTICES.md", names)
                self.assertIn(prefix + "deps/DAXQO-Motor/motor/engine.py", names)
                self.assertIn(prefix + "deps/DAXQO-Trajectory-Collector/trajectory/collector.py", names)
                self.assertTrue(any(name.endswith("/SOURCE-MANIFEST.json") for name in names))
                self.assertFalse(any(".git/" in name or "/.tools/" in name or "/dist/" in name or "/evidence/" in name for name in names))
                for excluded in ("provider.json", "harness.sqlite3", "old.apk", "device.json", "local-token", "generated.py", "ignored-secret.json", "account.json", "captured-trace.json"):
                    self.assertFalse(any(name.endswith(excluded) for name in names), excluded)
                manifest_name = prefix + "SOURCE-MANIFEST.json"
                manifest = json.loads(archive.read(manifest_name))
                self.assertTrue(manifest["repositories"]["main"]["dirty"])
                self.assertTrue(manifest["repositories"]["motor"]["revision"])
                rows = {row["archive"]: row for row in manifest["files"]}
                file_name = prefix + "README.md"
                self.assertEqual(rows[file_name]["sha256"], hashlib.sha256(current_readme).hexdigest())

    def test_rejects_symlink_escape_and_missing_target(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            safe = root / "included.txt"
            safe.write_text("safe", encoding="utf-8")
            included = {"included.txt"}
            with self.assertRaisesRegex(package_v1_module.PackageError, "escapes its repository"):
                package_v1_module._resolve_link(root, "link", "../../outside", included)
            with self.assertRaisesRegex(package_v1_module.PackageError, "missing file"):
                package_v1_module._resolve_link(root, "link", "absent.txt", included)
            if hasattr(os, "symlink"):
                link = root / "link"
                try:
                    os.symlink("../../outside", link)
                except (OSError, NotImplementedError):
                    return
                with self.assertRaises(package_v1_module.PackageError):
                    package_v1_module._resolve_link(root, "link", os.readlink(link), included)


if __name__ == "__main__":
    unittest.main()
