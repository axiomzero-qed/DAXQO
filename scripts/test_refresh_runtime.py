"""Focused, side-effect-free tests for refresh_runtime CLI and staged version parsing."""
from __future__ import annotations

import unittest
from pathlib import Path

from scripts import refresh_runtime as refresh


class RefreshRuntimeCliTests(unittest.TestCase):
    def test_custom_paths_are_parsed_and_passed_to_detached_worker(self):
        args = refresh.build_parser().parse_args([
            "--launch", "detach", "--sha256", "a" * 64,
            "--app", "/home/daxqo/.local/share/daxqo",
            "--stage", "/home/daxqo/.local/share/daxqo/update-stage",
            "--manager-stage", "/home/daxqo/.local/share/daxqo/update-stage",
        ])
        command = refresh.build_worker_command(args)
        self.assertEqual(command[command.index("--app") + 1], str(Path("/home/daxqo/.local/share/daxqo")))
        self.assertEqual(command[command.index("--stage") + 1], str(Path("/home/daxqo/.local/share/daxqo/update-stage")))
        self.assertEqual(command[command.index("--manager-stage") + 1], str(Path("/home/daxqo/.local/share/daxqo/update-stage")))
        self.assertEqual(command[command.index("--launch") + 1], "worker")
        self.assertEqual(command[command.index("--sha256") + 1], "a" * 64)

    def test_original_path_defaults_are_preserved(self):
        args = refresh.build_parser().parse_args(["--launch", "worker", "--sha256", "b" * 64])
        self.assertEqual(args.app, Path("/root/phone-harness"))
        self.assertEqual(args.stage, Path("/sdcard/Download/daxqo-stage"))
        self.assertEqual(args.manager_stage, Path("/sdcard/Download/daxqo-stage/backend"))


class StagedVersionParsingTests(unittest.TestCase):
    def test_reads_one_literal_semver_assignment(self):
        source = b'APP_VERSION = "0.6.1"\n'
        self.assertEqual(refresh.parse_app_version(source), "0.6.1")

    def test_rejects_missing_ambiguous_computed_and_invalid_versions(self):
        cases = (
            b"DEFAULT_PORT = 18882\n",
            b'APP_VERSION = "0.6.1"\nAPP_VERSION = "0.6.2"\n',
            b'APP_VERSION = "0.6" + ".1"\n',
            b'APP_VERSION = "0.06.1"\n',
            b'APP_VERSION = "0.6.1-01"\n',
            b'APP_VERSION = "latest"\n',
            b'APP_VERSION = 6\n',
        )
        for source in cases:
            with self.subTest(source=source), self.assertRaises(refresh.RefreshError):
                refresh.parse_app_version(source)

    def test_rejects_non_utf8_or_invalid_python(self):
        for source in (b"\xff", b'APP_VERSION = "0.6.1"\nif\n'):
            with self.subTest(source=source), self.assertRaises(refresh.RefreshError):
                refresh.parse_app_version(source)


if __name__ == "__main__":
    unittest.main()
