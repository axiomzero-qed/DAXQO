import datetime as dt
from email.message import EmailMessage
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from email_control import MailControlService
from remote_protocol import sign, verify
from server import Store


class FakeHarness:
    def __init__(self, root):
        self.state = root / "state"
        self.provider_dir = self.state / "private"
        self.provider_dir.mkdir(parents=True)
        self.store = Store(self.state / "harness.sqlite3")
        self.start_calls = []
        self.stop_calls = []

    def safe_text(self, value, limit=10000):
        return str(value)[:limit]

    def start_task(self, body):
        self.start_calls.append(dict(body))
        ident, stamp = "task_" + str(len(self.start_calls)), dt.datetime.now(dt.timezone.utc).isoformat()
        with self.store.lock, self.store.db:
            self.store.db.execute("INSERT INTO tasks(id,title,prompt,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                                  (ident, body.get("title", "Remote task"), body["prompt"], "queued", stamp, stamp))
        return {"id": ident}

    def stop(self, task_id):
        self.stop_calls.append(task_id)
        return {"id": task_id}


class FakeSMTP:
    instances = []

    def __init__(self, *args, **kwargs):
        self.sent = []
        self.logged_in = None
        self.__class__.instances.append(self)

    def __enter__(self): return self
    def __exit__(self, *_): return False
    def login(self, user, password): self.logged_in = (user, password)
    def noop(self): return (250, b"ok")
    def send_message(self, message): self.sent.append(message)


class FakeIMAP:
    instances = []
    capabilities = (b"IMAP4REV1", b"ID")

    def __init__(self, *args, **kwargs):
        self.commands = []
        self.__class__.instances.append(self)

    def login(self, user, password): self.logged_in = (user, password)
    def _simple_command(self, name, value): self.commands.append((name, value)); return ("OK", [])
    def select(self, mailbox): self.mailbox = mailbox; return ("OK", [b"1"])
    def noop(self): return ("OK", [b"NOOP"])
    def logout(self): return ("BYE", [b"bye"])


class MailControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.harness = FakeHarness(Path(self.tmp.name))
        self.addCleanup(self.harness.store.db.close)
        self.service = MailControlService(self.harness)
        self.cfg = {"enabled": True, "accountAddress": "device@example.net", "username": "fixture-user",
                    "imapHost": "imap.example.net", "imapPort": 993, "smtpHost": "smtp.example.net",
                    "smtpPort": 465, "password": "fixture-mail-password"}
        self.service.save_settings(self.cfg)
        created = self.service.create_contact({"email": "owner@example.org"})
        self.contact = created["contact"]
        self.secret = created["pairingSecret"]
        FakeSMTP.instances.clear()

    def command_email(self, action="submit", *, sender="owner@example.org", contact="owner@example.org", secret=None,
                      nonce=None, **fields):
        stamp = dt.datetime.now(dt.timezone.utc)
        payload = {"v": 1, "action": action, "contact": contact, "nonce": nonce or uuid.uuid4().hex,
                   "sentAt": stamp.isoformat(), **fields}
        raw = json.dumps(sign(payload, secret or self.secret), ensure_ascii=False, separators=(",", ":"))
        msg = EmailMessage()
        msg["From"] = sender
        msg["To"] = "device@example.net"
        msg["Subject"] = "DAXQO/1 " + payload["nonce"]
        msg.set_content(raw)
        return msg.as_bytes()

    def commands(self):
        with self.harness.store.lock:
            return [dict(r) for r in self.harness.store.db.execute("SELECT * FROM mail_commands ORDER BY received_at")]

    def http(self, path, body):
        class Handler:
            def _body(self): return body
            def _json(self, status, result): self.response = (status, result)
        handler = Handler()
        self.assertTrue(self.service.handle_http(handler, "POST", path))
        return handler.response

    def test_public_settings_never_expose_password_or_contact_secret(self):
        public = self.service.public()
        blob = json.dumps(public)
        self.assertNotIn("fixture-mail-password", blob)
        self.assertNotIn(self.secret, blob)
        self.assertTrue(public["mail"]["passwordConfigured"])
        self.assertEqual(public["contacts"][0]["email"], "owner@example.org")

    def test_forged_from_and_wrong_hmac_are_ignored(self):
        self.service._process_email(self.command_email(sender="attacker@example.org"))
        self.service._process_email(self.command_email(secret="wrong synthetic secret", nonce=uuid.uuid4().hex))
        self.assertEqual(self.harness.start_calls, [])
        self.assertEqual(self.commands(), [])

    def test_valid_submit_is_claimed_once_and_replay_does_not_start_again(self):
        raw = self.command_email(prompt="check a harmless local file", title="fixture", nonce="a" * 32)
        self.service._process_email(raw)
        self.service._process_email(raw)
        self.assertEqual(len(self.harness.start_calls), 1)
        command = self.commands()[0]
        self.assertEqual((command["nonce"], command["task_id"], command["status"]), ("a" * 32, "task_1", "running"))
        self.assertIn("never request, expose, or transmit mailbox credentials", self.harness.start_calls[0]["prompt"])

    def test_status_and_stop_cannot_access_another_contact_task(self):
        other = self.service.create_contact({"email": "other@example.org"})
        task_id = self.harness.start_task({"prompt": "private task"})["id"]
        with self.harness.store.lock, self.harness.store.db:
            self.harness.store.db.execute("INSERT INTO mail_commands(id,contact_id,nonce,action,task_id,status,received_at) VALUES(?,?,?,?,?,?,?)",
                                          (str(uuid.uuid4()), other["contact"]["id"], "owner_nonce_0000000000000001", "submit", task_id, "running", "2026-01-01T00:00:00Z"))
        raw = self.command_email("stop", taskId=task_id)
        with patch("email_control.smtplib.SMTP_SSL", FakeSMTP):
            self.service._process_email(raw)
        latest = self.commands()[-1]
        self.assertEqual(latest["status"], "rejected")
        self.assertEqual(self.harness.stop_calls, [])
        self.assertEqual(len(FakeSMTP.instances), 1)
        denial = verify(json.loads(FakeSMTP.instances[0].sent[0].get_content()), self.secret)
        self.assertEqual(denial["report"]["status"], "rejected")
        self.assertNotIn(task_id, json.dumps(denial["report"]))

    def test_status_without_task_id_only_lists_callers_tasks(self):
        self.service._process_email(self.command_email(prompt="my task"))
        other = self.service.create_contact({"email": "other@example.org"})
        foreign_task = self.harness.start_task({"prompt": "other task"})["id"]
        with self.harness.store.lock, self.harness.store.db:
            self.harness.store.db.execute("INSERT INTO mail_commands(id,contact_id,nonce,action,task_id,status,received_at) VALUES(?,?,?,?,?,?,?)",
                                          (str(uuid.uuid4()), other["contact"]["id"], "foreign_nonce_0000000000000001", "submit", foreign_task, "running", "2026-01-01T00:00:00Z"))
        with patch("email_control.smtplib.SMTP_SSL", FakeSMTP):
            self.service._process_email(self.command_email("status"))
        report = verify(json.loads(FakeSMTP.instances[-1].sent[0].get_content()), self.secret)["report"]
        self.assertEqual([row["taskId"] for row in report["tasks"]], ["task_1"])

    def test_uncertain_smtp_reply_is_never_automatically_retried(self):
        self.service._process_email(self.command_email(prompt="fixture"))
        with self.harness.store.lock, self.harness.store.db:
            self.harness.store.db.execute("UPDATE tasks SET status='completed',result='done' WHERE id='task_1'")
        attempts = []
        class BrokenSMTP(FakeSMTP):
            def send_message(self, message):
                attempts.append(message)
                raise OSError("synthetic connection loss")
        with patch("email_control.smtplib.SMTP_SSL", BrokenSMTP):
            self.service._deliver_finished()
            self.service._deliver_finished()
        self.assertEqual(len(attempts), 1)
        self.assertEqual(self.commands()[0]["status"], "reply_uncertain")

    def test_outgoing_reports_obey_utf8_protocol_byte_limit(self):
        report = self.service._bounded_report({"action": "result", "status": "completed", "result": "任务已完成。" * 3000})
        self.assertLessEqual(len(json.dumps(report, ensure_ascii=False, separators=(",", ":")).encode("utf-8")), 8000)
        self.assertTrue(report["truncated"])

    def test_completed_submit_sends_one_redacted_signed_result(self):
        self.service._process_email(self.command_email(prompt="fixture work"))
        with self.harness.store.lock, self.harness.store.db:
            self.harness.store.db.execute("UPDATE tasks SET status='completed',result=?,updated_at=? WHERE id='task_1'",
                                          ("Done. fixture-mail-password " + self.secret, dt.datetime.now(dt.timezone.utc).isoformat()))
        with patch("email_control.smtplib.SMTP_SSL", FakeSMTP):
            self.service._deliver_finished()
            self.service._deliver_finished()
        self.assertEqual(len(FakeSMTP.instances), 1)
        smtp = FakeSMTP.instances[0]
        self.assertEqual(smtp.logged_in, ("fixture-user", "fixture-mail-password"))
        msg = smtp.sent[0]
        self.assertEqual(msg["To"], "owner@example.org")
        self.assertEqual(msg["From"], "device@example.net")
        self.assertEqual(msg["Content-Transfer-Encoding"], "base64")
        self.assertNotIn(" ", msg.get_content())
        signed = json.loads(msg.get_content())
        report = verify(signed, self.secret)
        self.assertEqual(report["action"], "result")
        self.assertEqual(report["report"]["status"], "completed")
        self.assertNotIn("fixture-mail-password", json.dumps(report))
        self.assertNotIn(self.secret, json.dumps(report))
        command = self.commands()[0]
        self.assertEqual(command["status"], "completed")
        self.assertTrue(command["replied_at"])

    def test_settings_password_empty_keeps_existing_private_password(self):
        self.service.save_settings({"enabled": False, "password": ""})
        public = self.service.public()["mail"]
        self.assertFalse(public["enabled"])
        self.assertTrue(public["passwordConfigured"])
        raw = self.service.config_path.read_text(encoding="utf-8")
        self.assertIn("fixture-mail-password", raw)
        if hasattr(self.service.config_path.stat(), "st_mode") and sys.platform != "win32":
            self.assertEqual(self.service.config_path.stat().st_mode & 0o777, 0o600)

    def test_http_settings_returns_full_state_for_existing_ui(self):
        self.service._process_email(self.command_email(prompt="fixture"))
        status, body = self.http("/api/remote/settings", {"enabled": False, "password": ""})
        self.assertEqual(status, 200)
        self.assertFalse(body["mail"]["enabled"])
        self.assertEqual(len(body["contacts"]), 1)
        self.assertEqual(len(body["commands"]), 1)
        self.assertNotIn(self.cfg["password"], json.dumps(body))
        self.assertNotIn(self.secret, json.dumps(body))

    def test_http_contact_enabled_is_explicit_and_idempotent(self):
        path = "/api/remote/contacts/" + self.contact["id"] + "/toggle"
        for _ in range(2):
            self.assertEqual(self.http(path, {"enabled": False})[0], 200)
            self.assertFalse(self.service.public()["contacts"][0]["enabled"])
        for invalid in ({}, {"enabled": 1}, {"enabled": "false"}, {"enabled": True, "extra": 1}):
            self.assertEqual(self.http(path, invalid)[0], 400)
            self.assertFalse(self.service.public()["contacts"][0]["enabled"])
        self.assertEqual(self.http(path, {"enabled": True})[0], 200)
        self.assertTrue(self.service.public()["contacts"][0]["enabled"])

    def test_delete_retains_fk_audit_hides_contact_and_repair_has_new_ownership(self):
        self.harness.store.db.execute("PRAGMA foreign_keys=ON")
        self.service._process_email(self.command_email(prompt="fixture"))
        path = "/api/remote/contacts/" + self.contact["id"] + "/delete"
        self.assertEqual(self.http(path, {})[0], 200)
        self.assertEqual(self.http(path, {})[0], 200)
        public = self.service.public()
        self.assertEqual(public["contacts"], [])
        self.assertEqual(len(public["commands"]), 1)
        self.assertEqual(public["commands"][0]["contactEmail"], "owner@example.org")
        self.assertEqual(list(self.harness.store.db.execute("PRAGMA foreign_key_check")), [])
        with self.assertRaises(KeyError):
            self.service.contact_action(self.contact["id"], "toggle", enabled=True)
        self.service._process_email(self.command_email(prompt="revoked fixture"))
        self.assertEqual(len(self.harness.start_calls), 1)
        repaired = self.service.create_contact({"email": "owner@example.org"})
        self.assertNotEqual(repaired["contact"]["id"], self.contact["id"])
        self.assertNotEqual(repaired["pairingSecret"], self.secret)
        self.service._process_email(self.command_email(prompt="old secret fixture"))
        self.assertEqual(len(self.harness.start_calls), 1)
        with patch("email_control.smtplib.SMTP_SSL", FakeSMTP):
            self.service._process_email(self.command_email("stop", secret=repaired["pairingSecret"], taskId="task_1"))
        report = verify(json.loads(FakeSMTP.instances[-1].sent[0].get_content()), repaired["pairingSecret"])["report"]
        self.assertEqual(report["status"], "rejected")
        self.assertEqual(self.harness.stop_calls, [])

    def test_legacy_contact_schema_is_migrated(self):
        harness = FakeHarness(Path(self.tmp.name) / "legacy")
        self.addCleanup(harness.store.db.close)
        with harness.store.lock, harness.store.db:
            harness.store.db.execute("CREATE TABLE mail_contacts (id TEXT PRIMARY KEY,email TEXT NOT NULL UNIQUE,secret TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL)")
            harness.store.db.execute("INSERT INTO mail_contacts VALUES('legacy','legacy@example.org','synthetic legacy secret',1,'2026-01-01T00:00:00Z')")
        service = MailControlService(harness)
        self.assertEqual(service.public()["contacts"][0]["id"], "legacy")
        service.contact_action("legacy", "delete")
        self.assertEqual(service.public()["contacts"], [])

    @unittest.skipIf(sys.platform == "win32", "POSIX file modes required")
    def test_existing_database_wal_and_shm_are_private(self):
        harness = FakeHarness(Path(self.tmp.name) / "permissions")
        self.addCleanup(harness.store.db.close)
        database = harness.state / "harness.sqlite3"
        paths = [Path(str(database) + suffix) for suffix in ("", "-wal", "-shm")]
        self.assertTrue(all(path.exists() for path in paths))
        for path in paths: path.chmod(0o644)
        MailControlService(harness)
        for path in paths: self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_imap_id_extension_registered_and_select_checked(self):
        calls = []
        class FakeIMAP:
            capabilities = (b"ID",)
            def __init__(self, *_args, **_kwargs): pass
            def login(self, *_args): pass
            def _simple_command(self, command, data):
                import imaplib
                self.assert_id = command in imaplib.Commands
                if not self.assert_id: raise AssertionError("ID extension is unregistered")
                calls.append((command, data))
                return "OK", []
            def select(self, _mailbox): return "OK", []
            def logout(self): pass
        with patch.dict("email_control.imaplib.Commands", {}, clear=True), patch("email_control.imaplib.IMAP4_SSL", FakeIMAP):
            self.service._connections(self.cfg)
        self.assertEqual(calls, [("ID", '("name" "DAXQO" "version" "0.4.0")')])
        class DeniedIMAP(FakeIMAP):
            capabilities = ()
            def select(self, _mailbox): return "NO", []
        with patch("email_control.imaplib.IMAP4_SSL", DeniedIMAP):
            with self.assertRaisesRegex(Exception, "Mailbox unavailable"):
                self.service._connections(self.cfg)

    def test_poll_accepts_standard_imap_size_bytes(self):
        raw = self.command_email(prompt="safe mailbox fixture", nonce="b" * 32)
        class FakeIMAP:
            seen = True  # Another mailbox client already marked this signed command read.
            criteria = []
            def search(self, _charset, *criteria):
                self.criteria.append(criteria)
                # Simulate a server whose SUBJECT search index is broken while ALL
                # still exposes the mailbox message with a matching fetched header.
                return ("OK", [b"1"] if criteria == ("ALL",) else [b""])
            def fetch(self, message_id, fields):
                if fields == "(RFC822.SIZE)": return "OK", [b"1 (RFC822.SIZE " + str(len(raw)).encode() + b")"]
                if fields == "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM)])":
                    head = raw.split(b"\n\n", 1)[0] + b"\n\n"
                    return "OK", [(b"1 (BODY[HEADER.FIELDS (SUBJECT FROM)] {" + str(len(head)).encode() + b"}", head), b")"]
                if fields.startswith("(BODY.PEEK[]"):
                    return "OK", [(b"1 (BODY[] {" + str(len(raw)).encode() + b"}", raw), b")"]
                raise AssertionError(f"unexpected FETCH: {fields}")
            def logout(self): pass
        imap = FakeIMAP()
        with patch.object(self.service, "_connections", return_value=imap):
            self.service.poll_once()
            self.service.poll_once()
        self.assertEqual(len(self.harness.start_calls), 1)
        self.assertEqual(imap.criteria, [("ALL",), ("ALL",)])

    def test_poll_uses_the_latest_hundred_matching_messages(self):
        raw = self.command_email(prompt="tail fixture", nonce="c" * 32)
        class FakeIMAP:
            fetched_ids = []
            def search(self, *_args): return "OK", [b" ".join(str(i).encode() for i in range(1, 106))]
            def fetch(self, message_id, fields):
                self.fetched_ids.append(int(message_id))
                if fields == "(RFC822.SIZE)": return "OK", [b"1 (RFC822.SIZE " + str(len(raw)).encode() + b")"]
                if fields == "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM)])":
                    head = raw.split(b"\n\n", 1)[0] + b"\n\n"
                    return "OK", [(b"1 (BODY[HEADER.FIELDS (SUBJECT FROM)] {" + str(len(head)).encode() + b"}", head), b")"]
                if fields.startswith("(BODY.PEEK[]"):
                    return "OK", [(b"1 (BODY[] {" + str(len(raw)).encode() + b"}", raw), b")"]
                raise AssertionError(f"unexpected FETCH: {fields}")
            def logout(self): pass
        imap = FakeIMAP()
        with patch.object(self.service, "_connections", return_value=imap):
            self.service.poll_once()
        self.assertEqual(sorted(set(imap.fetched_ids)), list(range(6, 106)))
        self.assertEqual(len(self.harness.start_calls), 1)

    def test_redaction_happens_before_output_truncation(self):
        for credential in (self.cfg["password"], self.secret):
            with self.subTest(kind="synthetic credential"):
                self.assertEqual(self.service._redact_result("x" * 10 + credential + "tail", 15), "x" * 10 + "[reda")

    def test_contact_revoked_during_smtp_login_prevents_send(self):
        self.service._process_email(self.command_email(prompt="fixture"))
        with self.harness.store.lock, self.harness.store.db:
            self.harness.store.db.execute("UPDATE tasks SET status='completed',result='done' WHERE id='task_1'")
        service, ident = self.service, self.contact["id"]
        class RevokingSMTP(FakeSMTP):
            def login(self, user, password):
                super().login(user, password)
                service.contact_action(ident, "toggle", enabled=False)
        with patch("email_control.smtplib.SMTP_SSL", RevokingSMTP):
            self.service._deliver_finished()
        self.assertEqual(FakeSMTP.instances[-1].sent, [])
        self.assertEqual(self.commands()[0]["status"], "interrupted")

    def test_finished_task_after_contact_deletion_converges_without_email(self):
        self.service._process_email(self.command_email(prompt="fixture task"))
        self.service.contact_action(self.contact["id"], "delete")
        with self.harness.store.lock, self.harness.store.db:
            self.harness.store.db.execute("UPDATE tasks SET status='completed',result='7+8=15' WHERE id='task_1'")
        with patch("email_control.smtplib.SMTP_SSL", FakeSMTP):
            self.service._deliver_finished()
        command = self.commands()[0]
        self.assertEqual(command["status"], "interrupted")
        self.assertEqual(self.harness.store.row("task_1")["status"], "completed")
        self.assertEqual(self.harness.stop_calls, [])
        self.assertEqual(FakeSMTP.instances, [])

    def test_settings_changed_during_smtp_login_prevents_old_config_send(self):
        self.service._process_email(self.command_email(prompt="fixture"))
        with self.harness.store.lock, self.harness.store.db:
            self.harness.store.db.execute("UPDATE tasks SET status='completed',result='done' WHERE id='task_1'")
        service = self.service
        class ChangingSMTP(FakeSMTP):
            def login(self, user, password):
                super().login(user, password)
                service.save_settings({"password": "new-synthetic-password"})
        with patch("email_control.smtplib.SMTP_SSL", ChangingSMTP):
            self.service._deliver_finished()
        self.assertEqual(FakeSMTP.instances[-1].sent, [])
        self.assertEqual(self.commands()[0]["status"], "interrupted")

    def test_connection_test_uses_imap_id_and_never_sends_mail(self):
        FakeIMAP.instances.clear()
        with patch("email_control.imaplib.IMAP4_SSL", FakeIMAP), patch("email_control.smtplib.SMTP_SSL", FakeSMTP):
            result = self.service.test_connection()
        self.assertTrue(result["ok"])
        self.assertEqual(FakeIMAP.instances[0].commands[0][0], "ID")
        self.assertEqual(FakeIMAP.instances[0].mailbox, "INBOX")
        self.assertEqual(FakeSMTP.instances[0].sent, [])


if __name__ == "__main__":
    unittest.main()
