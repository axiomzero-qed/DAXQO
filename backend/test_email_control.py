import datetime as dt
import base64
import shutil
import subprocess
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import format_datetime
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
MAIL_LIBS = ROOT / ".tools" / "mail-libs"
if MAIL_LIBS.is_dir(): sys.path.insert(0, str(MAIL_LIBS))
sys.path.insert(0, str(ROOT / "backend"))
import email_control
from email_control import MailControlService
from email_control import _verify_plain_email, _stamp
from remote_protocol import sign, verify
from server import Store

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
except ImportError:
    serialization = rsa = None

OPENSSL = shutil.which("openssl")


class FakeHarness:
    def __init__(self, root):
        self.state = root / "state"
        self.provider_dir = self.state / "private"
        self.provider_dir.mkdir(parents=True)
        self.store = Store(self.state / "harness.sqlite3")
        self.start_calls = []
        self.stop_calls = []
        self.permission_approvals = []
        self.permission_decisions = []

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

    def android_permission_approvals(self, task_id=None):
        return [dict(item) for item in self.permission_approvals if task_id is None or item.get("taskId") == task_id]

    def decide_android_permission(self, approval_id, decision, *, task_id=None):
        item = next((item for item in self.permission_approvals if item["id"] == approval_id and item["taskId"] == task_id), None)
        if not item or item["status"] != "pending": raise ValueError("Approval is no longer pending")
        self.permission_decisions.append((approval_id, decision, task_id))
        item["status"] = "approved" if decision == "accept" else "declined"
        return {"id": approval_id, "taskId": task_id, "status": item["status"]}


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


