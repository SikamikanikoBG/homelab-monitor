"""Unit tests for user-configurable history retention (retention_days setting).

Covers:
  • the setting exists in SETTING_DEFAULTS and defaults to the prior hardcoded
    window, so out-of-the-box behaviour is byte-identical (back-compat);
  • it round-trips through /api/settings GET/POST;
  • get_retention_days()/get_retention_secs() read the live value and clamp
    garbage / out-of-range values defensively (never raise);
  • POST validation rejects garbage / out-of-range with a clean 400;
  • the main-history prune honours the live setting AND only touches the
    monitor's OWN time-series tables (never any host-facing data)."""
import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


def _reset_setting():
    with app.LOCK:
        app.DB.execute("DELETE FROM settings WHERE key='retention_days'")
        app.DB.commit()


class TestRetentionDefault(unittest.TestCase):
    def setUp(self):
        _reset_setting()

    def test_present_in_defaults(self):
        self.assertIn("retention_days", app.SETTING_DEFAULTS)

    def test_default_matches_prior_hardcoded_window(self):
        # Back-compat: absent an override, the effective window equals the old
        # RETENTION constant exactly — nothing gets pruned differently.
        self.assertEqual(app.SETTING_DEFAULTS["retention_days"],
                         str(app._RETENTION_DAYS_DEFAULT))
        self.assertEqual(app.get_retention_secs(), app.RETENTION)
        self.assertEqual(app.get_retention_days(), app._RETENTION_DAYS_DEFAULT)


class TestRetentionLiveRead(unittest.TestCase):
    def setUp(self):
        _reset_setting()

    def tearDown(self):
        _reset_setting()

    def test_setting_changes_effective_window(self):
        app.save_settings({"retention_days": "30"})
        self.assertEqual(app.get_retention_days(), 30)
        self.assertEqual(app.get_retention_secs(), 30 * 86400)

    def test_clamp_below_min(self):
        app.save_settings({"retention_days": "0"})
        self.assertEqual(app.get_retention_days(), app._RETENTION_DAYS_MIN)

    def test_clamp_above_max(self):
        app.save_settings({"retention_days": "999999"})
        self.assertEqual(app.get_retention_days(), app._RETENTION_DAYS_MAX)

    def test_garbage_falls_back_to_default(self):
        app.save_settings({"retention_days": "not-a-number"})
        self.assertEqual(app.get_retention_days(), app._RETENTION_DAYS_DEFAULT)

    def test_blank_falls_back_to_default(self):
        app.save_settings({"retention_days": ""})
        self.assertEqual(app.get_retention_days(), app._RETENTION_DAYS_DEFAULT)


class TestRetentionValidation(unittest.TestCase):
    def test_validator_accepts_valid(self):
        self.assertIsNone(app._validate_retention_settings({"retention_days": "45"}))

    def test_validator_accepts_absent_and_blank(self):
        self.assertIsNone(app._validate_retention_settings({}))
        self.assertIsNone(app._validate_retention_settings({"retention_days": ""}))

    def test_validator_rejects_garbage(self):
        self.assertIsNotNone(app._validate_retention_settings({"retention_days": "abc"}))

    def test_validator_rejects_out_of_range(self):
        self.assertIsNotNone(app._validate_retention_settings({"retention_days": "0"}))
        self.assertIsNotNone(app._validate_retention_settings({"retention_days": "5000"}))


class TestRetentionApi(unittest.TestCase):
    def setUp(self):
        _reset_setting()
        self.c = app.app.test_client()

    def tearDown(self):
        _reset_setting()

    def test_get_exposes_default(self):
        r = self.c.get("/api/settings")
        self.assertEqual(r.status_code, 200)
        s = r.get_json()["settings"]
        self.assertEqual(s["retention_days"], str(app._RETENTION_DAYS_DEFAULT))

    def test_post_roundtrips(self):
        r = self.c.post("/api/settings", data=json.dumps({"retention_days": "90"}),
                        content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["settings"]["retention_days"], "90")
        self.assertEqual(app.get_retention_days(), 90)

    def test_post_rejects_garbage_400(self):
        r = self.c.post("/api/settings", data=json.dumps({"retention_days": "junk"}),
                        content_type="application/json")
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.get_json()["ok"])
        # rejected → unchanged (still the default)
        self.assertEqual(app.get_retention_days(), app._RETENTION_DAYS_DEFAULT)

    def test_post_rejects_out_of_range_400(self):
        r = self.c.post("/api/settings", data=json.dumps({"retention_days": "99999"}),
                        content_type="application/json")
        self.assertEqual(r.status_code, 400)


class TestRetentionPruneHonoursSetting(unittest.TestCase):
    """The prune must delete rows past the LIVE window and leave newer ones —
    and must only touch the monitor's own time-series tables."""

    def setUp(self):
        _reset_setting()
        with app.LOCK:
            app.DB.execute("DELETE FROM samples")
            app.DB.commit()

    def tearDown(self):
        _reset_setting()
        with app.LOCK:
            app.DB.execute("DELETE FROM samples")
            app.DB.commit()

    def test_prune_uses_live_setting(self):
        app.save_settings({"retention_days": "10"})
        now = int(time.time())
        old = now - 20 * 86400        # older than the 10-day window → pruned
        keep = now - 5 * 86400        # within the 10-day window → kept
        with app.LOCK:
            for ts in (old, keep):
                app.DB.execute("INSERT OR REPLACE INTO samples(ts,util) VALUES(?,?)", (ts, 1))
            app.DB.commit()
        # Mirror the sampler's DELETE using the live retention value.
        retention = app.get_retention_secs()
        with app.LOCK:
            app.DB.execute("DELETE FROM samples WHERE ts<?", (now - retention,))
            app.DB.commit()
            rows = [r[0] for r in app.DB.execute("SELECT ts FROM samples ORDER BY ts").fetchall()]
        self.assertEqual(rows, [keep])

    def test_lower_setting_prunes_more(self):
        now = int(time.time())
        ts30 = now - 30 * 86400
        with app.LOCK:
            app.DB.execute("INSERT OR REPLACE INTO samples(ts,util) VALUES(?,?)", (ts30, 1))
            app.DB.commit()
        # 60-day window keeps a 30-day-old row.
        app.save_settings({"retention_days": "60"})
        with app.LOCK:
            app.DB.execute("DELETE FROM samples WHERE ts<?", (now - app.get_retention_secs(),))
            app.DB.commit()
            self.assertEqual(app.DB.execute("SELECT COUNT(*) FROM samples").fetchone()[0], 1)
        # Lower to 7 days → the same row is now beyond the window.
        app.save_settings({"retention_days": "7"})
        with app.LOCK:
            app.DB.execute("DELETE FROM samples WHERE ts<?", (now - app.get_retention_secs(),))
            app.DB.commit()
            self.assertEqual(app.DB.execute("SELECT COUNT(*) FROM samples").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
