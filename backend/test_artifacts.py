import os
from pathlib import Path
import sqlite3
import socket
import stat
import tempfile
import threading
import time
import unittest
import uuid

from artifacts import image_type, read_file, referenced_files
import server
import test_server as server_fixtures
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


class TaskWorkspaceTests(unittest.TestCase):
    """Exercise real Store/Harness workspace isolation with the bounded fake app-server fixture."""

    def setUp(self):
        # Reuse fixture setup only; this class does not inherit its 200+ tests.
        server_fixtures.HarnessTests.setUp(self)

    def tearDown(self):
        server_fixtures.HarnessTests.tearDown(self)

    def _wait_completed(self, task_id, timeout=4.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            row = self.harness.store.row(task_id)
            if row and row["status"] not in ("queued", "running"):
                self.assertEqual(row["status"], "completed", row.get("error"))
                return row
            time.sleep(0.01)
        self.fail(f"task did not finish within {timeout}s: {task_id}")

    def test_parallel_tasks_have_private_same_named_outputs_and_scoped_gui_email_artifacts(self):
        calls = []
        original_request = self.engine.request
        request_lock = threading.Lock()

        def recording_request(method, params, *args, **kwargs):
            if method in ("thread/start", "thread/resume", "turn/start"):
                with request_lock:
                    calls.append((method, dict(params)))
            return original_request(method, params, *args, **kwargs)

        self.engine.request = recording_request
        first = self.harness.start_task({"prompt": "create first local result"})
        # This legacy fake uses one fixed thread ID. Finish its first turn
        # before creating the second, then exercise concurrent file writes below.
        self._wait_completed(first["id"])
        second = self.harness.start_task({"prompt": "create second local result"})
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(first["workspacePath"], "tasks/" + first["id"])
        self.assertEqual(second["workspacePath"], "tasks/" + second["id"])
        self.assertNotEqual(self.harness.task_workspace(self.harness.store.row(first["id"])),
                            self.harness.task_workspace(self.harness.store.row(second["id"])))
        self._wait_completed(second["id"])

        first_root = self.harness.task_workspace(self.harness.store.row(first["id"]))
        second_root = self.harness.task_workspace(self.harness.store.row(second["id"]))
        writers = [threading.Thread(target=(root / "same.txt").write_text,
                                    args=(content,), kwargs={"encoding": "utf-8"})
                   for root, content in ((first_root, "first only"), (second_root, "second only"))]
        for writer in writers: writer.start()
        for writer in writers:
            writer.join(timeout=1.0)
            self.assertFalse(writer.is_alive(), "parallel task output write did not finish")
        self.assertEqual((first_root / "same.txt").read_text(encoding="utf-8"), "first only")
        self.assertEqual((second_root / "same.txt").read_text(encoding="utf-8"), "second only")
        out = first_root / "outputs"
        out.mkdir()
        (out / "report.txt").write_text("private output", encoding="utf-8")
        (out / ".hidden.txt").write_text("hidden", encoding="utf-8")
        (out / "AGENTS.md").write_text("instructions", encoding="utf-8")
        first_result = f"[relative](same.txt) [absolute](<{(first_root / 'same.txt').as_posix()}>) `outputs`"
        second_result = "`same.txt`"
        self.harness.store.update(first["id"], result=first_result)
        self.harness.store.update(second["id"], result=second_result)
        self.harness.store.add_message(first["id"], "assistant", first_result, "completed")

        first_artifacts = self.harness.result_artifacts(first_result, expand_dirs=True, task_id=first["id"])
        self.assertEqual({item["path"] for item in first_artifacts}, {
            f"tasks/{first['id']}/same.txt", f"tasks/{first['id']}/outputs/report.txt"})
        second_artifacts = self.harness.result_artifacts(second_result, task_id=second["id"])
        self.assertEqual([item["path"] for item in second_artifacts], [f"tasks/{second['id']}/same.txt"])
        escaped_text = f"`{(first_root / 'same.txt').as_posix()}`"
        self.assertEqual(self.harness.result_artifacts(escaped_text, task_id=second["id"]), [])
        self.assertEqual(self.harness.result_artifacts("`tasks/" + second["id"] + "/same.txt`",
                                                       task_id=first["id"]), [])
        gui_paths = {item["path"] for item in self.harness.task_detail(first["id"])["artifacts"]}
        self.assertEqual(gui_paths, {f"tasks/{first['id']}/same.txt", f"tasks/{first['id']}/outputs"})
        gui_messages = self.harness.task_messages(first["id"])
        assistant = next(message for message in gui_messages
                         if message["role"] == "assistant" and message["text"] == first_result)
        self.assertEqual({item["path"] for item in assistant["artifacts"]}, gui_paths)

        # The email collector must bind the report taskId to the submitting contact.
        stamp = server.now()
        contact_a, contact_b = "contact-workspace-a", "contact-workspace-b"
        with self.harness.store.lock, self.harness.store.db:
            for ident, email in ((contact_a, "a@example.invalid"), (contact_b, "b@example.invalid")):
                self.harness.store.db.execute(
                    "INSERT INTO mail_contacts(id,email,secret,created_at) VALUES(?,?,?,?)",
                    (ident, email, "secret-" + ident, stamp))
            self.harness.store.db.execute(
                "INSERT INTO mail_commands(id,contact_id,nonce,action,task_id,status,received_at) VALUES(?,?,?,'submit',?,'completed',?)",
                ("cmd-a", contact_a, "nonce-a", first["id"], stamp))
            self.harness.store.db.execute(
                "INSERT INTO mail_commands(id,contact_id,nonce,action,task_id,status,received_at) VALUES(?,?,?,'submit',?,'completed',?)",
                ("cmd-b", contact_b, "nonce-b", second["id"], stamp))
        report_a = {"taskId": first["id"], "status": "completed"}
        report_b = {"taskId": second["id"], "status": "completed"}
        groups_a, _ = self.harness.remote._attachment_groups(report_a, {"id": contact_a})
        groups_b, _ = self.harness.remote._attachment_groups(report_b, {"id": contact_b})
        denied, _ = self.harness.remote._attachment_groups(report_b, {"id": contact_a})
        self.assertEqual({item["path"] for item in groups_a[0]},
                         {f"tasks/{first['id']}/same.txt", f"tasks/{first['id']}/outputs/report.txt"})
        self.assertEqual([item["path"] for item in groups_b[0]], [f"tasks/{second['id']}/same.txt"])
        self.assertEqual(denied, [[]])

        starts = [params for method, params in calls if method == "thread/start"]
        self.assertEqual({params["cwd"] for params in starts}, {str(first_root), str(second_root)})

        # Completed-task follow-up stays on the same thread; resume reloads that thread in its task cwd.
        starts_before_followup = sum(method == "turn/start" for method, _ in calls)
        self.harness.send_message(first["id"], {"text": "continue here", "clientMessageId": "workspace-followup"})
        self._wait_completed(first["id"])
        followup_starts = [params for method, params in calls if method == "turn/start"]
        self.assertGreater(len(followup_starts), starts_before_followup)
        self.assertEqual(followup_starts[-1].get("threadId"), "thread-fixture")
        self.harness.store.update(first["id"], status="interrupted")
        self.harness._loaded_threads.discard("thread-fixture")
        self.harness.start_task({}, first["id"])
        self._wait_completed(first["id"])
        resumes = [params for method, params in calls if method == "thread/resume"]
        self.assertTrue(any(params.get("cwd") == str(first_root) and params.get("threadId") == "thread-fixture"
                            for params in resumes))

    def test_old_schema_migrates_without_moving_legacy_workspace_files(self):
        legacy_workspace = self.harness.workspace / "legacy-root-file.txt"
        legacy_workspace.write_text("keep in original root", encoding="utf-8")
        db_path = self.harness.state / "legacy.sqlite3"
        connection = sqlite3.connect(db_path)
        connection.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY,title TEXT NOT NULL,prompt TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,thread_id TEXT,model TEXT,result TEXT,error TEXT,stop_requested INTEGER NOT NULL DEFAULT 0)")
        ident = "legacy-task"
        stamp = server.now()
        connection.execute("INSERT INTO tasks(id,title,prompt,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                           (ident, "legacy", "old task", "completed", stamp, stamp))
        connection.commit()
        connection.close()

        migrated = server.Store(db_path)
        try:
            row = migrated.row(ident)
            self.assertIsNone(row["workspace_path"])
            self.assertEqual(self.harness.task_workspace(row), self.harness.workspace.resolve())
            self.assertEqual(legacy_workspace.read_text(encoding="utf-8"), "keep in original root")
            self.assertFalse((self.harness.workspace / "tasks" / ident).exists())
        finally:
            migrated.db.close()

    def test_malicious_workspace_paths_and_workspace_links_are_rejected(self):
        ident = str(uuid.uuid4())
        for row in (
            {"id": ident, "workspace_path": "tasks/../outside"},
            {"id": ident, "workspace_path": "tasks/" + str(uuid.uuid4())},
            {"id": "not-a-uuid", "workspace_path": "tasks/not-a-uuid"},
            {"id": ident, "workspace_path": "../tasks/" + ident},
        ):
            with self.subTest(row=row), self.assertRaises(ValueError):
                self.harness.task_workspace(row, create=True)

        tasks = self.root / "tasks"
        tasks.mkdir(exist_ok=True)
        outside = Path(self.temp.name) / "outside-task"
        outside.mkdir()
        link = tasks / ident
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"task workspace symlink creation is unavailable: {exc}")
        with self.assertRaises(ValueError):
            self.harness.task_workspace({"id": ident, "workspace_path": "tasks/" + ident})


if __name__ == "__main__":
    unittest.main()