class FakeMailboxIMAP:
    def __init__(self, messages):
        self.messages = messages
        self.full_fetches = 0

    def search(self, *_args):
        return "OK", [b" ".join(str(i).encode() for i in self.messages)]

    def fetch(self, message_id, fields):
        raw = self.messages[int(message_id)]
        parsed = BytesParser(policy=policy.default).parsebytes(raw)
        if fields == "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM MESSAGE-ID DATE)])":
            header = b"".join((name + ": " + str(parsed.get(name, "")) + "\r\n").encode("utf-8")
                               for name in ("Subject", "From", "Message-ID", "Date") if parsed.get(name) is not None) + b"\r\n"
            return "OK", [(b"1 (BODY[HEADER.FIELDS] {" + str(len(header)).encode() + b"}", header), b")"]
        if fields == "(RFC822.SIZE)":
            return "OK", [b"1 (RFC822.SIZE " + str(len(raw)).encode() + b")"]
        if fields.startswith("(BODY.PEEK[]"):
            self.full_fetches += 1
            return "OK", [(b"1 (BODY[] {" + str(len(raw)).encode() + b"}", raw), b")"]
        raise AssertionError(f"unexpected FETCH: {fields}")

    def logout(self): pass


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
                      nonce=None, sent_at=None, **fields):
        stamp = sent_at or dt.datetime.now(dt.timezone.utc)
        payload = {"v": 1, "action": action, "contact": contact, "nonce": nonce or uuid.uuid4().hex,
                   "sentAt": stamp.isoformat(), **fields}
        raw = json.dumps(sign(payload, secret or self.secret), ensure_ascii=False, separators=(",", ":"))
        msg = EmailMessage()
        msg["From"] = sender
        msg["To"] = "device@example.net"
        msg["Subject"] = "DAXQO/1 " + payload["nonce"]
        msg["Message-ID"] = f"<{uuid.uuid4()}@example.test>"
        msg["Date"] = dt.datetime.now(dt.timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
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

    def test_stop_covers_waiting_approval_and_blocked_system(self):
        task_ids = []
        with patch("email_control.smtplib.SMTP_SSL", FakeSMTP):
            for index, status in enumerate(("running", "waiting_approval", "blocked_system")):
                task_id = self.harness.start_task({"prompt": "stop fixture " + str(index)})["id"]
                task_ids.append(task_id)
                with self.harness.store.lock, self.harness.store.db:
                    self.harness.store.db.execute("UPDATE tasks SET status=? WHERE id=?", (status, task_id))
                    self.harness.store.db.execute(
                        "INSERT INTO mail_commands(id,contact_id,nonce,action,task_id,status,received_at) VALUES(?,?,?,?,?,?,?)",
                        (str(uuid.uuid4()), self.contact["id"], "owned_stop_nonce_" + str(index).zfill(16),
                         "submit", task_id, "running", "2026-01-01T00:00:00Z"))
                raw = self.command_email("stop", taskId=task_id)
                self.service._process_email(raw)
        self.assertEqual(self.harness.stop_calls, task_ids)

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

    def _start_owned_task(self, *, status="running"):
        self.service._process_email(self.command_email(prompt="my fixture task", nonce=uuid.uuid4().hex))
        with self.harness.store.lock, self.harness.store.db:
            self.harness.store.db.execute("UPDATE tasks SET status=? WHERE id='task_1'", (status,))
        return "task_1"

    def _approval(self, task_id="task_1", *, status="pending", expires_ms=None, approval_id=None):
        item = {"id": approval_id or str(uuid.uuid4()), "taskId": task_id, "action": "tap Allow",
                "packageName": "com.example.fixture", "summary": "Approve the requested action",
                "status": status, "expiresAt": expires_ms or int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000) + 120_000}
        self.harness.permission_approvals.append(item)
        return item

    def test_signed_approval_is_task_owned_replay_safe_and_resumes_once(self):
        self._start_owned_task(status="waiting_approval")
        approval = self._approval()
        raw = self.command_email("approve", taskId="task_1", approvalId=approval["id"], nonce="approval_nonce_000000000001")
        with patch("email_control.smtplib.SMTP_SSL", FakeSMTP):
            self.service._process_email(raw)
            self.service._process_email(raw)
        self.assertEqual(self.harness.permission_decisions, [(approval["id"], "accept", "task_1")])
        report = verify(json.loads(FakeSMTP.instances[-1].sent[0].get_content()), self.secret)["report"]
        self.assertEqual((report["action"], report["approvalId"], report["status"]), ("approve", approval["id"], "approved"))

    def test_signed_approval_rejects_cross_contact_and_expired_ids(self):
        self._start_owned_task(status="waiting_approval")
        approval = self._approval()
        other = self.service.create_contact({"email": "other@example.org"})
        raw = self.command_email("approve", sender="other@example.org", contact="other@example.org",
            secret=other["pairingSecret"], taskId="task_1", approvalId=approval["id"], nonce="foreign_approval_nonce_000001")
        with patch("email_control.smtplib.SMTP_SSL", FakeSMTP):
            self.service._process_email(raw)
        self.assertEqual(self.harness.permission_decisions, [])
        report = verify(json.loads(FakeSMTP.instances[-1].sent[0].get_content()), other["pairingSecret"])["report"]
        self.assertEqual(report["status"], "rejected")
        self.assertNotIn("task_1", json.dumps(report))

        expired = self._approval(expires_ms=int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000) - 1)
        with patch("email_control.smtplib.SMTP_SSL", FakeSMTP):
            self.service._process_email(self.command_email("decline", taskId="task_1", approvalId=expired["id"], nonce="expired_approval_nonce_00001"))
        self.assertEqual(self.harness.permission_decisions, [])
        report = verify(json.loads(FakeSMTP.instances[-1].sent[0].get_content()), self.secret)["report"]
        self.assertEqual(report["status"], "rejected")

    def test_waiting_approval_stays_running_and_sends_one_notice_not_completion(self):
        task_id = self._start_owned_task(status="waiting_approval")
        approval = self._approval(task_id)
        with patch("email_control.smtplib.SMTP_SSL", FakeSMTP):
            self.service._deliver_finished()
            self.service._deliver_finished()
        original = next(row for row in self.commands() if row["action"] == "submit")
        self.assertEqual(original["status"], "running")
        with self.harness.store.lock:
            self.assertEqual(self.harness.store.db.execute("SELECT COUNT(*) FROM mail_approval_notices WHERE approval_id=?", (approval["id"],)).fetchone()[0], 1)
        self.assertEqual(sum(len(smtp.sent) for smtp in FakeSMTP.instances), 1)
        notice = verify(json.loads(FakeSMTP.instances[0].sent[0].get_content()), self.secret)["report"]
        self.assertEqual(notice["status"], "waiting_approval")
        self.assertEqual(notice["approval"]["packageName"], "com.example.fixture")
        self.assertIn("确认 " + approval["id"], json.dumps(notice, ensure_ascii=False))

    def test_status_reports_waiting_approval_as_nonterminal(self):
        self._start_owned_task(status="waiting_approval")
        with patch("email_control.smtplib.SMTP_SSL", FakeSMTP):
            self.service._process_email(self.command_email("status", taskId="task_1"))
        report = verify(json.loads(FakeSMTP.instances[-1].sent[0].get_content()), self.secret)["report"]
        self.assertEqual(report["status"], "waiting_approval")
        self.assertNotIn("result", report)

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
                if fields == "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM MESSAGE-ID DATE)])":
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

    def test_oversized_message_is_skipped_before_header_or_body_fetch(self):
        class FakeIMAP:
            later_fetches = []
            def search(self, *_args): return "OK", [b"1"]
            def fetch(self, _message_id, fields):
                if fields == "(RFC822.SIZE)":
                    return "OK", [b"1 (RFC822.SIZE " + str(96 * 1024 + 1).encode() + b")"]
                self.later_fetches.append(fields)
                raise AssertionError(f"oversized message must not be read: {fields}")
            def logout(self): pass
        imap = FakeIMAP()
        with patch.object(self.service, "_connections", return_value=imap):
            self.service.poll_once()
        self.assertEqual(imap.later_fetches, [])
        self.assertEqual(self.harness.start_calls, [])
        self.assertEqual(self.service.public()["receipts"], [])

    def test_poll_uses_the_latest_hundred_matching_messages(self):
        raw = self.command_email(prompt="tail fixture", nonce="c" * 32)
        class FakeIMAP:
            fetched_ids = []
            def search(self, *_args): return "OK", [b" ".join(str(i).encode() for i in range(1, 106))]
            def fetch(self, message_id, fields):
                self.fetched_ids.append(int(message_id))
                if fields == "(RFC822.SIZE)": return "OK", [b"1 (RFC822.SIZE " + str(len(raw)).encode() + b")"]
                if fields == "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM MESSAGE-ID DATE)])":
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

    def test_poll_records_trusted_ordinary_mail_once_without_reading_body(self):
        mail = EmailMessage()
        mail["From"] = "owner@example.org"
        mail["To"] = "device@example.net"
        mail["Subject"] = "写一篇文章 " + "恶意标题 " * 100
        mail["Message-ID"] = "<ordinary-1@example.test>"
        mail["Date"] = "Thu, 22 Oct 2026 00:00:00 +0000"
        mail.set_content("This body must not be fetched.")
        imap = FakeMailboxIMAP({1: mail.as_bytes()})
        with patch.object(self.service, "_connections", return_value=imap):
            self.service.poll_once()
            self.service.poll_once()
        self.assertEqual(imap.full_fetches, 0)
        self.assertEqual(self.harness.start_calls, [])
        receipts = self.service.public()["receipts"]
        self.assertEqual(len(receipts), 1)
        receipt = receipts[0]
        self.assertEqual(set(receipt), {"id", "contactEmail", "subject", "status", "reason", "receivedAt"})
        self.assertEqual(receipt["contactEmail"], "owner@example.org")
        self.assertEqual(receipt["status"], "not_started")
        self.assertIn("普通邮件未包含签名命令", receipt["reason"])
        self.assertLessEqual(len(receipt["subject"]), 200)

    def test_poll_does_not_record_unknown_or_disabled_sender(self):
        messages = {}
        for ident, sender in ((1, "stranger@example.org"), (2, "owner@example.org")):
            mail = EmailMessage()
            mail["From"] = sender
            mail["Subject"] = "普通邮件"
            mail["Message-ID"] = f"<untrusted-{ident}@example.test>"
            mail.set_content("must not be fetched")
            messages[ident] = mail.as_bytes()
        self.service.contact_action(self.contact["id"], "toggle", enabled=False)
        imap = FakeMailboxIMAP(messages)
        with patch.object(self.service, "_connections", return_value=imap):
            self.service.poll_once()
        self.assertEqual(self.service.public()["receipts"], [])
        self.assertEqual(imap.full_fetches, 0)
        self.assertEqual(self.harness.start_calls, [])

    def test_plain_email_disabled_records_header_only_and_never_reads_body(self):
        mail = EmailMessage()
        mail["From"] = "owner@example.org"
        mail["To"] = "device@example.net"
        mail["Subject"] = "Ordinary email"
        mail["Message-ID"] = "<disabled@example.test>"
        mail["Date"] = format_datetime(dt.datetime.now(dt.timezone.utc))
        mail.set_content("do not fetch this body")
        imap = FakeMailboxIMAP({1: mail.as_bytes()})
        with patch.object(self.service, "_connections", return_value=imap):
            self.service.poll_once()
        self.assertEqual(imap.full_fetches, 0)
        self.assertEqual(self.harness.start_calls, [])
        self.assertEqual(self.service.public()["receipts"][0]["status"], "not_started")

    def test_five_invalid_plain_candidates_do_not_starve_sixth_and_hmac_still_runs(self):
        gmail = self.service.create_contact({"email": "owner@gmail.com"})
        self.service.save_settings({"plainEmailEnabled": True})
        since = dt.datetime.fromisoformat(self.service._config["plainEmailSince"].replace("Z", "+00:00"))
        invalid, messages = {}, {}
        for ident in range(1, 6):
            mail = EmailMessage()
            mail["From"], mail["To"] = "owner@gmail.com", "device@example.net"
            mail["Subject"], mail["Date"] = f"Invalid candidate {ident}", format_datetime(since + dt.timedelta(seconds=1))
            mail["Message-ID"] = f"<invalid-{ident}@gmail.com>"
            mail.set_content("untrusted candidate")
            invalid[ident] = messages[ident] = mail.as_bytes(policy=policy.SMTP)
        sixth = EmailMessage()
        sixth["From"], sixth["To"] = "owner@gmail.com", "device@example.net"
        sixth["Subject"], sixth["Date"] = "Sixth task", format_datetime(since + dt.timedelta(seconds=1))
        sixth["Message-ID"] = "<sixth-valid@gmail.com>"
        sixth.set_content("sixth valid task")
        messages[6] = sixth.as_bytes(policy=policy.SMTP)
        command = {"v": 1, "action": "submit", "contact": "owner@gmail.com", "nonce": uuid.uuid4().hex,
                   "sentAt": dt.datetime.now(dt.timezone.utc).isoformat(), "prompt": "signed protocol task"}
        signed = EmailMessage()
        signed["From"], signed["To"] = "owner@gmail.com", "device@example.net"
        signed["Subject"], signed["Date"] = "DAXQO/1 " + command["nonce"], format_datetime(dt.datetime.now(dt.timezone.utc))
        signed["Message-ID"] = "<signed-hmac@gmail.com>"
        signed.set_content(json.dumps(sign(command, gmail["pairingSecret"]), separators=(",", ":")))
        messages[7] = signed.as_bytes(policy=policy.SMTP)

        def fake_verify(raw, sender, receiver, start):
            if raw in invalid.values(): raise ValueError("synthetic invalid DKIM")
            message = BytesParser(policy=policy.default).parsebytes(raw)
            return {"action": "submit", "prompt": "sixth valid task", "title": "Sixth task",
                    "nonce": "plain:" + str(message["Message-ID"]), "messageId": str(message["Message-ID"]),
                    "subject": str(message["Subject"])}
        imap = FakeMailboxIMAP(messages)
        with patch.object(self.service, "_connections", return_value=imap), patch("email_control.dkim", object()), \
             patch("email_control._verify_plain_email", side_effect=fake_verify), patch("email_control.smtplib.SMTP_SSL", FakeSMTP):
            self.service.poll_once()
            self.service.poll_once()
        prompts = [call["prompt"] for call in self.harness.start_calls]
        self.assertEqual(len(prompts), 2)
        self.assertTrue(any("sixth valid task" in prompt for prompt in prompts))
        self.assertTrue(any("signed protocol task" in prompt for prompt in prompts))

    def test_malformed_dkim_message_does_not_block_following_hmac_message(self):
        gmail = self.service.create_contact({"email": "owner@gmail.com"})
        self.service.save_settings({"plainEmailEnabled": True})
        since = dt.datetime.fromisoformat(self.service._config["plainEmailSince"].replace("Z", "+00:00"))
        malformed = EmailMessage()
        malformed["From"], malformed["To"] = "owner@gmail.com", "device@example.net"
        malformed["Subject"], malformed["Date"] = "Malformed DKIM", format_datetime(since + dt.timedelta(seconds=1))
        malformed["Message-ID"] = "<malformed-dkim@gmail.com>"
        malformed.set_content("plain candidate")
        command = {"v": 1, "action": "submit", "contact": "owner@gmail.com", "nonce": uuid.uuid4().hex,
                   "sentAt": dt.datetime.now(dt.timezone.utc).isoformat(), "prompt": "later signed HMAC task"}
        hmac_mail = EmailMessage()
        hmac_mail["From"], hmac_mail["To"] = "owner@gmail.com", "device@example.net"
        hmac_mail["Subject"], hmac_mail["Date"] = "DAXQO/1 " + command["nonce"], format_datetime(dt.datetime.now(dt.timezone.utc))
        hmac_mail["Message-ID"] = "<following-hmac@gmail.com>"
        hmac_mail.set_content(json.dumps(sign(command, gmail["pairingSecret"]), separators=(",", ":")))
        class BrokenDKIM:
            MessageFormatError = type("MessageFormatError", (Exception,), {})
            def __init__(self, *_args, **_kwargs): raise self.MessageFormatError("malformed fixture")
        imap = FakeMailboxIMAP({1: malformed.as_bytes(policy=policy.SMTP), 2: hmac_mail.as_bytes(policy=policy.SMTP)})
        with patch.object(self.service, "_connections", return_value=imap), patch("email_control.dkim", BrokenDKIM), \
             patch("email_control.smtplib.SMTP_SSL", FakeSMTP):
            self.service.poll_once()
        self.assertEqual(len(self.harness.start_calls), 1)
        self.assertIn("later signed HMAC task", self.harness.start_calls[0]["prompt"])
        self.assertIn("邮件签名格式无效", self.service.public()["receipts"][0]["reason"])

    def test_same_second_disable_enable_invalidates_inflight_verification(self):
        gmail = self.service.create_contact({"email": "owner@gmail.com"})
        fixed_stamp = _stamp()
        with patch("email_control._stamp", return_value=fixed_stamp):
            self.service.save_settings({"plainEmailEnabled": True})
            before = dict(self.service._config)
            generation = self.service._auth_generation
            since = dt.datetime.fromisoformat(fixed_stamp.replace("Z", "+00:00"))
            mail = EmailMessage()
            mail["From"], mail["To"] = "owner@gmail.com", "device@example.net"
            mail["Subject"], mail["Date"] = "Toggle race", format_datetime(since + dt.timedelta(seconds=1))
            mail["Message-ID"] = "<toggle-race@gmail.com>"
            mail.set_content("must not launch")
            raw = mail.as_bytes(policy=policy.SMTP)
            def toggle_during_verify(*_args):
                self.service.save_settings({"plainEmailEnabled": False})
                self.service.save_settings({"plainEmailEnabled": True})
                return {"action": "submit", "prompt": "must not launch", "title": "Toggle race",
                        "nonce": "plain:toggle-race", "messageId": "<toggle-race@gmail.com>", "subject": "Toggle race"}
            imap = FakeMailboxIMAP({1: raw})
            with patch.object(self.service, "_connections", return_value=imap), patch("email_control.dkim", object()), \
                 patch("email_control._verify_plain_email", side_effect=toggle_during_verify):
                self.service.poll_once()
            self.assertEqual(before, self.service._config)
            self.assertGreater(self.service._auth_generation, generation)
        self.assertEqual(self.harness.start_calls, [])
        self.assertEqual(self.commands(), [])

    def test_expired_rescan_of_known_signed_nonce_does_not_create_failure_receipt(self):
        nonce = "d" * 32
        self.service._process_email(self.command_email(prompt="first run", nonce=nonce))
        old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=601)
        stale = self.command_email(prompt="historical", nonce=nonce, sent_at=old)
        imap = FakeMailboxIMAP({1: stale})
        with patch.object(self.service, "_connections", return_value=imap):
            self.service.poll_once()
        self.assertEqual(len(self.harness.start_calls), 1)
        self.assertEqual(self.service.public()["receipts"], [])

    def test_invalid_signature_gets_generic_local_receipt_without_execution(self):
        raw = self.command_email(prompt="must not execute", secret="wrong synthetic signature key")
        imap = FakeMailboxIMAP({1: raw})
        with patch.object(self.service, "_connections", return_value=imap):
            self.service.poll_once()
        self.assertEqual(self.harness.start_calls, [])
        receipts = self.service.public()["receipts"]
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["status"], "not_started")
        self.assertIn("签名命令无效或已处理", receipts[0]["reason"])
        self.assertNotIn("signature", json.dumps(receipts).lower())

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


