"""Unit tests for the Daily Brief (#170): the render contract (html/summary/subject),
the all-green vs. needs-attention headline + Action-needed block, graceful section
degradation, the settings round-trip, the schedule (_brief_due) once-per-day logic,
and the preview/test API contract. All delivery is mocked — NO real outbound in CI."""
import os
import sys
import time
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


def _clean():
    with app.LOCK:
        app.DB.execute("DELETE FROM hosts")
        app.DB.execute("DELETE FROM uptime_checks")
        app.DB.execute("DELETE FROM events")
        app.DB.execute("DELETE FROM settings")
        app.DB.commit()
    app._BRIEF_LAST_SENT["date"] = None


def _healthy_state(disk_pct=40):
    """A green fleet snapshot in app.LATEST / app.HEALTH."""
    app.LATEST.update({
        "util": 0, "mem_used": 500, "mem_total": 24576, "power": 40, "temp": 41,
        "gpu_avail": True, "gpus": [{"name": "Test GPU"}], "models": [],
        "host": {"hostname": "testhub", "cpu": 2, "ram_used": 1000, "ram_total": 8000,
                 "disks": [{"mount": "/", "pct": disk_pct}]},
    })
    app.HEALTH.update({
        "docker": {"available": True, "summary": {"total": 3, "running": 3, "problems": 0}, "containers": []},
        "systemd": {"available": True, "summary": {"running": 10, "failed": 0}, "services": []},
        "at": int(time.time()),
    })


class TestRenderContract(unittest.TestCase):
    def setUp(self):
        _clean()
        _healthy_state()

    def test_returns_html_summary_subject_level(self):
        html, summary, subject, level = app.render_brief("dark")
        self.assertTrue(html.lstrip().startswith("<!DOCTYPE html>"))
        self.assertIn("Daily brief", html)
        self.assertIn("testhub", subject)
        self.assertIsInstance(summary, str)
        self.assertEqual(level, "info")          # all-green → info severity

    def test_no_emoji_anywhere(self):
        """Arsen's hard requirement: no 'infant icons' in either render."""
        _healthy_state(disk_pct=95)              # force an action line too
        html, summary, _, _ = app.render_brief("dark")
        for blob in (html, summary):
            self.assertFalse(any(ord(ch) >= 0x1F000 for ch in blob),
                             "brief must contain no emoji")

    def test_all_green_has_no_action_block(self):
        html, summary, subject, _ = app.render_brief("dark")
        self.assertIn("All systems healthy", html)
        self.assertIn("All systems healthy", subject)
        self.assertNotIn("ACTION NEEDED", html)
        self.assertNotIn("ACTION NEEDED", summary)

    def test_issue_surfaces_headline_and_action(self):
        _healthy_state(disk_pct=95)          # host card goes crit on disk
        html, summary, subject, level = app.render_brief("dark")
        self.assertIn("needs you", html)
        self.assertIn("ACTION NEEDED", html.upper())
        self.assertIn("ACTION NEEDED", summary)
        self.assertNotIn("All systems healthy", subject)
        self.assertEqual(level, "critical")      # disk crit → critical severity

    def test_offline_registered_host_counts_down_not_up(self):
        """A registered host with no live poll data is offline — and must NOT be
        counted in the 'hosts up' tally (the 5/5-while-one-down bug)."""
        with app.LOCK:
            app.DB.execute("INSERT INTO hosts(name, ssh_target, tags, added_at, last_check_at, last_check_json) "
                           "VALUES(?,?,?,?,?,?)",
                           ("edge", "u@edge", "", int(time.time()), int(time.time()),
                            '{"summary": {"overall": "ok"}}'))   # stale-OK Test result
            app.DB.commit()
        with app.HOST_DATA_LOCK:
            app.HOST_DATA.pop("edge", None)      # never successfully polled → offline
        fleet = app._brief_fleet()
        self.assertEqual(fleet["total"], 2)      # hub + edge
        self.assertEqual(fleet["up"], 1)         # only the hub — NOT 2/2
        html, summary, subject, _ = app.render_brief("dark")
        self.assertIn("edge", html)
        self.assertIn("offline", html.lower())

    def test_events_section_appears_when_present(self):
        with app.LOCK:
            app.DB.execute("INSERT INTO events(ts, service, kind, detail) VALUES(?,?,?,?)",
                           (int(time.time()) - 300, "immich", "oom", "killed worker"))
            app.DB.commit()
        html, _, _, _ = app.render_brief("dark")
        self.assertIn("Alerts", html)
        self.assertIn("immich", html)

    def test_light_theme_uses_light_palette(self):
        html, _, _, _ = app.render_brief("light")
        self.assertIn(app._BRIEF_PALETTE["light"]["bg"], html)

    def test_no_dead_fragment_link(self):
        """Email clients strip/neutralise href="#"; the brief must not ship one."""
        html, _, _, _ = app.render_brief("dark")
        self.assertNotIn('href="#"', html)


