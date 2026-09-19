"""Home shortcuts (the Overview launchpad): the parse/validate door and the
/api/shortcuts endpoint the launchpad talks to.

The value round-trips to every browser that opens this hub and is rendered as
an <a href>, so the URL scheme check is a security test, not a nicety.
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_PATH", ":memory:")
import app  # noqa: E402
from backend.shortcuts import parse_shortcuts, validate_shortcuts  # noqa: E402

ONE = {"name": "Immich", "url": "http://ardi:2283", "icon": "photo",
       "group": "Media", "host": "", "container": "immich_server"}


class TestParseShortcuts(unittest.TestCase):
    def test_blank_is_empty_not_error(self):
        for raw in ("", "   ", None):
            self.assertEqual(parse_shortcuts(raw), ([], None))

    def test_valid_entry_round_trips(self):
        out, err = parse_shortcuts(json.dumps([ONE]))
        self.assertIsNone(err)
        self.assertEqual(out, [ONE])

    def test_optional_fields_default_to_empty(self):
        out, err = parse_shortcuts('[{"name":"Router","url":"https://192.168.1.1"}]')
        self.assertIsNone(err)
        self.assertEqual(out[0], {"name": "Router", "url": "https://192.168.1.1",
                                  "icon": "", "group": "", "host": "", "container": ""})

    def test_javascript_url_rejected(self):
        # Stored XSS: the tile is an <a href> every viewer clicks by design.
        for bad in ("javascript:alert(1)", "data:text/html,<script>x</script>",
                    "JavaScript:alert(1)", "/relative", "ftp://box/x", 'http://a" onmouseover="x'):
            out, err = parse_shortcuts(json.dumps([dict(ONE, url=bad)]))
            self.assertIsNone(out, bad)
            self.assertIn("http", err)

    def test_unknown_icon_rejected(self):
        out, err = parse_shortcuts(json.dumps([dict(ONE, icon="skull")]))
        self.assertIsNone(out)
        self.assertIn("icon", err)

    def test_name_required_and_capped(self):
        self.assertIsNone(parse_shortcuts(json.dumps([dict(ONE, name="  ")]))[0])
        self.assertIsNone(parse_shortcuts(json.dumps([dict(ONE, name="x" * 41)]))[0])

    def test_duplicate_pin_rejected(self):
        out, err = parse_shortcuts(json.dumps([ONE, dict(ONE, group="Other")]))
        self.assertIsNone(out)
        self.assertIn("twice", err)

    def test_oversized_list_rejected(self):
        many = [dict(ONE, name=f"n{i}", url=f"http://h/{i}") for i in range(41)]
        self.assertIsNone(parse_shortcuts(json.dumps(many))[0])

    def test_not_json_and_not_a_list(self):
        self.assertIsNone(parse_shortcuts("nope")[0])
        self.assertIsNone(parse_shortcuts('{"name":"x"}')[0])
        self.assertIsNone(parse_shortcuts('["x"]')[0])

    def test_long_group_host_container_are_trimmed_not_rejected(self):
        out, err = parse_shortcuts(json.dumps([dict(ONE, group="g" * 60, host="h" * 60,
                                                    container="c" * 200)]))
        self.assertIsNone(err)
        self.assertEqual(len(out[0]["group"]), 24)
        self.assertEqual(len(out[0]["host"]), 40)
        self.assertEqual(len(out[0]["container"]), 120)

    def test_validate_wraps_the_reason(self):
        self.assertIsNone(validate_shortcuts(json.dumps([ONE])))
        self.assertIn("Home shortcuts:", validate_shortcuts('[{"name":"x","url":"javascript:1"}]'))


class TestShortcutsEndpoint(unittest.TestCase):
    def setUp(self):
        self.c = app.app.test_client()
        self._saved = app.get_settings().get("home_shortcuts")

    def tearDown(self):
        app.save_settings({"home_shortcuts": self._saved or ""})

    def test_post_then_get_round_trip(self):
        r = self.c.post("/api/shortcuts", json={"shortcuts": [ONE]})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(self.c.get("/api/shortcuts").get_json()["shortcuts"], [ONE])

    def test_post_rejects_a_bad_url_with_400(self):
        r = self.c.post("/api/shortcuts", json={"shortcuts": [dict(ONE, url="javascript:1")]})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.get_json()["ok"])

    def test_post_replaces_the_whole_list(self):
        self.c.post("/api/shortcuts", json={"shortcuts": [ONE]})
        self.c.post("/api/shortcuts", json={"shortcuts": []})
        self.assertEqual(self.c.get("/api/shortcuts").get_json()["shortcuts"], [])

    def test_a_corrupt_stored_value_reads_as_empty_not_a_500(self):
        app.save_settings({"home_shortcuts": "not json at all"})
        r = self.c.get("/api/shortcuts")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["shortcuts"], [])

    def test_settings_post_validates_the_same_way(self):
        r = self.c.post("/api/settings", json={"home_shortcuts": '[{"name":"x","url":"nope"}]'})
        self.assertEqual(r.status_code, 400)
        self.assertIn("Home shortcuts", r.get_json()["error"])


if __name__ == "__main__":
    unittest.main()
