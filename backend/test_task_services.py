"""Behavior tests for task_services using isolated SQLite and harness fixtures."""
from __future__ import annotations

import datetime as dt
import errno
import json
import io
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import quote

from task_services import MAX_UPLOAD, SHANGHAI, TaskServices, stamp


class FixtureStore:
    """Test fixture: a small SQLite store matching the task-service contract."""

    def __init__(self):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(":memory:", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute(
            "CREATE TABLE tasks (id TEXT PRIMARY KEY,title TEXT,prompt TEXT,status TEXT,error TEXT)"
        )

    def rows(self):
        with self.lock:
            return self.db.execute("SELECT * FROM tasks ORDER BY rowid").fetchall()

    def close(self):
        self.db.close()


class FixtureHarness:
    """Test fixture: filesystem and task operations, with no server/model calls."""

    def __init__(self, workspace):
        self.store = FixtureStore()
        self.lock = threading.RLock()
        self.workspace = Path(workspace)
        self.reconnect_error = None
        self.started = 0

    def resolve_workspace_path(self, relative):
        candidate = (self.workspace / relative).resolve(strict=True)
        root = self.workspace.resolve(strict=True)
        if not candidate.is_relative_to(root):
            raise ValueError("workspace path escapes root")
        return candidate, candidate.relative_to(root).as_posix()

    @staticmethod
    def safe_text(value, limit=300):
        return str(value)[:limit]

    def reconnect(self):
        if self.reconnect_error:
            raise RuntimeError(self.reconnect_error)

    def start_task(self, body):
        self.started += 1
        ident = f"fixture-task-{self.started}"
        with self.store.lock, self.store.db:
            self.store.db.execute(
                "INSERT INTO tasks(id,title,prompt,status,error) VALUES(?,?,?,?,NULL)",
                (ident, body["title"], body["prompt"], "queued"),
            )
        return {"id": ident, "title": body["title"], "status": "queued"}


class FixtureHandler:
    """Test fixture: bounded request body resembling a raw upload handler."""

    def __init__(self, body, name="attachment.bin", declared_size=None):
        self.path = "/api/uploads?name=" + quote(name)
        self.headers = {"Content-Length": str(len(body) if declared_size is None else declared_size)}
        self.rfile = io.BytesIO(body)
        self.connection = None


class TaskServicesFixtureCase(unittest.TestCase):
    """Test fixture: each case gets a fresh temporary workspace and SQLite store."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="daxqo-task-services-")
        self.workspace = Path(self.temp.name) / "workspace"
        self.workspace.mkdir()
        self.harness = FixtureHarness(self.workspace)
        self.now = dt.datetime(2026, 10, 22, 4, 0, tzinfo=dt.timezone.utc)
        self.services = TaskServices(self.harness, clock=lambda: self.now)

    def tearDown(self):
        self.services.close()
        self.harness.store.close()
        self.temp.cleanup()

    def upload(self, data, name="fixture.bin", declared_size=None):
        return self.services.upload(FixtureHandler(data, name, declared_size))["attachment"]

    def make_schedule(self, kind="once", **extra):
        body = {"title": "Fixture schedule", "prompt": "Run fixture", "kind": kind}
        body.update(extra)
        return self.services.create_schedule(body)


class SettingsFixtureTests(TaskServicesFixtureCase):
    """Settings behavior using only the isolated fixture harness."""

    def test_integer_bounds_and_boolean_rejection(self):
        for value in (0, 5, True, 1.5, "2"):
            with self.subTest(maxConcurrentTasks=value), self.assertRaises(ValueError):
                self.services.save_settings({"maxConcurrentTasks": value})
        accepted = self.services.save_settings({"maxConcurrentTasks": 4})
        self.assertEqual(accepted["settings"]["maxConcurrentTasks"], 4)
        for value in (0, 5, True, 2.0):
            with self.subTest(maxSubagents=value), self.assertRaises(ValueError):
                self.services.save_settings({"maxSubagents": value})
        with self.assertRaises(ValueError):
            self.services.save_settings({"subagentsEnabled": 1})

    def test_runtime_settings_rejected_while_active_but_native_limit_can_change(self):
        self.harness.start_task({"title": "active", "prompt": "fixture"})
        self.services.save_settings({"maxConcurrentTasks": 3})
        with self.assertRaisesRegex(ValueError, "running"):
            self.services.save_settings({"contextWindow": 131072})
        self.assertEqual(self.services.settings["contextWindow"], 65536)

    def test_failed_reconnect_rolls_back_persisted_and_effective_settings(self):
        self.harness.reconnect_error = "fixture reconnect failure"
        old = dict(self.services.settings)
        with self.assertRaisesRegex(ValueError, "设置已恢复"):
            self.services.save_settings({"contextWindow": 131072})
        self.assertEqual(self.services.settings, old)
        self.assertEqual(self.services.effective_settings, old)
        row = self.harness.store.db.execute("SELECT value FROM daxqo_settings WHERE id=1").fetchone()
        self.assertEqual(json.loads(row[0]), old)


class UploadFixtureTests(TaskServicesFixtureCase):
    """Upload behavior using in-memory request bodies and a temporary workspace."""

    def test_upload_accepts_exact_limit_and_rejects_oversize_before_reading(self):
        attachment = self.upload(b"x" * MAX_UPLOAD)
        self.assertEqual(attachment["size"], MAX_UPLOAD)
        with self.assertRaises(OverflowError):
            self.services.upload(FixtureHandler(b"", declared_size=MAX_UPLOAD + 1))
        self.assertEqual(len(list((self.workspace / "imports").iterdir())), 1)

    def test_incomplete_and_empty_upload_policy(self):
        with self.assertRaisesRegex(ValueError, "不完整"):
            self.services.upload(FixtureHandler(b"short", declared_size=10))
        self.assertEqual(list((self.workspace / "imports").iterdir()), [])
        self.assertEqual(self.harness.store.db.execute("SELECT count(*) FROM uploads").fetchone()[0], 0)
        empty = self.upload(b"", name="empty.bin")
        self.assertEqual(empty["size"], 0)
        self.assertEqual((self.workspace / empty["path"]).read_bytes(), b"")

    def test_filename_traversal_and_symlink_import_directory_rejected(self):
        for name in ("../escape", "nested/file.txt", "..", "C:secret"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.upload(b"x", name=name)
        imports = self.workspace / "imports"
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        try:
            imports.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            if exc.errno in (errno.EPERM, errno.EACCES) or getattr(exc, "winerror", None) == 1314:
                self.skipTest("symlink creation is disabled by this Windows environment")
            raise
        with self.assertRaisesRegex(ValueError, "链接"):
            self.upload(b"x")

    def test_prepare_body_rejects_symlinked_attachment_file(self):
        attachment = self.upload(b"valid")
        path = self.workspace / attachment["path"]
        outside = Path(self.temp.name) / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        path.unlink()
        try:
            path.symlink_to(outside)
        except OSError as exc:
            if exc.errno in (errno.EPERM, errno.EACCES) or getattr(exc, "winerror", None) == 1314:
                self.skipTest("symlink creation is disabled by this Windows environment")
            raise
        with self.assertRaises(ValueError):
            self.services.prepare_body({"text": "read", "attachments": [attachment["id"]]})


class PrepareBodyFixtureTests(TaskServicesFixtureCase):
    """Attachment binding and repeatability checked against fixture upload rows."""

    def test_attachment_ids_are_resolved_from_store_and_input_stays_stable(self):
        attachment = self.upload(b"fixture", name="bound.txt")
        body = {"text": "Please inspect", "attachments": [attachment["id"]]}
        original = {key: list(value) if isinstance(value, list) else value for key, value in body.items()}
        first = self.services.prepare_body(body)
        second = self.services.prepare_body(body)
        self.assertEqual(body, original)
        self.assertEqual(first, second)
        self.assertEqual(first["text"].count("本次导入的附件"), 1)
        self.assertIn("bound.txt", first["text"])
        forged = {"text": "read", "attachments": ["../../outside"]}
        with self.assertRaisesRegex(ValueError, "附件不存在"):
            self.services.prepare_body(forged)
        self.assertEqual(forged["text"], "read")


class ScheduleFixtureTests(TaskServicesFixtureCase):
    """Scheduler behavior with injected time and fixture task creation."""

    def test_once_runs_once_and_is_disabled_after_automatic_claim(self):
        schedule = self.make_schedule("once", runAt=stamp(self.now + dt.timedelta(seconds=1)))
        self.now += dt.timedelta(seconds=1)
        self.services.tick()
        row = self.services.schedule(schedule["id"])
        self.assertFalse(row["enabled"])
        self.assertEqual(row["lastStatus"], "running")
        self.services.tick()
        self.assertEqual(self.harness.started, 1)
        self.assertEqual(self.harness.store.db.execute("SELECT count(*) FROM schedule_runs").fetchone()[0], 1)

    def test_daily_time_is_interpreted_in_beijing_timezone(self):
        self.now = dt.datetime(2026, 10, 22, 4, 0, tzinfo=dt.timezone.utc)  # 12:00 Beijing
        schedule = self.make_schedule("daily", time="13:15")
        due = dt.datetime.fromisoformat(schedule["nextRunAt"])
        self.assertEqual(due, dt.datetime(2026, 10, 22, 5, 15, tzinfo=dt.timezone.utc))
        self.assertEqual(due.astimezone(SHANGHAI).strftime("%H:%M"), "13:15")

    def test_interval_does_not_overlap_and_reconciles_terminal_task(self):
        schedule = self.make_schedule("interval", intervalMinutes=5)
        launched = self.services.run_schedule(schedule["id"])
        with self.assertRaisesRegex(ValueError, "already running"):
            self.services.run_schedule(schedule["id"])
        task_id = launched["task"]["id"]
        with self.harness.store.db:
            self.harness.store.db.execute("UPDATE tasks SET status='completed' WHERE id=?", (task_id,))
        result = self.services.schedule(schedule["id"])
        self.services._reconcile()
        result = self.services.schedule(schedule["id"])
        self.assertEqual(result["lastStatus"], "completed")
        self.assertEqual(result["lastRuns"][0]["status"], "completed")
        self.assertIsNotNone(result["lastRuns"][0]["finishedAt"])

    def test_full_capacity_defers_due_interval_without_overlapping_tasks(self):
        self.make_schedule("interval", intervalMinutes=1)
        self.harness.start_task({"title": "occupies capacity", "prompt": "fixture"})
        self.services.tick()
        self.assertEqual(self.harness.started, 1)
        self.assertEqual(self.harness.store.db.execute("SELECT count(*) FROM schedule_runs").fetchone()[0], 0)
        self.assertEqual(self.services.schedules()[0]["nextRunAt"], stamp(self.now + dt.timedelta(minutes=1)))

    def test_pause_resume_preserves_daily_schedule_and_recomputes_future_time(self):
        schedule = self.make_schedule("daily", time="13:30")
        paused = self.services.toggle_schedule(schedule["id"], False)
        self.assertFalse(paused["enabled"])
        self.assertIsNone(paused["nextRunAt"])
        self.now += dt.timedelta(hours=2)
        resumed = self.services.toggle_schedule(schedule["id"], True)
        self.assertTrue(resumed["enabled"])
        due = dt.datetime.fromisoformat(resumed["nextRunAt"])
        self.assertEqual(due.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M"), "2026-10-23 13:30")

    def test_claimed_run_after_crash_is_interrupted_and_not_retried(self):
        schedule = self.make_schedule("interval", intervalMinutes=5)
        run_id = "fixture-claimed-run"
        with self.harness.store.db:
            self.harness.store.db.execute(
                "INSERT INTO schedule_runs VALUES(?,?,?,?,?,?,?)",
                (run_id, schedule["id"], None, "claimed", stamp(self.now), None, None),
            )
        recovered = TaskServices(self.harness, clock=lambda: self.now)
        try:
            self.assertEqual(self.harness.store.db.execute("SELECT status FROM schedule_runs WHERE id=?", (run_id,)).fetchone()[0], "interrupted")
            recovered.tick()
            self.assertEqual(self.harness.started, 0)
            self.assertEqual(self.harness.store.db.execute("SELECT count(*) FROM schedule_runs").fetchone()[0], 1)
        finally:
            recovered.close()


if __name__ == "__main__":
    unittest.main()
