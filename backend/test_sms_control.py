"""SMS intake/reply safety tests using a real SQLite store and a fake native bridge."""
import tempfile
import time
import datetime as dt
import unittest
from pathlib import Path

import server
from sms_control import SmsControlService, normalize_china_mobile, _short_result
from android_mcp_server import BridgeError


class FakeHarness:
    def __init__(self, root):
        self.store = server.Store(root / "harness.sqlite3")
        self.provider_dir = root / "state"
        self.provider_dir.mkdir()
        self.state = root / "bridge-state"
        self.state.mkdir()
        capability = self.state / "capability"
        capability.write_text("fixture-capability-0123456789abcdef", encoding="ascii")
        capability.chmod(0o600)
        self.started = []

    @staticmethod
    def safe_text(value, limit=200): return str(value or "")[:limit]

    def start_task(self, body):
        self.started.append(body)
        ident = "task-" + str(len(self.started))
        stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        with self.store.db:
            self.store.db.execute("INSERT INTO tasks(id,title,prompt,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                                  (ident, body["prompt"][:80], body["prompt"], "running", stamp, stamp))
        return {"id": ident}


class FakeSmsBridge:
    def __init__(self, country="cn"):
        self.country = country
        self.subscriptions = [7]
        self.selected = 7
        self.ready = True
        self.permissions = {"readSms": True, "sendSms": True, "readPhoneState": True}
        self.calls = []
        self.messages = []
        self.send_error = False
        self.status_value = "sent"

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path == "/api/sms/status":
            return {"ready": self.ready, "selectedSubscriptionId": self.selected,
                    "permissions": self.permissions,
                    "subscriptions": [{"subscriptionId": sub, "countryIso": self.country,
                        "eligible": True, "blockedReason": None} for sub in self.subscriptions]}
        if path == "/api/sms/config": return {"ok": True}
        if path == "/api/sms/query":
            after = body["afterId"]
            return {"subscriptionId": 7, "truncated": False,
                    "messages": [m for m in self.messages if int(m["id"]) > after][:body["limit"]]}
        if path == "/api/sms/send":
            if self.send_error: raise BridgeError("timeout", "delivery unknown")
            return {"id": "send-1", "status": self.status_value}
        if path.startswith("/api/sms/sends/"): return {"id": "send-1", "status": self.status_value}
        raise AssertionError((method, path, body))


class SmsControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.harness = FakeHarness(self.root)
        self.sms = SmsControlService(self.harness)
        self.bridge = FakeSmsBridge()
        self.sms.bridge = self.bridge

    def tearDown(self):
        self.sms.close()
        self.harness.store.db.close()
        self.temp.cleanup()

    def enable(self, *, country="cn", reply=True):
        self.bridge.country = country
        return self.sms.set_settings({"enabled": True, "subscriptionId": 7, "replyEnabled": reply})

    def contact(self, number="13800138000"):
        row = self.sms.add_contact({"number": number, "label": "test"})["contact"]
        self.sms.contact_action(row["id"], "toggle", {"enabled": True})
        row["enabled"] = True
        return row

    def test_phone_normalization_and_short_codes_rejected(self):
        for value in ("13800138000", "8613800138000", "+8613800138000", "008613800138000"):
            self.assertEqual(normalize_china_mobile(value), "+8613800138000")
        for value in ("10086", "+441234567890", "120", "12345678901", "+8612000000000"):
            with self.subTest(value=value), self.assertRaises(ValueError): normalize_china_mobile(value)
        with self.assertRaises(ValueError): normalize_china_mobile("１３８００１３８０００")
        self.assertIsNone(self.sms._thread, "constructing the service must not start background polling")

    def test_new_contacts_are_disabled_and_enabled_allowlist_is_capped(self):
        first = self.sms.add_contact({"number": "13800138000"})["contact"]
        self.assertFalse(first["enabled"])
        self.assertFalse(self.sms.public()["contacts"][0]["enabled"])
        self.sms.contact_action(first["id"], "toggle", {"enabled": True})
        for index in range(1, 50):
            row = self.sms.add_contact({"number": f"1380013{index:04d}"})["contact"]
            self.sms.contact_action(row["id"], "toggle", {"enabled": True})
        last = self.sms.add_contact({"number": "13900139999"})["contact"]
        with self.assertRaisesRegex(ValueError, "50"):
            self.sms.contact_action(last["id"], "toggle", {"enabled": True})

    def test_duplicate_contact_is_a_readable_input_error(self):
        self.sms.add_contact({"number": "13800138000"})
        with self.assertRaisesRegex(ValueError, "已登记"):
            self.sms.add_contact({"number": "+8613800138000"})

    def test_read_only_mode_does_not_require_send_sms_permission(self):
        self.bridge.permissions = {"readSms": True, "sendSms": False, "readPhoneState": True}
        self.bridge.ready = False
        self.contact()
        result = self.sms.set_settings({"enabled": True, "subscriptionId": 7, "replyEnabled": False})
        self.assertTrue(result["sms"]["ready"])
        self.sms.poll_once()
        self.assertTrue(any(path == "/api/sms/query" for _, path, _ in self.bridge.calls))

    def test_allowlist_disabled_cutpoint_dedupe_and_result_loop_filter(self):
        contact = self.contact()
        self.enable()
        base = int(time.time() * 1000)
        self.bridge.messages = [
            {"id": "1", "sender": contact["number"], "body": "old request", "receivedAt": base - 60000, "subscriptionId": 7},
            {"id": "2", "sender": contact["number"], "body": "DAXQO/RESULT completed", "receivedAt": base + 2000, "subscriptionId": 7},
            {"id": "3", "sender": "+8613800138001", "body": "not allowlisted", "receivedAt": base + 3000, "subscriptionId": 7},
            {"id": "4", "sender": contact["number"], "body": "do the task", "receivedAt": base + 4000, "subscriptionId": 7},
        ]
        self.sms.poll_once()
        self.sms.poll_once()
        self.assertEqual([row["prompt"] for row in self.harness.started], ["do the task"])
        rows = self.harness.store.db.execute("SELECT native_id,status FROM sms_commands ORDER BY native_id").fetchall()
        self.assertEqual([(row[0], row[1]) for row in rows], [("1", "ignored"), ("2", "ignored"), ("4", "started")])
        self.assertEqual(self.harness.store.db.execute("SELECT after_id FROM sms_cursors WHERE subscription_id=7").fetchone()[0], 4)
        query = next(call for call in self.bridge.calls if call[1] == "/api/sms/query")
        self.assertEqual(query[2]["senders"], [contact["number"]])

    def test_disable_and_contact_revocation_stop_pending_dispatch(self):
        contact = self.contact(); self.enable()
        now_ms = int(time.time() * 1000) + 2000
        self.sms._ingest(7, {"id": "9", "sender": contact["number"], "body": "run", "receivedAt": now_ms, "subscriptionId": 7},
                         [{"id": contact["id"], "number": contact["number"], "enabled_since": contact["createdAt"]}], self.sms._config, self.sms._generation)
        self.sms.set_settings({"enabled": False, "subscriptionId": 7, "replyEnabled": True})
        self.sms._dispatch_pending(self.sms._config, self.sms._generation)
        self.assertEqual(self.harness.started, [])
        # If it is encountered after a later re-enable, the new activation cutpoint
        # makes it permanently ineligible.
        self.harness.store.db.execute("UPDATE sms_commands SET received_at=? WHERE native_id='9'", (server.now(),))
        self.enable()
        self.sms._dispatch_pending(self.sms._config, self.sms._generation)
        self.assertEqual(self.harness.started, [])
        row = self.harness.store.db.execute("SELECT status FROM sms_commands WHERE native_id='9'").fetchone()
        self.assertEqual(row[0], "ignored")

    def test_uk_and_unknown_country_subscriptions_are_rejected(self):
        for country in ("gb", "", "us"):
            with self.subTest(country=country):
                self.bridge.country = country
                with self.assertRaises(ValueError):
                    self.sms.set_settings({"enabled": True, "subscriptionId": 7, "replyEnabled": True})
        self.assertFalse(any(path == "/api/sms/config" and body and body.get("enabled") for _, path, body in self.bridge.calls))

    def test_queued_messages_never_resume_after_disable_contact_revocation_or_sim_change(self):
        contact = self.contact(); self.enable()
        def pending(ident):
            self.sms._ingest(7, {"id": ident, "sender": contact["number"], "body": "queued task",
                "receivedAt": int(time.time() * 1000) + 60_000, "subscriptionId": 7},
                [{"id": contact["id"], "number": contact["number"], "enabled_since": contact["createdAt"]}],
                self.sms._config, self.sms._generation)
        pending("50")
        self.sms.set_settings({"enabled": False, "subscriptionId": 7, "replyEnabled": True})
        self.enable()
        self.sms._dispatch_pending(self.sms._config, self.sms._generation)
        pending("51")
        self.sms.contact_action(contact["id"], "toggle", {"enabled": False})
        self.sms.contact_action(contact["id"], "toggle", {"enabled": True})
        self.sms._dispatch_pending(self.sms._config, self.sms._generation)
        pending("52")
        self.bridge.subscriptions = [7, 8]; self.bridge.selected = 8
        self.sms.set_settings({"enabled": True, "subscriptionId": 8, "replyEnabled": True})
        self.sms._dispatch_pending(self.sms._config, self.sms._generation)
        self.assertEqual(self.harness.started, [])
        rows = self.harness.store.db.execute("SELECT status,body FROM sms_commands ORDER BY native_id").fetchall()
        self.assertEqual([tuple(row) for row in rows], [("ignored", None)] * 3)

    def test_no_enabled_contacts_or_disabled_feature_never_queries_message_content(self):
        self.sms.set_settings({"enabled": False, "subscriptionId": None, "replyEnabled": True})
        self.assertIn(("POST", "/api/sms/config", {"enabled": False}), self.bridge.calls)
        self.enable()
        self.sms.poll_once()
        self.assertFalse(any(path == "/api/sms/query" for _, path, _ in self.bridge.calls))
        self.sms.set_settings({"enabled": False, "subscriptionId": 7, "replyEnabled": True})
        self.bridge.calls.clear()
        self.sms.poll_once()
        self.assertEqual(self.bridge.calls, [])

    def _started_command(self, sender="+8613800138000", *, reply_status="not_due"):
        contact = self.contact(sender)
        stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        task_id = "task-fixture"
        with self.harness.store.db:
            self.harness.store.db.execute("INSERT INTO tasks(id,title,prompt,status,created_at,updated_at,result) VALUES(?,?,?,?,?,?,?)",
                (task_id, "fixture", "fixture", "completed", stamp, stamp, "完成了电话里的任务，拨打 10086"))
            self.harness.store.db.execute("INSERT INTO sms_commands(id,subscription_id,native_id,contact_id,sender,received_at,status,task_id,reply_status,replied_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("cmd-fixture", 7, "21", contact["id"], sender, stamp, "started", task_id, reply_status, stamp if reply_status == "queued" else None))
        return contact

    def test_terminal_reply_is_short_fixed_to_original_registered_sender(self):
        self.enable(); contact = self._started_command()
        self.sms._poll_reply_statuses(self.sms._config, {}, self.sms._generation)
        sends = [call for call in self.bridge.calls if call[1] == "/api/sms/send"]
        self.assertEqual(len(sends), 1)
        payload = sends[0][2]
        self.assertEqual(payload["to"], contact["number"])
        self.assertTrue(payload["text"].startswith("DAXQO/RESULT "))
        self.assertLessEqual(len(payload["text"].encode("utf-16-le")) // 2, 70)
        self.assertIn("10086", payload["text"])
        self.assertEqual(_short_result("x" * 500)[:14], "DAXQO/RESULT x")

    def test_uncertain_result_send_is_never_retried(self):
        self.enable(); self._started_command()
        self.bridge.send_error = True
        self.sms._poll_reply_statuses(self.sms._config, {}, self.sms._generation)
        self.bridge.send_error = False
        self.sms._poll_reply_statuses(self.sms._config, {}, self.sms._generation)
        self.assertEqual(len([call for call in self.bridge.calls if call[1] == "/api/sms/send"]), 1)
        row = self.harness.store.db.execute("SELECT reply_status FROM sms_commands WHERE id='cmd-fixture'").fetchone()
        self.assertEqual(row[0], "unknown")

    def test_old_task_never_replies_through_a_newly_selected_sim(self):
        self.enable(); self._started_command()
        self.bridge.subscriptions = [7, 8]
        self.bridge.selected = 8
        self.sms.set_settings({"enabled": True, "subscriptionId": 8, "replyEnabled": True})
        self.sms._poll_reply_statuses(self.sms._config, {}, self.sms._generation)
        self.assertFalse(any(path == "/api/sms/send" for _, path, _ in self.bridge.calls))
        row = self.harness.store.db.execute("SELECT reply_status FROM sms_commands WHERE id='cmd-fixture'").fetchone()
        self.assertEqual(row[0], "suppressed")

    def test_async_send_status_must_match_the_persisted_native_id(self):
        self.enable(); self._started_command(reply_status="queued")
        self.harness.store.db.execute("UPDATE sms_commands SET reply_id='expected-send' WHERE id='cmd-fixture'")
        self.sms._poll_reply_statuses(self.sms._config, {}, self.sms._generation)
        self.assertFalse(any(path == "/api/sms/send" for _, path, _ in self.bridge.calls))
        row = self.harness.store.db.execute("SELECT reply_status FROM sms_commands WHERE id='cmd-fixture'").fetchone()
        self.assertEqual(row[0], "unknown")

    def test_unconfirmed_vendor_send_times_out_without_resending(self):
        self.enable(); self._started_command(reply_status="queued")
        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=6)).isoformat().replace("+00:00", "Z")
        self.harness.store.db.execute("UPDATE sms_commands SET reply_id='send-1',replied_at=? WHERE id='cmd-fixture'", (old,))
        self.sms._poll_reply_statuses(self.sms._config, {}, self.sms._generation)
        self.sms._poll_reply_statuses(self.sms._config, {}, self.sms._generation)
        self.assertFalse(any(path == "/api/sms/send" or path.startswith("/api/sms/sends/") for _, path, _ in self.bridge.calls))
        row = self.harness.store.db.execute("SELECT reply_status,replied_at FROM sms_commands WHERE id='cmd-fixture'").fetchone()
        self.assertEqual(tuple(row), ("unknown", old))

    def test_restart_converts_inflight_task_and_send_to_uncertain_without_replay(self):
        contact = self.contact(); stamp = server.now()
        with self.harness.store.db:
            self.harness.store.db.execute("INSERT INTO sms_commands(id,subscription_id,native_id,contact_id,sender,received_at,status,reply_status) VALUES(?,?,?,?,?,?,?,?)",
                ("cmd-inflight", 7, "22", contact["id"], contact["number"], stamp, "dispatching", "sending"))
        restarted = SmsControlService(self.harness); restarted.bridge = self.bridge
        self.assertEqual(tuple(self.harness.store.db.execute("SELECT status,reply_status FROM sms_commands WHERE id='cmd-inflight'").fetchone()), ("uncertain", "unknown"))
        restarted.close()


if __name__ == "__main__": unittest.main()
