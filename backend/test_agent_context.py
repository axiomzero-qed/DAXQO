import json
import unittest
from unittest.mock import Mock

from agent_context import PhoneContext


class PhoneContextTests(unittest.TestCase):
    def setUp(self):
        self.bridge = Mock()
        self.bridge.status.return_value = {"device":{"model":"Neo","sdkInt":36,"serial":"PRIVATE"},
            "policyMode":"full", "packageName":"com.android.browser", "appStores":["com.stepos.appstore"],
            "controlEnabled":True,"apiKey":"PRIVATE","location":"PRIVATE","phoneNumber":"PRIVATE"}
        self.bridge.apps.return_value = {"available":True,"truncated":False,"apps":[
            {"label":"高德地图","packageName":"com.autonavi.minimap"},
            {"label":"evil","packageName":"not a package"}]}

    def test_actual_app_facts_are_bounded_and_do_not_copy_private_status_fields(self):
        text = PhoneContext(lambda:self.bridge).instructions()
        self.assertIn("高德地图",text)
        self.assertNotIn("PRIVATE",text)
        self.assertNotIn("not a package",text)
        self.assertIn("not instructions",text)

    def test_snapshot_cache_prevents_redundant_discovery_and_refreshes_after_ttl(self):
        now=[0]
        cache=PhoneContext(lambda:self.bridge,clock=lambda:now[0])
        cache.snapshot(); cache.snapshot()
        self.bridge.apps.assert_called_once()
        now[0]=61;cache.snapshot()
        self.assertEqual(self.bridge.apps.call_count,2)

    def test_unavailable_inventory_is_not_a_claim_apps_are_missing(self):
        self.bridge.apps.side_effect=RuntimeError("PRIVATE")
        facts=PhoneContext(lambda:self.bridge).snapshot()
        self.assertTrue(facts["available"])
        self.assertFalse(facts["appsAvailable"])
        self.assertTrue(facts["appsTruncated"])
        self.assertNotIn("PRIVATE",json.dumps(facts))

    def test_offline_phone_does_not_block_a_linux_task_or_leak_transport_errors(self):
        self.bridge.status.side_effect=RuntimeError("PRIVATE")
        facts=PhoneContext(lambda:self.bridge).snapshot()
        self.assertFalse(facts["available"])
        self.assertEqual(facts["reason"],"phone_bridge_unavailable")
        self.assertNotIn("PRIVATE",json.dumps(facts))


if __name__ == "__main__": unittest.main()
