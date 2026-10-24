import os
from pathlib import Path
import socket
import stat
import tempfile
import unittest

from artifacts import image_type, read_file, referenced_files
from server import Harness


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "workspace"
        self.root.mkdir()
        self.state = Path(self.temp.name) / "state"
        self.private = self.root / "private"
        self.private.mkdir()
        self.resolver = lambda relative: Harness.resolve_workspace_path(
            type("ResolverHarness", (), {"workspace": self.root, "provider_dir": self.private})(), relative
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_markdown_absolute_backtick_chinese_spaces_and_dedup(self):
        target = self.root / "中文 文件.txt"
        target.write_text("content", encoding="utf-8")
        other = self.root / "另一个 文件.md"
        other.write_text("other", encoding="utf-8")
        text = f"[链接](<{target.as_posix()}>) `{other.as_posix()}` `{target.as_posix()}`"
        items = referenced_files(text, self.root, self.resolver)
        self.assertEqual([item["path"] for item in items], [target.name, other.name])

    def test_expands_only_requested_directory_and_filters_private_hidden_and_symlink(self):
        folder = self.root / "chosen"
        folder.mkdir()
        (folder / "keep.txt").write_text("keep", encoding="utf-8")
        (folder / "AGENTS.md").write_text("instructions", encoding="utf-8")
        (folder / ".hidden.txt").write_text("hidden", encoding="utf-8")
        other = self.root / "other"
        other.mkdir()
        (other / "sibling.txt").write_text("sibling", encoding="utf-8")
        outside = Path(self.temp.name) / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        try:
            (folder / "leak.txt").symlink_to(outside)
        except (OSError, NotImplementedError):
            pass
        (self.private / "secret.txt").write_text("private", encoding="utf-8")

        items = referenced_files(f"`{folder.as_posix()}`", self.root, self.resolver, expand_dirs=True)
        self.assertEqual([item["path"] for item in items], ["chosen/keep.txt"])

    def test_rejects_external_parent_missing_and_private_paths(self):
        outside = Path(self.temp.name) / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        references = [
            f"[outside](<{outside.as_posix()}>)",
            "`../outside.txt`",
            "`missing.txt`",
            "`private/secret.txt`",
        ]
        (self.private / "secret.txt").write_text("private", encoding="utf-8")
        for reference in references:
            with self.subTest(reference=reference):
                self.assertEqual(referenced_files(reference, self.root, self.resolver), [])

    def test_rejects_symlink_escape_when_supported(self):
        outside = Path(self.temp.name) / "escape.txt"
        outside.write_text("outside", encoding="utf-8")
        link = self.root / "escape.txt"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation is unavailable")
        self.assertEqual(referenced_files(f"`{link.as_posix()}`", self.root, self.resolver), [])

    def test_read_file_limit_and_directory_paths(self):
        file_path = self.root / "data.txt"
        file_path.write_bytes(b"12345")
        with self.assertRaises(OverflowError):
            read_file(self.resolver, "data.txt", limit=4)
        directory = self.root / "folder"
        directory.mkdir()
        with self.assertRaises(ValueError):
            read_file(self.resolver, "folder")

    def test_read_file_rejects_unix_socket_path(self):
        if not hasattr(socket, "AF_UNIX"):
            self.skipTest("Unix domain sockets are unavailable")
        socket_path = self.root / "socket-file"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            try:
                listener.bind(str(socket_path))
            except PermissionError as exc:
                self.skipTest(f"Unix socket creation denied by host policy: {exc.strerror or 'permission denied'}")
            with self.assertRaises(ValueError):
                read_file(self.resolver, "socket-file")
        finally:
            listener.close()

    @unittest.skipUnless(os.name == "posix", "Character-device fixture requires POSIX")
    def test_read_file_rejects_real_character_device(self):
        device = Path(os.devnull)
        if not stat.S_ISCHR(device.stat().st_mode):
            self.skipTest("os.devnull is not a character device on this host")
        resolver = lambda _relative: (device, os.fspath(device))
        with self.assertRaises(ValueError):
            read_file(resolver, "devnull")

    @unittest.skipUnless(os.name == "posix", "FIFO fixtures require POSIX")
    def test_read_file_rejects_fifo_when_creation_is_permitted(self):
        fifo = self.root / "pipe"
        try:
            os.mkfifo(fifo)
        except PermissionError as exc:
            self.skipTest(f"FIFO creation denied by host policy: {exc.strerror or 'permission denied'}")
        with self.assertRaises(ValueError):
            read_file(self.resolver, "pipe")

    def test_image_type_accepts_raster_signatures_and_rejects_markup(self):
        examples = {
            b"\x89PNG\r\n\x1a\nrest": "image/png",
            b"\xff\xd8\xffrest": "image/jpeg",
            b"GIF89arest": "image/gif",
            b"RIFF\x00\x00\x00\x00WEBPrest": "image/webp",
            b"BMrest": "image/bmp",
        }
        for raw, expected in examples.items():
            with self.subTest(expected=expected):
                self.assertEqual(image_type(raw), expected)
        for raw in (b"<svg xmlns='http://www.w3.org/2000/svg'>", b"<html><body>x</body></html>"):
            with self.subTest(raw=raw[:10]), self.assertRaises(ValueError):
                image_type(raw)


if __name__ == "__main__":
    unittest.main()