@unittest.skipIf(email_control.dkim is None or (rsa is None and OPENSSL is None),
                 "DKIM unavailable and neither cryptography nor openssl can create a test key")
class PlainEmailAuthenticationTests(unittest.TestCase):
    PROVIDER_DOMAINS = ("qq.com", "foxmail.com", "163.com", "126.com", "yeah.net")

    @classmethod
    def setUpClass(cls):
        cls._keypairs = {}
        cls.now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)

    @classmethod
    def _keypair(cls, bits):
        if bits not in cls._keypairs:
            if rsa is not None:
                private_key = rsa.generate_private_key(public_exponent=65537, key_size=bits).private_bytes(
                    serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption())
                key_object = serialization.load_pem_private_key(private_key, password=None)
                public_key = key_object.public_key().public_bytes(
                    serialization.Encoding.DER, serialization.PublicFormat.PKCS1)
            else:
                private_key = subprocess.run([OPENSSL, "genrsa", str(bits)], capture_output=True, check=True).stdout
                public_key = subprocess.run([OPENSSL, "rsa", "-pubout", "-outform", "DER"],
                                            input=private_key, capture_output=True, check=True).stdout
            cls._keypairs[bits] = (private_key, public_key)
        return cls._keypairs[bits]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.harness = FakeHarness(Path(self.tmp.name))
        self.addCleanup(self.harness.store.db.close)
        self.service = MailControlService(self.harness)
        self.service.save_settings({"enabled": True, "accountAddress": "device@example.net", "username": "fixture-user",
                                    "imapHost": "imap.example.net", "imapPort": 993, "smtpHost": "smtp.example.net",
                                    "smtpPort": 465, "password": "fixture-mail-password"})
        created = self.service.create_contact({"email": "owner@gmail.com"})
        self.contact = created["contact"]
        self.contact_secret = created["pairingSecret"]
        self._fixture_public_keys = {}
        FakeSMTP.instances.clear()

    def dnsfunc(self, name, timeout=5):
        query = name.lower().rstrip(b".")
        try:
            selector, domain = query.split(b"._domainkey.", 1)
        except ValueError:
            self.fail("unexpected DKIM DNS query shape")
        public_key = self._fixture_public_keys.get((selector.decode("ascii"), domain.decode("ascii")))
        if public_key is None:
            self.fail(f"no fixture key registered for {selector!r}._domainkey.{domain!r}")
        return b"v=DKIM1; k=rsa; p=" + base64.b64encode(public_key)

    def commands(self):
        with self.harness.store.lock:
            return [dict(row) for row in self.harness.store.db.execute("SELECT * FROM mail_commands ORDER BY received_at")]

    def _approval(self, task_id="task_1"):
        item = {"id": str(uuid.uuid4()), "taskId": task_id, "action": "tap Allow",
                "packageName": "com.example.fixture", "summary": "Approve fixture action",
                "status": "pending", "expiresAt": int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000) + 120_000}
        self.harness.permission_approvals.append(item)
        return item

    def signed_email(self, *, body="please process this fixture", subject="Fixture request", sender="owner@gmail.com",
                     receiver="device@example.net", date=None, include_headers=None, length=False, body_type="plain",
                     attachment=False, signing_domain=None, key_bits=2048, private_key=None, timestamp=None,
                     signed_headers=None, selector="fixture", signature_algorithm=b"rsa-sha256",
                     transfer_encoding="quoted-printable", html_body="<p>HTML fixture</p>"):
        if signed_headers is not None:
            if include_headers is not None:
                raise ValueError("Use either include_headers or signed_headers")
            include_headers = signed_headers
        if signing_domain is None:
            signing_domain = sender.rsplit("@", 1)[-1].lower()
        if private_key is None:
            private_key, public_key = self._keypair(key_bits)
        else:
            if isinstance(private_key, str):
                private_key = private_key.encode("ascii")
            if rsa is not None:
                key_object = serialization.load_pem_private_key(private_key, password=None)
                public_key = key_object.public_key().public_bytes(
                    serialization.Encoding.DER, serialization.PublicFormat.PKCS1)
            else:
                public_key = subprocess.run([OPENSSL, "rsa", "-pubout", "-outform", "DER"],
                                            input=private_key, capture_output=True, check=True).stdout
        self._fixture_public_keys[(selector, signing_domain.lower())] = public_key
        message = EmailMessage()
        message["From"] = sender
        message["To"] = receiver
        message["Subject"] = subject
        message["Date"] = format_datetime(date or self.now)
        message["Message-ID"] = f"<{uuid.uuid4()}@gmail.com>"
        if body_type == "plain":
            message.set_content(body, charset="utf-8", cte=transfer_encoding)
        elif body_type == "html":
            message.set_content(body, subtype="html", charset="utf-8", cte=transfer_encoding)
        elif body_type == "multipart":
            message.set_content(body, charset="utf-8", cte=transfer_encoding)
            message.add_alternative(html_body, subtype="html", charset="utf-8", cte=transfer_encoding)
        elif body_type == "empty-multipart":
            message.set_type("multipart/alternative")
        if attachment:
            message.add_attachment(b"fixture attachment", maintype="application", subtype="octet-stream", filename="fixture.bin")
        raw = message.as_bytes(policy=policy.SMTP)
        headers = include_headers or ([b"from", b"to", b"subject", b"date", b"message-id",
                                      b"content-type", b"mime-version", b"content-transfer-encoding"]
                                     if signing_domain.lower() == "gmail.com" else
                                     [b"from", b"to", b"subject", b"date"])
        signer = lambda: email_control.dkim.sign(raw, selector.encode("ascii"), signing_domain.encode("ascii"), private_key,
                                                  include_headers=headers, length=length,
                                                  signature_algorithm=signature_algorithm)
        if timestamp is None:
            signature = signer()
        else:
            signing_time = int(timestamp.timestamp()) if hasattr(timestamp, "timestamp") else int(timestamp)
            with patch("email_control.dkim.time.time", return_value=signing_time):
                signature = signer()
        return signature + raw

    @staticmethod
    def replace_outer_header(raw, name, value):
        header, separator, body = raw.partition(b"\r\n\r\n")
        if not separator:
            raise AssertionError("fixture message has no header/body separator")
        target = name.encode("ascii").lower() + b":"
        lines = header.split(b"\r\n")
        matches = [index for index, line in enumerate(lines) if line.lower().startswith(target)]
        if len(matches) > 1:
            raise AssertionError(f"expected at most one outer {name} header, got {len(matches)}")
        replacement = name.encode("ascii") + b": " + value.encode("ascii")
        if matches:
            lines[matches[0]] = replacement
        else:
            lines.append(replacement)
        return b"\r\n".join(lines) + separator + body

    def verify_fixture(self, raw, *, sender="owner@gmail.com", since=None, now=None):
        start = since or (self.now - dt.timedelta(days=2)).isoformat()
        return _verify_plain_email(raw, sender, "device@example.net", start,
                                   now=now or self.now, dnsfunc=self.dnsfunc)

    def enable_plain(self):
        self.service.save_settings({"plainEmailEnabled": True})

    def test_real_dkim_verification_returns_subject_and_body(self):
        raw = self.signed_email(body="Please review the local fixture.", subject="Check the report")
        result = self.verify_fixture(raw)
        self.assertEqual(result["action"], "submit")
        self.assertEqual(result["title"], "Check the report")
        self.assertIn("邮件主题：Check the report", result["prompt"])
        self.assertIn("Please review the local fixture.", result["prompt"])
        self.assertTrue(result["messageId"].startswith("<"))

    def test_gmail_fixture_keeps_2048_bit_rsa_sha256_and_legacy_signed_headers(self):
        raw = self.signed_email()
        parsed = BytesParser(policy=policy.default).parsebytes(raw)
        fields = email_control.dkim.parse_tag_value(str(parsed["DKIM-Signature"]).encode("ascii"))
        signed = {header.strip().lower() for header in fields[b"h"].split(b":")}
        self.assertEqual(fields[b"d"], b"gmail.com")
        self.assertEqual(fields[b"a"], b"rsa-sha256")
        self.assertNotIn(b"l", fields)
        self.assertEqual(signed, {b"from", b"to", b"subject", b"date", b"message-id",
                                  b"content-type", b"mime-version", b"content-transfer-encoding"})
        if rsa is not None:
            private, _ = self._keypair(2048)
            self.assertEqual(serialization.load_pem_private_key(private, password=None).key_size, 2048)
        with self.assertRaises(ValueError):
            self.verify_fixture(self.signed_email(key_bits=1024))

    def test_provider_fixtures_use_exact_domains_1024_bit_sha256_and_minimum_headers(self):
        for domain in self.PROVIDER_DOMAINS:
            with self.subTest(domain=domain):
                sender = "owner@" + domain
                raw = self.signed_email(sender=sender, signing_domain=domain, key_bits=1024)
                self.assertTrue(email_control.dkim.DKIM(raw, minkey=1024).verify(dnsfunc=self.dnsfunc))
                parsed = BytesParser(policy=policy.default).parsebytes(raw)
                fields = email_control.dkim.parse_tag_value(str(parsed["DKIM-Signature"]).encode("ascii"))
                self.assertEqual(fields[b"d"], domain.encode("ascii"))
                self.assertEqual(fields[b"a"], b"rsa-sha256")
                self.assertNotIn(b"l", fields)
                signed = {header.strip().lower() for header in fields[b"h"].split(b":")}
                self.assertTrue({b"from", b"to", b"subject", b"date"}.issubset(signed))

    def test_supported_provider_signatures_create_one_task_and_reply_to_registered_sender(self):
        self.enable_plain()
        start = dt.datetime.fromisoformat(self.service._config["plainEmailSince"].replace("Z", "+00:00"))
        verifier = lambda *args, **kwargs: _verify_plain_email(*args, dnsfunc=self.dnsfunc, **kwargs)
        with patch("email_control._verify_plain_email", side_effect=verifier), \
             patch("email_control.smtplib.SMTP_SSL", FakeSMTP):
            for index, domain in enumerate(self.PROVIDER_DOMAINS, 1):
                sender = "owner@" + domain
                contact = self.service.create_contact({"email": sender})["contact"]
                raw = self.signed_email(sender=sender, signing_domain=domain, key_bits=1024,
                    subject=f"Fixture {domain}", body="请检查这个普通任务。",
                    date=start + dt.timedelta(seconds=index))
                self.service._process_plain_email(raw, {**contact, "plain_since": None}, "provider-" + domain)
                self.service._process_plain_email(raw, {**contact, "plain_since": None}, "provider-duplicate-" + domain)

        self.assertEqual(len(self.harness.start_calls), len(self.PROVIDER_DOMAINS))
        self.assertEqual(len(self.commands()), len(self.PROVIDER_DOMAINS))
        for call, domain in zip(self.harness.start_calls, self.PROVIDER_DOMAINS):
            self.assertTrue(call["prompt"].endswith(f"邮件主题：Fixture {domain}\n\n请检查这个普通任务。"))
        self.assertEqual(len(FakeSMTP.instances), len(self.PROVIDER_DOMAINS))
        self.assertEqual([smtp.sent[0]["To"] for smtp in FakeSMTP.instances],
                         ["owner@" + domain for domain in self.PROVIDER_DOMAINS])
        self.assertEqual([row["ack_status"] for row in self.commands()], ["sent"] * len(self.PROVIDER_DOMAINS))

    def test_valid_provider_signatures_from_unknown_or_disabled_contacts_are_not_fetched(self):
        self.enable_plain()
        disabled = self.service.create_contact({"email": "owner@163.com"})["contact"]
        self.service.contact_action(disabled["id"], "toggle", enabled=False)
        unknown_sender = "stranger@qq.com"
        disabled_mail = self.signed_email(sender=disabled["email"], signing_domain="163.com", key_bits=1024)
        unknown_mail = self.signed_email(sender=unknown_sender, signing_domain="qq.com", key_bits=1024)
        imap = FakeMailboxIMAP({1: unknown_mail, 2: disabled_mail})
        with patch.object(self.service, "_connections", return_value=imap):
            self.service.poll_once()
        self.assertEqual(imap.full_fetches, 0)
        self.assertEqual(self.harness.start_calls, [])
        self.assertEqual(self.commands(), [])

    def test_domestic_provider_cutover_blocks_old_mail_and_survives_service_restart(self):
        # Model an installation whose global plain-mail feature predates the
        # provider rollout, with a QQ contact that already existed then.
        activated = self.now - dt.timedelta(days=2)
        cutover = self.now - dt.timedelta(minutes=30)
        with patch("email_control._stamp", return_value=activated.strftime("%Y-%m-%dT%H:%M:%SZ")):
            self.enable_plain()
        contact = self.service.create_contact({"email": "owner@qq.com"})["contact"]
        with self.harness.store.lock, self.harness.store.db:
            self.harness.store.db.execute("UPDATE mail_contacts SET created_at=? WHERE id=?",
                                          (activated.strftime("%Y-%m-%dT%H:%M:%SZ"), contact["id"]))
            self.harness.store.db.execute("UPDATE mail_features SET enabled_at=? WHERE name='qq-netease-plain-v1'",
                                          (cutover.strftime("%Y-%m-%dT%H:%M:%SZ"),))
        self.service._domestic_since = cutover.strftime("%Y-%m-%dT%H:%M:%SZ")
        legacy = self.signed_email(sender="owner@qq.com", signing_domain="qq.com", key_bits=1024,
            signed_headers=[b"from", b"to", b"subject", b"date"], date=cutover - dt.timedelta(minutes=5),
            subject="Before domestic rollout")
        post_cutover = self.signed_email(sender="owner@qq.com", signing_domain="qq.com", key_bits=1024,
            signed_headers=[b"from", b"to", b"subject", b"date"], date=cutover + dt.timedelta(seconds=1),
            subject="After domestic rollout")
        verifier = lambda *args, **kwargs: _verify_plain_email(*args, dnsfunc=self.dnsfunc, **kwargs)
        with patch("email_control._verify_plain_email", side_effect=verifier), \
             patch("email_control.smtplib.SMTP_SSL", FakeSMTP):
            self.assertFalse(self.service._process_plain_email(legacy, contact, "legacy-cutover-mail"))
            self.assertEqual(self.harness.start_calls, [])
            self.assertTrue(self.service._process_plain_email(post_cutover, contact, "new-cutover-mail"))
        self.assertEqual(len(self.harness.start_calls), 1)

        later = self.now + dt.timedelta(days=10)
        with patch("email_control._stamp", return_value=later.strftime("%Y-%m-%dT%H:%M:%SZ")):
            restarted = MailControlService(self.harness)
        self.assertEqual(restarted._domestic_since, cutover.strftime("%Y-%m-%dT%H:%M:%SZ"))
        with patch("email_control._verify_plain_email", side_effect=verifier), \
             patch("email_control.smtplib.SMTP_SSL", FakeSMTP):
            self.assertFalse(restarted._process_plain_email(post_cutover, contact, "replayed-after-restart"))
        self.assertEqual(len(self.harness.start_calls), 1)
        self.assertEqual(len(self.commands()), 1)

    def test_provider_signature_requires_exact_domain_and_signed_identity_fields(self):
        sender = "owner@qq.com"
        signed_required = [b"from", b"to", b"subject", b"date"]
        valid = self.signed_email(sender=sender, signing_domain="qq.com", key_bits=1024,
                                  signed_headers=signed_required)
        cases = [
            ("forged From", valid.replace(b"owner@qq.com", b"attacker@qq.com", 1), sender),
            ("tampered signed To", self.replace_outer_header(valid, "To", "other@example.net"), sender),
            ("allowed but mismatched d", self.signed_email(sender=sender, signing_domain="foxmail.com",
                key_bits=1024, signed_headers=signed_required), sender),
            ("subdomain d", self.signed_email(sender=sender, signing_domain="mail.qq.com",
                key_bits=1024, signed_headers=signed_required), sender),
            ("unsigned To", self.signed_email(sender=sender, signing_domain="qq.com", key_bits=1024,
                signed_headers=[b"from", b"subject", b"date"]), sender),
            ("unsigned Subject", self.signed_email(sender=sender, signing_domain="qq.com", key_bits=1024,
                signed_headers=[b"from", b"to", b"date"]), sender),
            ("tampered signed Subject", self.replace_outer_header(valid, "Subject", "Changed subject"), sender),
            ("tampered signed body", valid.replace(b"please process this fixture", b"please PROCESS this fixture"), sender),
            ("SHA-1", self.signed_email(sender=sender, signing_domain="qq.com", key_bits=1024,
                signed_headers=signed_required, signature_algorithm=b"rsa-sha1"), sender),
            ("body length tag", self.signed_email(sender=sender, signing_domain="qq.com", key_bits=1024,
                signed_headers=signed_required, length=True), sender),
        ]
        unsigned = EmailMessage()
        unsigned["From"], unsigned["To"], unsigned["Subject"] = sender, "device@example.net", "Unsigned"
        unsigned["Date"] = format_datetime(self.now)
        unsigned.set_content("unsigned body")
        cases.append(("missing signature", unsigned.as_bytes(policy=policy.SMTP), sender))
        for label, raw, expected_sender in cases:
            with self.subTest(label=label), self.assertRaises(ValueError):
                self.verify_fixture(raw, sender=expected_sender)

    def test_unsigned_message_id_changes_do_not_change_qq_replay_nonce(self):
        self.enable_plain()
        sender = "owner@qq.com"
        contact = self.service.create_contact({"email": sender})["contact"]
        since = dt.datetime.fromisoformat(self.service._config["plainEmailSince"].replace("Z", "+00:00"))
        raw = self.signed_email(sender=sender, signing_domain="qq.com", key_bits=1024,
            signed_headers=[b"from", b"to", b"subject", b"date"],
            date=since + dt.timedelta(seconds=1))
        original_id = BytesParser(policy=policy.default).parsebytes(raw)["Message-ID"]
        changed = raw.replace(str(original_id).encode("ascii"), b"<changed-mid@example.invalid>", 1)
        first = self.verify_fixture(raw, sender=sender)
        second = self.verify_fixture(changed, sender=sender)
        self.assertNotEqual(first["messageId"], second["messageId"])
        self.assertEqual(first["nonce"], second["nonce"])

        verifier = lambda *args, **kwargs: _verify_plain_email(*args, dnsfunc=self.dnsfunc, **kwargs)
        with patch("email_control._verify_plain_email", side_effect=verifier), \
             patch("email_control.smtplib.SMTP_SSL", FakeSMTP):
            self.service._process_plain_email(raw, {**contact, "plain_since": None}, "qq-original")
            self.service._process_plain_email(changed, {**contact, "plain_since": None}, "qq-mid-changed")
        self.assertEqual(len(self.harness.start_calls), 1)

    def test_provider_message_id_dedup_and_signature_fingerprint_block_replays(self):
        self.enable_plain()
        sender = "owner@qq.com"
        contact = self.service.create_contact({"email": sender})["contact"]
        since = dt.datetime.fromisoformat(self.service._config["plainEmailSince"].replace("Z", "+00:00"))
        raw = self.signed_email(sender=sender, signing_domain="qq.com", key_bits=1024,
            signed_headers=[b"from", b"to", b"subject", b"date"], date=since + dt.timedelta(seconds=1))
        original_id = BytesParser(policy=policy.default).parsebytes(raw)["Message-ID"]
        changed_mid = raw.replace(str(original_id).encode("ascii"), b"<changed-mid@example.invalid>", 1)
        verifier = lambda *args, **kwargs: _verify_plain_email(*args, dnsfunc=self.dnsfunc, **kwargs)
        with patch.object(self.service, "_connections", return_value=FakeMailboxIMAP({1: raw})), \
             patch("email_control.smtplib.SMTP_SSL", FakeSMTP), \
             patch("email_control._verify_plain_email", side_effect=verifier):
            self.service.poll_once()
        self.assertEqual(len(self.harness.start_calls), 1)

        repeated = FakeMailboxIMAP({2: raw})
        with patch.object(self.service, "_connections", return_value=repeated):
            self.service.poll_once()
        self.assertEqual(repeated.full_fetches, 0, "contact_id + Message-ID header claim should skip known mail")

        changed = FakeMailboxIMAP({3: changed_mid})
        with patch.object(self.service, "_connections", return_value=changed), \
             patch("email_control.smtplib.SMTP_SSL", FakeSMTP), \
             patch("email_control._verify_plain_email", side_effect=verifier):
            self.service.poll_once()
        self.assertEqual(changed.full_fetches, 1, "changed unsigned Message-ID requires signature verification")
        self.assertEqual(len(self.harness.start_calls), 1, "the same signed b= fingerprint must not replay")
        self.assertEqual(len(self.commands()), 1)

    def test_unsigned_outer_mime_headers_do_not_change_authenticated_provider_body(self):
        sender = "owner@qq.com"
        signed_headers = [b"from", b"to", b"subject", b"date"]
        cases = (
            ("quoted-printable", "plain", "请保留说明=并读取文件"),
            ("base64", "plain", "请保留说明并读取文件"),
            ("quoted-printable", "multipart", "多段正文中文任务"),
        )
        for encoding, body_type, body in cases:
            with self.subTest(encoding=encoding, body_type=body_type):
                raw = self.signed_email(sender=sender, signing_domain="qq.com", key_bits=1024,
                    signed_headers=signed_headers, transfer_encoding=encoding, body_type=body_type,
                    body=body, html_body="<p>HTML side text</p><script>not an instruction</script>")
                baseline = self.verify_fixture(raw, sender=sender)
                tampered = self.replace_outer_header(raw, "Content-Type", "application/octet-stream")
                tampered = self.replace_outer_header(tampered, "Content-Transfer-Encoding", "base64")
                tampered = self.replace_outer_header(tampered, "MIME-Version", "9.9")
                parsed = self.verify_fixture(tampered, sender=sender)
                self.assertEqual((parsed["action"], parsed["title"], parsed["prompt"], parsed["nonce"]),
                                 (baseline["action"], baseline["title"], baseline["prompt"], baseline["nonce"]))
                self.assertIn(body, parsed["prompt"])
                self.assertNotIn("not an instruction", parsed["prompt"])

        empty = self.signed_email(sender=sender, signing_domain="qq.com", key_bits=1024,
            signed_headers=signed_headers, body="", subject="请检查下载目录")
        title_only = self.verify_fixture(empty, sender=sender)
        self.assertEqual(title_only["prompt"], "邮件主题：请检查下载目录")

    def test_fresh_unsigned_date_cannot_refresh_stale_dkim_timestamp(self):
        sender = "owner@163.com"
        stale = self.now - dt.timedelta(hours=25)
        raw = self.signed_email(sender=sender, signing_domain="163.com", key_bits=1024,
            signed_headers=[b"from", b"to", b"subject"], date=self.now, timestamp=stale)
        with self.assertRaises(ValueError):
            self.verify_fixture(raw, sender=sender, since=(self.now - dt.timedelta(days=2)).isoformat())

    def test_ununsigned_date_is_accepted_only_with_recent_dkim_timestamp(self):
        sender = "owner@126.com"
        signed_headers = [b"from", b"to", b"subject"]
        recent = self.signed_email(sender=sender, signing_domain="126.com", key_bits=1024,
            signed_headers=signed_headers, date=self.now, timestamp=self.now)
        result = self.verify_fixture(recent, sender=sender,
            since=(self.now - dt.timedelta(hours=2)).isoformat())
        self.assertIn("please process this fixture", result["prompt"])

    def test_empty_body_falls_back_to_authenticated_subject_but_nonempty_body_wins(self):
        title = "请检查下载目录中的报告"
        empty = self.verify_fixture(self.signed_email(body=" \n\t", subject=title))
        self.assertEqual((empty["action"], empty["title"], empty["prompt"]),
                         ("submit", title, "邮件主题：" + title))
        no_body_part = self.verify_fixture(self.signed_email(body_type="empty-multipart", subject=title))
        self.assertEqual(no_body_part["prompt"], "邮件主题：" + title)
        empty_html = self.verify_fixture(self.signed_email(body="<html><body> \n&nbsp; </body></html>",
                                                            body_type="html", subject="HTML 空白"))
        self.assertEqual(empty_html["prompt"], "邮件主题：HTML 空白")
        multilingual = self.verify_fixture(self.signed_email(body="", subject="给手机：请整理下载文件"))
        self.assertEqual(multilingual["prompt"], "邮件主题：给手机：请整理下载文件")
        body = self.verify_fixture(self.signed_email(body="正文中的任务指令优先", subject="普通主题"))
        self.assertEqual(body["action"], "submit")
        self.assertIn("邮件主题：普通主题", body["prompt"])
        self.assertIn("正文中的任务指令优先", body["prompt"])
        self.assertNotEqual(body["prompt"], body["title"])
        for subject in ("确认 12345678-1234-1234-1234-123456789abc", "Re: 确认安装！", "Fwd: 我确认需要安装，你来点确认"):
            with self.subTest(subject=subject):
                result = self.verify_fixture(self.signed_email(body=" \n", subject=subject))
                self.assertEqual(result["action"], "approval_help")
                self.assertNotIn("approvalId", result)
        self.enable_plain()
        since = dt.datetime.fromisoformat(self.service._config["plainEmailSince"].replace("Z", "+00:00"))
        task_mail = self.signed_email(body="  ", subject=title, date=since + dt.timedelta(seconds=1))
        contact = {**self.contact, "email": "owner@gmail.com", "plain_since": None}
        verifier = lambda *a, **kw: _verify_plain_email(*a, dnsfunc=self.dnsfunc, **kw)
        with patch("email_control._verify_plain_email", side_effect=verifier), patch("email_control.smtplib.SMTP_SSL", FakeSMTP):
            self.service._process_plain_email(task_mail, contact, "fixture-title-only")
        self.assertEqual(len(self.harness.start_calls), 1)
        self.assertTrue(self.harness.start_calls[0]["prompt"].endswith(title))

    def test_long_task_subject_is_preserved_and_prompt_limit_rejects(self):
        long_subject = "请检查文件" * 45 + "最后请保留此尾部指令"
        result = self.verify_fixture(self.signed_email(body="", subject=long_subject))
        self.assertEqual(result["subject"], long_subject)
        self.assertTrue(result["prompt"].endswith("邮件主题：" + long_subject))
        self.assertIn("最后请保留此尾部指令", result["prompt"])
        self.assertEqual(result["title"], long_subject[:100])

        oversized = self.signed_email(body="", subject="任务" * 4100)
        with self.assertRaisesRegex(ValueError, "8000字"):
            self.verify_fixture(oversized)

    def test_dkim_verified_first_line_only_parses_full_approval_id(self):
        approval_id = str(uuid.uuid4())
        accepted = self.verify_fixture(self.signed_email(body="\n确认 " + approval_id + "\n> 拒绝 " + approval_id))
        self.assertEqual((accepted["action"], accepted["approvalId"]), ("approve", approval_id))
        rejected = self.verify_fixture(self.signed_email(body="拒绝 " + approval_id + "\nquoted text"))
        self.assertEqual((rejected["action"], rejected["approvalId"]), ("decline", approval_id))
        ambiguous = self.verify_fixture(self.signed_email(body="确认\n\n> 确认 " + approval_id))
        self.assertEqual(ambiguous["action"], "approve_unambiguous")
        self.assertIsNone(ambiguous["approvalId"])
        unsafe = self.verify_fixture(self.signed_email(body="确认不要安装"))
        self.assertEqual(unsafe["action"], "approval_help")
        not_exact = self.verify_fixture(self.signed_email(body="我确认需要安装，你来点确认，但目标不对"))
        self.assertEqual(not_exact["action"], "approval_help")
        ordinary = self.verify_fixture(self.signed_email(body="Please do a task.\n\n> 确认 " + approval_id))
        self.assertEqual(ordinary["action"], "submit")

    def test_dkim_verified_email_decides_owned_approval_and_missing_id_never_launches(self):
        self.enable_plain()
        start = dt.datetime.fromisoformat(self.service._config["plainEmailSince"].replace("Z", "+00:00"))
        submit = self.signed_email(body="A harmless original task.", subject="Original task", date=start + dt.timedelta(seconds=1))
        with patch("email_control._verify_plain_email", side_effect=lambda *a, **kw: _verify_plain_email(*a, dnsfunc=self.dnsfunc, **kw)), \
             patch("email_control.smtplib.SMTP_SSL", FakeSMTP):
            self.service._process_plain_email(submit, {**self.contact, "email": "owner@gmail.com", "plain_since": None}, "fixture-original-header")
        FakeSMTP.instances.clear()
        with self.harness.store.lock, self.harness.store.db:
            self.harness.store.db.execute("UPDATE tasks SET status='waiting_approval' WHERE id='task_1'")
        approval = self._approval()
        self._approval()  # Ambiguous short confirmations cannot choose between two pending requests.
        confirmation = self.signed_email(body="\n确认 " + approval["id"] + "\n\n> old quoted text",
            subject="Re: Original task", date=start + dt.timedelta(seconds=2))
        no_id = self.signed_email(body="确认", subject="Re: Original task", date=start + dt.timedelta(seconds=3))
        verifier = lambda *a, **kw: _verify_plain_email(*a, dnsfunc=self.dnsfunc, **kw)
        contact = {**self.contact, "email": "owner@gmail.com", "plain_since": None}
        with patch("email_control._verify_plain_email", side_effect=verifier), patch("email_control.smtplib.SMTP_SSL", FakeSMTP):
            self.service._process_plain_email(no_id, contact, "fixture-no-id")
            self.service._process_plain_email(confirmation, contact, "fixture-approval")
        self.assertEqual(len(self.harness.start_calls), 1)
        self.assertEqual(self.harness.permission_decisions, [(approval["id"], "accept", "task_1")])
        self.assertEqual(len(FakeSMTP.instances), 2)
        no_id_report = BytesParser(policy=policy.default).parsebytes(FakeSMTP.instances[0].sent[0].as_bytes()).get_content()
        self.assertIn("确认 <审批ID>", no_id_report)
        self.assertEqual(FakeSMTP.instances[1].sent[0]["To"], "owner@gmail.com")

    def test_exact_short_confirmation_accepts_only_one_owned_unexpired_approval(self):
        self.enable_plain()
        start = dt.datetime.fromisoformat(self.service._config["plainEmailSince"].replace("Z", "+00:00"))
        submit = self.signed_email(body="Original task.", date=start + dt.timedelta(seconds=1))
        verifier = lambda *a, **kw: _verify_plain_email(*a, dnsfunc=self.dnsfunc, **kw)
        contact = {**self.contact, "email": "owner@gmail.com", "plain_since": None}
        with patch("email_control._verify_plain_email", side_effect=verifier), patch("email_control.smtplib.SMTP_SSL", FakeSMTP):
            self.service._process_plain_email(submit, contact, "fixture-single-owner-task")
        FakeSMTP.instances.clear()
        with self.harness.store.lock, self.harness.store.db:
            self.harness.store.db.execute("UPDATE tasks SET status='waiting_approval' WHERE id='task_1'")
        approval = self._approval()
        short = self.signed_email(body="我确认需要安装，你来点确认！", date=start + dt.timedelta(seconds=2))
        with patch("email_control._verify_plain_email", side_effect=verifier), patch("email_control.smtplib.SMTP_SSL", FakeSMTP):
            self.service._process_plain_email(short, contact, "fixture-short-confirmation")
        self.assertEqual(self.harness.permission_decisions, [(approval["id"], "accept", "task_1")])
        self.assertEqual(len(self.harness.start_calls), 1)

    def test_approval_decision_does_not_hold_store_lock_and_keeps_auth_lock(self):
        self.enable_plain()
        start = dt.datetime.fromisoformat(self.service._config["plainEmailSince"].replace("Z", "+00:00"))
        verifier = lambda *a, **kw: _verify_plain_email(*a, dnsfunc=self.dnsfunc, **kw)
        contact = {**self.contact, "email": "owner@gmail.com", "plain_since": None}
        submit = self.signed_email(body="Original task.", date=start + dt.timedelta(seconds=1))
        with patch("email_control._verify_plain_email", side_effect=verifier), patch("email_control.smtplib.SMTP_SSL", FakeSMTP):
            self.service._process_plain_email(submit, contact, "fixture-lock-submit")
        with self.harness.store.lock, self.harness.store.db:
            self.harness.store.db.execute("UPDATE tasks SET status='waiting_approval' WHERE id='task_1'")
        approval = self._approval()
        original_decide = self.harness.decide_android_permission
        probe_state = {}

        def guarded_decide(approval_id, decision, *, task_id=None):
            store_acquired = threading.Event()
            def take_store_lock():
                with self.harness.store.lock:
                    store_acquired.set()
            store_probe = threading.Thread(target=take_store_lock)
            store_probe.start()
            store_probe.join(1)
            probe_state["store_free"] = store_acquired.is_set()

            auth_acquired = threading.Event()
            def take_auth_lock():
                with self.service._auth_lock:
                    auth_acquired.set()
            auth_probe = threading.Thread(target=take_auth_lock)
            auth_probe.start()
            auth_probe.join(0.05)
            probe_state["auth_serialized"] = not auth_acquired.is_set()
            auth_probe.join(1)
            store_probe.join(1)
            return original_decide(approval_id, decision, task_id=task_id)

        self.harness.decide_android_permission = guarded_decide
        confirmation = self.signed_email(body="确认 " + approval["id"], date=start + dt.timedelta(seconds=2))
        with patch("email_control._verify_plain_email", side_effect=verifier), patch("email_control.smtplib.SMTP_SSL", FakeSMTP):
            self.service._process_plain_email(confirmation, contact, "fixture-lock-approval")
        self.assertTrue(probe_state.get("store_free"), "store lock must be released before entering the harness")
        self.assertTrue(probe_state.get("auth_serialized"), "contact revocation must serialize with approval dispatch")
        self.assertEqual(self.harness.permission_decisions, [(approval["id"], "accept", "task_1")])

    def test_waiting_approval_notice_to_gmail_is_readable_and_sent_once(self):
        self.enable_plain()
        start = dt.datetime.fromisoformat(self.service._config["plainEmailSince"].replace("Z", "+00:00"))
        submit = self.signed_email(body="Original task.", date=start + dt.timedelta(seconds=1))
        verifier = lambda *a, **kw: _verify_plain_email(*a, dnsfunc=self.dnsfunc, **kw)
        contact = {**self.contact, "email": "owner@gmail.com", "plain_since": None}
        with patch("email_control._verify_plain_email", side_effect=verifier), patch("email_control.smtplib.SMTP_SSL", FakeSMTP):
            self.service._process_plain_email(submit, contact, "fixture-notice-submit")
        FakeSMTP.instances.clear()
        with self.harness.store.lock, self.harness.store.db:
            self.harness.store.db.execute("UPDATE tasks SET status='waiting_approval' WHERE id='task_1'")
        approval = self._approval()
        with patch("email_control.smtplib.SMTP_SSL", FakeSMTP):
            self.service._deliver_finished()
            self.service._deliver_finished()
        self.assertEqual(len(FakeSMTP.instances), 1)
        notice = FakeSMTP.instances[0].sent[0]
        self.assertEqual(notice["To"], "owner@gmail.com")
        body = notice.get_content()
        for expected in ("tap Allow", "com.example.fixture", approval["id"], "确认 " + approval["id"], "2026-"):
            self.assertIn(expected, body)
        original = next(row for row in self.commands() if row["action"] == "submit")
        self.assertEqual(original["status"], "running")

    def test_forged_authentication_results_and_tampered_fields_are_rejected(self):
        unsigned = EmailMessage()
        unsigned["From"] = "owner@gmail.com"
        unsigned["To"] = "device@example.net"
        unsigned["Subject"] = "No signature"
        unsigned["Date"] = format_datetime(self.now)
        unsigned["Message-ID"] = "<unsigned@gmail.com>"
        unsigned["Authentication-Results"] = "mx.google.com; dkim=pass"
        unsigned.set_content("untrusted body")
        cases = [("spoofed auth-results", unsigned.as_bytes(policy=policy.SMTP), "owner@gmail.com")]
        valid = self.signed_email()
        cases.extend((
            ("forged From", valid.replace(b"owner@gmail.com", b"attacker@gmail.com", 1), "owner@gmail.com"),
            ("non-Gmail sender identity", valid, "owner@attacker.example"),
            ("tampered To", valid.replace(b"device@example.net", b"other@example.net", 1), "owner@gmail.com"),
            ("tampered Subject", valid.replace(b"Subject: Fixture request", b"Subject: New request", 1), "owner@gmail.com"),
            ("tampered body", valid.replace(b"please process this fixture", b"please PROCESS this fixture", 1), "owner@gmail.com"),
        ))
        for label, raw, sender in cases:
            with self.subTest(label=label), self.assertRaises(ValueError):
                self.verify_fixture(raw, sender=sender)

    def test_unsigned_critical_headers_duplicates_and_body_length_tag_are_rejected(self):
        no_to = self.signed_email(include_headers=[b"from", b"subject", b"date", b"message-id",
                                                   b"content-type", b"mime-version", b"content-transfer-encoding"])
        duplicate = self.signed_email().replace(b"Subject: Fixture request\r\n", b"Subject: Fixture request\r\nSubject: second\r\n", 1)
        length_limited = self.signed_email(length=True)
        for label, raw in (("unsigned To", no_to), ("duplicate Subject", duplicate), ("DKIM l tag", length_limited)):
            with self.subTest(label=label), self.assertRaises(ValueError):
                self.verify_fixture(raw)

    def test_old_pre_enable_and_future_dates_are_rejected(self):
        since = self.now - dt.timedelta(hours=1)
        cases = (
            ("before enable", self.now - dt.timedelta(hours=2), since),
            ("over 24 hours", self.now - dt.timedelta(hours=25), self.now - dt.timedelta(days=2)),
            ("future over tolerance", self.now + dt.timedelta(seconds=121), self.now - dt.timedelta(days=2)),
        )
        for label, date, start in cases:
            with self.subTest(label=label), self.assertRaises(ValueError):
                self.verify_fixture(self.signed_email(date=date), since=start.isoformat())

    def test_html_is_text_only_and_attachments_or_oversized_prompt_are_rejected(self):
        html = self.signed_email(body="<p>Keep this text</p><script>secret()</script>", body_type="html")
        result = self.verify_fixture(html)
        self.assertIn("Keep this text", result["prompt"])
        self.assertNotIn("secret()", result["prompt"])
        for label, raw in (("attachment", self.signed_email(attachment=True)),
                           ("prompt limit", self.signed_email(body="x" * 8000))):
            with self.subTest(label=label), self.assertRaises(ValueError):
                self.verify_fixture(raw)

    def test_authenticated_email_runs_once_and_ack_and_result_use_fixed_recipient(self):
        self.enable_plain()
        since = dt.datetime.fromisoformat(self.service._config["plainEmailSince"].replace("Z", "+00:00"))
        raw = self.signed_email(body="Do the harmless fixture task.", subject="Fixture task", date=since + dt.timedelta(seconds=1))
        imap = FakeMailboxIMAP({1: raw})
        original = _verify_plain_email
        verifier = lambda *args: original(*args, dnsfunc=self.dnsfunc)
        with patch.object(self.service, "_connections", return_value=imap), \
             patch("email_control._verify_plain_email", side_effect=verifier), \
             patch("email_control.smtplib.SMTP_SSL", FakeSMTP):
            self.service.poll_once()
            self.service.poll_once()
        self.assertEqual(len(self.harness.start_calls), 1)
        self.assertEqual(self.commands()[0]["transport"], "plain")
        self.assertEqual(self.commands()[0]["ack_status"], "sent")
        self.assertEqual(len(FakeSMTP.instances), 1)
        ack = FakeSMTP.instances[0].sent[0]
        self.assertEqual(ack["To"], "owner@gmail.com")
        self.assertIn("已验证你的可信邮箱身份", ack.get_content())
        with self.harness.store.lock, self.harness.store.db:
            self.harness.store.db.execute("UPDATE tasks SET status='completed',result=? WHERE id='task_1'",
                                          ("Done " + self.contact_secret + " fixture-mail-password",))
        with patch("email_control.smtplib.SMTP_SSL", FakeSMTP):
            self.service._deliver_finished()
        self.assertEqual(len(FakeSMTP.instances), 2)
        result_mail = FakeSMTP.instances[1].sent[0]
        self.assertEqual(result_mail["To"], "owner@gmail.com")
        self.assertIn("已完成", result_mail.get_content())
        self.assertNotIn(self.contact_secret, result_mail.get_content())
        self.assertNotIn("fixture-mail-password", result_mail.get_content())

    def test_revocation_during_dkim_verification_prevents_launch(self):
        self.enable_plain()
        since = dt.datetime.fromisoformat(self.service._config["plainEmailSince"].replace("Z", "+00:00"))
        raw = self.signed_email(date=since + dt.timedelta(seconds=1))
        imap = FakeMailboxIMAP({1: raw})
        original = _verify_plain_email
        def verify_then_revoke(*args):
            result = original(*args, dnsfunc=self.dnsfunc)
            self.service.contact_action(self.contact["id"], "toggle", enabled=False)
            return result
        with patch.object(self.service, "_connections", return_value=imap), \
             patch("email_control._verify_plain_email", side_effect=verify_then_revoke):
            self.service.poll_once()
        self.assertEqual(self.harness.start_calls, [])
        self.assertEqual(self.commands(), [])

    def test_ambiguous_ack_send_is_never_retried(self):
        self.enable_plain()
        since = dt.datetime.fromisoformat(self.service._config["plainEmailSince"].replace("Z", "+00:00"))
        raw = self.signed_email(date=since + dt.timedelta(seconds=1))
        imap = FakeMailboxIMAP({1: raw})
        attempts = []
        class UncertainSMTP(FakeSMTP):
            def send_message(self, message):
                attempts.append(message)
                raise OSError("fixture uncertain delivery")
        original = _verify_plain_email
        verifier = lambda *args: original(*args, dnsfunc=self.dnsfunc)
        with patch.object(self.service, "_connections", return_value=imap), \
             patch("email_control._verify_plain_email", side_effect=verifier), \
             patch("email_control.smtplib.SMTP_SSL", UncertainSMTP):
            self.service.poll_once()
            self.service.poll_once()
        self.assertEqual(len(self.harness.start_calls), 1)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(self.commands()[0]["ack_status"], "uncertain")

    def test_authenticated_attachment_sends_one_fixed_recipient_rejection(self):
        self.enable_plain()
        since = dt.datetime.fromisoformat(self.service._config["plainEmailSince"].replace("Z", "+00:00"))
        raw = self.signed_email(subject="Attachment request", date=since + dt.timedelta(seconds=1), attachment=True)
        imap = FakeMailboxIMAP({1: raw})
        original = _verify_plain_email
        verifier = lambda *args: original(*args, dnsfunc=self.dnsfunc)
        with patch.object(self.service, "_connections", return_value=imap), \
             patch("email_control._verify_plain_email", side_effect=verifier), \
             patch("email_control.smtplib.SMTP_SSL", FakeSMTP):
            self.service.poll_once()
            self.service.poll_once()
        self.assertEqual(self.harness.start_calls, [])
        self.assertEqual(len(FakeSMTP.instances), 1)
        refusal = FakeSMTP.instances[0].sent[0]
        self.assertEqual(refusal["To"], "owner@gmail.com")
        self.assertIn("暂不支持附件", refusal.get_content())
        self.assertEqual(self.commands()[0]["status"], "rejected")


if __name__ == "__main__":
    unittest.main()
