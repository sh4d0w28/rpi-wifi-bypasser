import unittest
from unittest import mock

from rpi_ap_tools.system import expressvpn


class ExpressVpnTests(unittest.TestCase):
    def test_group_single_region_country(self):
        grouped = expressvpn._group_for_region("vietnam")
        self.assertEqual(grouped["country_key"], "vietnam")
        self.assertEqual(grouped["country_label"], "Vietnam")
        self.assertEqual(grouped["region_label"], "Vietnam")

    def test_group_multi_region_country(self):
        grouped = expressvpn._group_for_region("usa-new-york")
        self.assertEqual(grouped["country_key"], "usa")
        self.assertEqual(grouped["country_label"], "USA")
        self.assertEqual(grouped["region_label"], "New York")

    def test_group_hong_kong_variant(self):
        grouped = expressvpn._group_for_region("hong-kong-2")
        self.assertEqual(grouped["country_key"], "hong-kong")
        self.assertEqual(grouped["country_label"], "Hong Kong")
        self.assertEqual(grouped["region_label"], "Hong Kong 2")

    @mock.patch("rpi_ap_tools.system.expressvpn.list_regions")
    def test_list_country_groups(self, list_regions):
        list_regions.return_value = {
            "ok": True,
            "regions": ["smart", "vietnam", "usa-new-york", "usa-seattle", "hong-kong-2"],
            "message": "",
        }
        result = expressvpn.list_country_groups()
        self.assertTrue(result["ok"])
        self.assertEqual([item["key"] for item in result["countries"]], ["smart", "hong-kong", "usa", "vietnam"])
        usa = next(item for item in result["countries"] if item["key"] == "usa")
        self.assertEqual([item["id"] for item in usa["regions"]], ["usa-new-york", "usa-seattle"])

    @mock.patch("rpi_ap_tools.system.expressvpn._run_command")
    @mock.patch("rpi_ap_tools.system.expressvpn.expressvpn_available")
    def test_get_status_summary_disconnected(self, available, run_command):
        available.return_value = True
        run_command.side_effect = [
            {"ok": True, "stdout": "Disconnected\n\nLocation: Smart (hong-kong-2)\nNetwork Lock: enabled when connected\nSplit Tunnel: disabled\n", "stderr": "", "returncode": 0},
            {"ok": True, "stdout": "Disconnected\n", "stderr": "", "returncode": 0},
            {"ok": True, "stdout": "smart\n", "stderr": "", "returncode": 0},
            {"ok": True, "stdout": "125.235.239.38\n", "stderr": "", "returncode": 0},
            {"ok": True, "stdout": "Unknown\n", "stderr": "", "returncode": 0},
        ]
        result = expressvpn.get_status_summary()
        self.assertEqual(result["connection_state"], "Disconnected")
        self.assertEqual(result["selected_region"], "smart")
        self.assertEqual(result["selected_country"], "Smart")
        self.assertEqual(result["public_ip"], "125.235.239.38")
        self.assertEqual(result["vpn_ip"], "-")
        self.assertEqual(result["network_lock_summary"], "enabled when connected")


if __name__ == "__main__":
    unittest.main()
