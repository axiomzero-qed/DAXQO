import datetime as dt
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from email.message import EmailMessage
from unittest.mock import patch

import email_control
from email_control import MailControlService
from remote_protocol import verify
from server import Harness
import test_email_control as email_fixtures

FakeSMTP = email_fixtures.FakeSMTP


class EmailAttachmentTests(unittest.TestCase):
    # Reuse only the established fixture helpers; do not inherit its test suite.
    command_email = email_fixtures.MailControlTests.command_email
    commands = email_fixtures.MailControlTests.commands

    def setUp(self):
        email_fixtures.MailControlTests.setUp(self)
        self.workspace = Path(self.tmp.name) / "workspace"
        self.workspace.mkdir()
        self.harness.workspace = self.workspace
        self.harness.resolve_workspace_path = types.MethodType(Harness.resolve_workspace_path, self.harness)
        self.harness.read_workspace_download = types.MethodType(Harness.read_workspace_download, self.harness)
        self.harness.result_artifacts = types.MethodType(Harness.result_artifacts, self.harness)
        FakeSMTP.instances.clear()

    def _complete_owned_task(self, result, *, transport="signed", status="completed"):
        self.service._process_email(self.command_email(prompt="make local fixture artifacts"))
        task_id = "task_" + str(len(self.harness.start_calls))
        command = next(row for row in self.commands() if row["task_id"] == task_id)
        with self.harness.store.lock, self.harness.store.db:
            self.harness.store.db.execute(
                "UPDATE tasks SET status=?,result=?,title=? WHERE id=?", (status, result, "Fixture成果", task_id))
            if transport == "plain":
                self.harness.store.db.execute("UPDATE mail_commands SET transport='plain' WHERE id=?", (command["id"],))
        return task_id

    def _deliver(self):
        with patch("email_control.smtplib.SMTP_SSL", FakeSMTP):
            self.service._deliver_finished()

    @staticmethod
    def _body(message):
        return next(part.get_content() for part in message.walk()
                    if part.get_content_type() == "text/plain" and part.get_content_disposition() is None)

    @staticmethod
    def _attachments(message):
        return [(part.get_filename(), part.get_payload(decode=True)) for part in message.walk()
                if part.get_content_disposition() == "attachment"]

    def _reply_parts(self):
        with self.harness.store.lock:
            return [dict(row) for row in self.harness.store.db.execute(
                "SELECT * FROM mail_reply_parts ORDER BY part")]

    def test_plain_and_signed_replies_attach_binary_and_image_and_signed_parts_verify(self):
        binary = self.workspace / "data.bin"
        image = self.workspace / "picture.png"
        binary.write_bytes(b"\x00\xffbinary\x10")
        image.write_bytes(b"\x89PNG\r\n\x1a\nfixture-image")
        result = f"Files: [binary](<{binary.relative_to(self.workspace).as_posix()}>) [image](<{image.relative_to(self.workspace).as_posix()}>)"

        for transport in ("plain", "signed"):
            with self.subTest(transport=transport):
                FakeSMTP.instances.clear()
                task_id = self._complete_owned_task(result, transport=transport)
                self._deliver()
                self.assertEqual(len(FakeSMTP.instances), 1)
                message = FakeSMTP.instances[0].sent[0]
                self.assertEqual({name: raw for name, raw in self._attachments(message)},
                                 {"data.bin": binary.read_bytes(), "picture.png": image.read_bytes()})
                if transport == "signed":
                    payload = json.loads(self._body(message))
                    verified = verify(payload, self.secret)
                    self.assertEqual(verified["report"]["taskId"], task_id)
                    self.assertEqual((verified["report"]["part"], verified["report"]["parts"]), (1, 1))
                    self.assertEqual(verified["report"]["attachments"]["count"], 2)
                parts = self._reply_parts()
                self.assertEqual([(r["part"], r["parts"], r["status"], r["attachment_count"])
                                  for r in parts], [(1, 1, "sent", 2)])
                with self.harness.store.lock, self.harness.store.db:
                    self.harness.store.db.execute("DELETE FROM mail_reply_parts")
                    self.harness.store.db.execute("DELETE FROM mail_commands")
                    self.harness.store.db.execute("DELETE FROM tasks")

    def test_batches_by_raw_total_and_skips_single_oversize_sparse_file(self):
        (self.workspace / "one.bin").write_bytes(b"1" * 11)
        (self.workspace / "two.bin").write_bytes(b"2" * 11)
        result = "[one](<one.bin>) [two](<two.bin>)"
        self._complete_owned_task(result)
        with patch.object(email_control, "MAX_FILE_BYTES", 20):
            self._deliver()
        self.assertEqual(len(FakeSMTP.instances), 2)
        parts = self._reply_parts()
        self.assertEqual([(p["part"], p["parts"], p["status"], p["attachment_count"])
                          for p in parts], [(1, 2, "sent", 1), (2, 2, "sent", 1)])
        for instance in FakeSMTP.instances:
            payload = json.loads(self._body(instance.sent[0]))
            report = verify(payload, self.secret)["report"]
            self.assertIn(report["part"], (1, 2))
            self.assertEqual(report["parts"], 2)
            self.assertLessEqual(sum(len(raw) for _, raw in self._attachments(instance.sent[0])), 20)
        self.assertEqual(sum(len(raw) for smtp in FakeSMTP.instances for _, raw in self._attachments(smtp.sent[0])), 22)

        FakeSMTP.instances.clear()
        (self.workspace / "oversize.bin").touch()
        with (self.workspace / "oversize.bin").open("r+b") as handle:
            handle.truncate(20 * 1024 * 1024 + 1)
        self._complete_owned_task("[large](<oversize.bin>)")
        self._deliver()
        msg = FakeSMTP.instances[-1].sent[0]
        self.assertEqual(self._attachments(msg), [])
        payload = verify(json.loads(self._body(msg)), self.secret)
        self.assertIn("超过 20 MB", payload["report"]["attachmentNotes"])

    def test_only_referenced_current_task_files_attach_not_foreign_hidden_or_private(self):
        (self.workspace / "mine.txt").write_text("mine", encoding="utf-8")
        (self.workspace / "unreferenced.txt").write_text("unreferenced", encoding="utf-8")
        (self.workspace / ".hidden.txt").write_text("hidden", encoding="utf-8")
        (self.harness.provider_dir / "private.txt").write_text("private", encoding="utf-8")
        other = self.service.create_contact({"email": "other@example.org"})
        foreign_task = self.harness.start_task({"prompt": "other person's task"})["id"]
        (self.workspace / "foreign.txt").write_text("foreign", encoding="utf-8")
        with self.harness.store.lock, self.harness.store.db:
            self.harness.store.db.execute("UPDATE tasks SET status='completed',result=? WHERE id=?",
                ("[foreign](<foreign.txt>)", foreign_task))
            self.harness.store.db.execute(
                "INSERT INTO mail_commands(id,contact_id,nonce,action,task_id,status,received_at) VALUES(?,?,?,?,?,?,?)",
                ("foreign-command", other["contact"]["id"], "foreign_nonce_0000000000000001", "submit",
                 foreign_task, "running", dt.datetime.now(dt.timezone.utc).isoformat()))
        result = " ".join(f"[ref](<{name}>)" for name in ("mine.txt", ".hidden.txt", "private/private.txt"))
        self._complete_owned_task(result)
        self._deliver()
        by_recipient = {message["To"]: {name for name, _ in self._attachments(message)}
                        for smtp in FakeSMTP.instances for message in smtp.sent}
        self.assertEqual(by_recipient["owner@example.org"], {"mine.txt"})
        self.assertEqual(by_recipient["other@example.org"], {"foreign.txt"})

    def test_same_names_are_renamed_and_directory_expansion_is_scoped(self):
        for folder, content in (("first", b"first"), ("second", b"second")):
            directory = self.workspace / folder
            directory.mkdir()
            (directory / "same.txt").write_bytes(content)
            (directory / "extra.txt").write_bytes(folder.encode())
        (self.workspace / "sibling.txt").write_bytes(b"sibling")
        self._complete_owned_task("`first` and `second`")
        self._deliver()
        attachments = self._attachments(FakeSMTP.instances[0].sent[0])
        self.assertEqual({name for name, _ in attachments}, {"same.txt", "extra.txt", "same_2.txt", "extra_2.txt"})
        self.assertNotIn("sibling.txt", {name for name, _ in attachments})

    def test_second_smtp_failure_marks_uncertain_and_never_retries(self):
        for name in ("one.bin", "two.bin", "three.bin"):
            (self.workspace / name).write_bytes(name.encode())
        self._complete_owned_task("[one](<one.bin>) [two](<two.bin>) [three](<three.bin>)")
        real_send = FakeSMTP.send_message
        class FailingSecond(FakeSMTP):
            calls = 0
            def send_message(self, message):
                type(self).calls += 1
                if type(self).calls == 2:
                    raise OSError("connection lost")
                return real_send(self, message)
        with patch.object(email_control, "MAX_FILE_BYTES", 10), patch("email_control.smtplib.SMTP_SSL", FailingSecond):
            self.service._deliver_finished()
            self.service._deliver_finished()
        self.assertEqual(FailingSecond.calls, 2)
        rows = self._reply_parts()
        self.assertEqual([row["status"] for row in rows], ["sent", "uncertain", "not_sent"])
        self.assertEqual(self.commands()[0]["status"], "reply_uncertain")

    def test_contact_revocation_between_parts_keeps_first_sent(self):
        (self.workspace / "one.bin").write_bytes(b"one bytes")
        (self.workspace / "two.bin").write_bytes(b"two bytes")
        self._complete_owned_task("[one](<one.bin>) [two](<two.bin>)")
        with patch.object(email_control, "MAX_FILE_BYTES", 10):
            original_transmit = self.service._transmit
            sent = []
            def transmit_then_revoke(config, contact, message):
                original_transmit(config, contact, message)
                sent.append(message)
                if len(sent) == 1:
                    self.service.contact_action(self.contact["id"], "toggle", enabled=False)
            with patch.object(self.service, "_transmit", side_effect=transmit_then_revoke), \
                    patch("email_control.smtplib.SMTP_SSL", FakeSMTP):
                self.service._deliver_finished()
        self.assertEqual(len(sent), 1)
        self.assertEqual([row["status"] for row in self._reply_parts()], ["sent", "disabled"])
        self.assertEqual(self.commands()[0]["status"], "interrupted")

    def test_restart_converts_inflight_parts_and_does_not_resend(self):
        self._complete_owned_task("finished")
        command = self.commands()[0]
        with self.harness.store.lock, self.harness.store.db:
            self.harness.store.db.execute("UPDATE mail_commands SET status='replying' WHERE id=?", (command["id"],))
            self.harness.store.db.execute("INSERT INTO mail_reply_parts(command_id,part,parts,message_id,status,attachment_count) VALUES(?,1,2,'<one>','sending',1)", (command["id"],))
            self.harness.store.db.execute("INSERT INTO mail_reply_parts(command_id,part,parts,message_id,status,attachment_count) VALUES(?,2,2,'<two>','pending',1)", (command["id"],))
        self.service = MailControlService(self.harness)
        self.assertEqual([row["status"] for row in self._reply_parts()], ["uncertain", "not_sent"])
        self._deliver()
        self.assertEqual(FakeSMTP.instances, [])

    def test_changed_missing_and_credential_files_are_skipped_with_notes(self):
        changed = self.workspace / "changed.txt"
        missing = self.workspace / "missing.txt"
        password_file = self.workspace / "password.txt"
        secret_file = self.workspace / "secret.txt"
        for path, content in ((changed, b"original"), (missing, b"remove"),
                              (password_file, self.cfg["password"].encode()), (secret_file, self.secret.encode())):
            path.write_bytes(content)
        result = " ".join(f"[file](<{path.name}>)" for path in (changed, missing, password_file, secret_file))
        self._complete_owned_task(result)
        original_groups = self.service._attachment_groups
        def discover_then_mutate(report, contact):
            groups, notes = original_groups(report, contact)
            changed.write_bytes(b"changed with longer content")
            missing.unlink()
            return groups, notes
        with patch.object(self.service, "_attachment_groups", side_effect=discover_then_mutate), \
                patch("email_control.smtplib.SMTP_SSL", FakeSMTP):
            self.service._deliver_finished()
        message = FakeSMTP.instances[0].sent[0]
        self.assertEqual(self._attachments(message), [])
        self.assertIn("已更改", self._body(message))
        self.assertIn("本机凭据", self._body(message))


if __name__ == "__main__":
    unittest.main()
