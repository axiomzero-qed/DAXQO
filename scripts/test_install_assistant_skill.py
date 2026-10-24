from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import shlex

from install_assistant_skill import InstallError, install


class InstallAssistantSkillTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = self.root / "phone app"
        self.app.mkdir()
        (self.app / "daxqo_handoff_SKILL.md").write_text(
            "Run python3 /root/phone-harness/assistant_client.py daxqo_submit\n", encoding="utf-8")
        self.skills = self.root / "existing skills"
        self.skills.mkdir()
        self.fallback = self.root / "fallback.md"
        self.fallback.write_text("fallback", encoding="utf-8")
        self.target = self.skills / "daxqo-handoff" / "SKILL.md"

    def tearDown(self):
        self.temp.cleanup()

    def test_preview_does_not_write_and_targets_named_skill_only(self):
        other = self.skills / "other-skill"
        other.mkdir()
        (other / "SKILL.md").write_text("keep", encoding="utf-8")
        result = install(self.app, self.skills, do_install=False, fallback=self.fallback)
        self.assertIn(str(self.target), result)
        self.assertFalse(self.target.exists())
        self.assertEqual((other / "SKILL.md").read_text(encoding="utf-8"), "keep")

    def test_install_and_identical_file_are_noop(self):
        first = install(self.app, self.skills, do_install=True, fallback=self.fallback)
        content = self.target.read_text(encoding="utf-8")
        self.assertIn(shlex.quote((self.app / "assistant_client.py").as_posix()), content)
        second = install(self.app, self.skills, do_install=True, fallback=self.fallback)
        self.assertIn("Already current", second)
        self.assertEqual(first, f"Installed {self.target}")
        self.assertEqual(list(self.target.parent.glob("*.backup-*")), [])

    def test_different_file_is_backed_up_before_update(self):
        self.target.parent.mkdir()
        self.target.write_text("unrelated existing file", encoding="utf-8")
        result = install(self.app, self.skills, do_install=True, fallback=self.fallback)
        backups = list(self.target.parent.glob("SKILL.md.backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), "unrelated existing file")
        self.assertIn("preserved previous file", result)
        self.assertIn("daxqo_submit", self.target.read_text(encoding="utf-8"))

    def test_symlink_in_target_is_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        link = self.skills / "daxqo-handoff"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation is unavailable on this platform")
        with self.assertRaises(InstallError):
            install(self.app, self.skills, do_install=True, fallback=self.fallback)
        self.assertFalse((outside / "SKILL.md").exists())

    def test_symlinked_app_skill_source_is_rejected(self):
        source = self.app / "daxqo_handoff_SKILL.md"
        source.unlink()
        external = self.root / "external-skill.md"
        external.write_text("must not be read through a link", encoding="utf-8")
        try:
            source.symlink_to(external)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation is unavailable on this platform")
        with self.assertRaises(InstallError):
            install(self.app, self.skills, do_install=False, fallback=self.fallback)


if __name__ == "__main__":
    unittest.main()