class TestSchedule(unittest.TestCase):
    def setUp(self):
        _clean()
        _healthy_state()

    def _enable(self):
        app.save_settings({"brief_enabled": "1", "brief_channel": "email", "email_host": "smtp.x",
                           "email_from": "a@x", "email_to": "b@x"})

    def test_due_fires_at_configured_time_once_per_day(self):
        now = time.time()
        hhmm = time.strftime("%H:%M", time.localtime(now))
        self._enable()
        app.save_settings({"brief_time": hhmm})
        due = app._brief_due(now)
        self.assertIsNotNone(due)
        self.assertEqual(due[1], "email")
        # mark sent today → suppressed
        app._BRIEF_LAST_SENT["date"] = due[2]
        self.assertIsNone(app._brief_due(now))

    def test_not_due_when_disabled_or_unconfigured(self):
        now = time.time()
        hhmm = time.strftime("%H:%M", time.localtime(now))
        app.save_settings({"brief_enabled": "0", "brief_time": hhmm})
        self.assertIsNone(app._brief_due(now))
        # enabled but channel not configured
        app.save_settings({"brief_enabled": "1", "brief_channel": "discord", "brief_time": hhmm})
        self.assertIsNone(app._brief_due(now))

    def test_channel_ready(self):
        s = {"email_host": "h", "email_from": "a@x", "email_to": "b@x"}
        self.assertTrue(app._brief_channel_ready(s, "email"))
        self.assertFalse(app._brief_channel_ready({}, "email"))
        self.assertTrue(app._brief_channel_ready({"discord_webhook_url": "u"}, "discord"))

    def test_run_once_claims_day_before_send_no_duplicate(self):
        """A transient send failure must still claim the day so the worker can't
        re-fire in the same minute and deliver a duplicate."""
        now = time.time()
        hhmm = time.strftime("%H:%M", time.localtime(now))
        self._enable()
        app.save_settings({"brief_time": hhmm})
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        with patch.object(app, "send_brief", MagicMock(side_effect=RuntimeError("smtp down"))) as m:
            self.assertTrue(app._brief_run_once(now))     # due → attempted
            self.assertEqual(app._BRIEF_LAST_SENT["date"], today)  # claimed despite failure
            self.assertFalse(app._brief_run_once(now))    # same minute → not due again
        m.assert_called_once()                            # exactly one delivery attempt


class TestApi(unittest.TestCase):
    def setUp(self):
        _clean()
        _healthy_state()
        self.c = app.app.test_client()

    def test_preview_returns_html(self):
        r = self.c.get("/api/brief/preview")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/html", r.headers.get("Content-Type", ""))
        self.assertIn(b"Daily brief", r.data)

    def test_test_requires_configured_channel(self):
        r = self.c.post("/api/brief/test", json={"channel": "email"})
        self.assertEqual(r.status_code, 400)   # email not configured

    def test_test_delivers_when_configured(self):
        app.save_settings({"email_host": "smtp.x", "email_from": "a@x", "email_to": "b@x"})
        with patch.object(app, "_send_brief_email", MagicMock()) as m:
            r = self.c.post("/api/brief/test", json={"channel": "email"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])
        m.assert_called_once()

    def test_unknown_channel_rejected(self):
        """brief_channel is enum-validated server-side, so a crafted value can never
        reach the store (or the dashboard's <option>). Closes the stored-XSS surface."""
        r = self.c.post("/api/settings", json={"brief_channel": 'discord" onmouseover="x'})
        self.assertEqual(r.status_code, 400)
        r2 = self.c.post("/api/settings", json={"brief_theme": "neon"})
        self.assertEqual(r2.status_code, 400)
        r3 = self.c.post("/api/settings", json={"brief_time": "9am"})
        self.assertEqual(r3.status_code, 400)

    def test_brief_settings_round_trip(self):
        r = self.c.post("/api/settings", json={"brief_enabled": "1", "brief_time": "07:30",
                                               "brief_channel": "ntfy", "brief_theme": "light"})
        self.assertEqual(r.status_code, 200)
        s = self.c.get("/api/settings").get_json()["settings"]
        self.assertEqual(s["brief_enabled"], "1")
        self.assertEqual(s["brief_time"], "07:30")
        self.assertEqual(s["brief_channel"], "ntfy")
        self.assertEqual(s["brief_theme"], "light")


if __name__ == "__main__":
    unittest.main()
