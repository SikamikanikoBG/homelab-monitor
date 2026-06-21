"""Tests for the Incidents layer: correlated-anomaly grouping into one lifecycled
incident, the open→extend→clear lifecycle with clear-debounce, the /api/incidents
shape, the compact summary embedded on /api/forecast, recovery ("cleared")
notifications firing exactly once and only when a channel is configured, the
no-rules / no-channel no-op, and no-secret-leak in the API payloads."""
import os
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


def _clean():
    with app.LOCK:
        app.DB.execute("DELETE FROM incidents")
        app.DB.execute("DELETE FROM incident_members")
        app.DB.execute("DELETE FROM alert_rules")
        app.DB.execute("DELETE FROM alert_history")
        app.DB.commit()


def _bundle(*series):
    """Build an anomalies bundle like _zscore_anomalies returns. Each arg is a
    (key, z, direction) tuple or a key string (defaults z=4.0 spike)."""
    items = []
    for s in series:
        if isinstance(s, str):
            key, z, direction = s, 4.0, "spike"
        else:
            key, z, direction = s
        items.append({"key": key, "unit": "W", "value": 300.0, "baseline": 200.0,
                      "z": z, "stddev": 25.0, "direction": direction,
                      "magnitude": 100.0, "samples": 40})
    items.sort(key=lambda a: abs(a["z"]), reverse=True)
    return {"status": "quiet" if not items else "quiet", "checked": 5,
            "threshold": 3.0, "window_h": 6, "items": items}


class TestIncidentGrouping(unittest.TestCase):
    def setUp(self):
        _clean()

    def test_simultaneous_anomalies_group_into_one(self):
        t = 1_000_000
        app.evaluate_incidents(_bundle("gpu_util", "gpu_power", "gpu_temp"), t)
        inc = app.list_incidents()
        self.assertEqual(len(inc), 1)                       # ONE incident, not three
        self.assertEqual(inc[0]["state"], "open")
        self.assertEqual(inc[0]["member_count"], 3)
        self.assertEqual(inc[0]["active_count"], 3)
        keys = {m["series"] for m in inc[0]["members"]}
        self.assertEqual(keys, {"gpu_util", "gpu_power", "gpu_temp"})

    def test_no_anomalies_opens_nothing(self):
        self.assertIsNone(app.evaluate_incidents(_bundle(), 1_000_000))
        self.assertEqual(app.list_incidents(), [])

    def test_severity_critical_when_broad(self):
        app.evaluate_incidents(_bundle("gpu_util", "gpu_power", "gpu_temp"), 1)
        self.assertEqual(app.list_incidents()[0]["severity"], "critical")  # >=3 series

    def test_severity_critical_when_extreme_single(self):
        app.evaluate_incidents(_bundle(("gpu_power", 7.0, "spike")), 1)
        self.assertEqual(app.list_incidents()[0]["severity"], "critical")  # |z|>=6

    def test_severity_warning_when_mild_single(self):
        app.evaluate_incidents(_bundle(("gpu_power", 3.5, "spike")), 1)
        self.assertEqual(app.list_incidents()[0]["severity"], "warning")


class TestIncidentLifecycle(unittest.TestCase):
    def setUp(self):
        _clean()

    def test_open_extend_clear_with_debounce(self):
        t = 1_000_000
        # open with one series
        app.evaluate_incidents(_bundle("gpu_power"), t)
        iid = app.list_incidents()[0]["id"]
        # a correlated series joins → SAME incident extends, not a new one
        app.evaluate_incidents(_bundle("gpu_power", "gpu_temp"), t + 20)
        inc = app.list_incidents()
        self.assertEqual(len(inc), 1)
        self.assertEqual(inc[0]["id"], iid)
        self.assertEqual(inc[0]["member_count"], 2)
        # one normal pass: debounce — must NOT clear yet (flap protection)
        app.evaluate_incidents(_bundle(), t + 40)
        self.assertEqual(app.list_incidents()[0]["state"], "open")
        # anomaly returns within the window → debounce resets, still one incident
        app.evaluate_incidents(_bundle("gpu_power"), t + 60)
        self.assertEqual(app.list_incidents()[0]["state"], "open")
        # now sustained normal for CLEAR_CONFIRM passes → clears
        for k in range(app._INCIDENT_CLEAR_CONFIRM):
            app.evaluate_incidents(_bundle(), t + 80 + k * 20)
        inc = app.list_incidents()
        self.assertEqual(inc[0]["state"], "cleared")
        self.assertIsNotNone(inc[0]["cleared_at"])
        self.assertEqual(inc[0]["id"], iid)

    def test_new_incident_after_clear(self):
        t = 1_000_000
        app.evaluate_incidents(_bundle("gpu_power"), t)
        first = app.list_incidents()[0]["id"]
        for k in range(app._INCIDENT_CLEAR_CONFIRM):
            app.evaluate_incidents(_bundle(), t + 20 + k * 20)
        # fresh anomaly after clear → a brand-new incident, not reopening the old one
        app.evaluate_incidents(_bundle("gpu_temp"), t + 200)
        inc = app.list_incidents()
        self.assertEqual(len(inc), 2)
        self.assertEqual(inc[0]["state"], "open")       # open first
        self.assertNotEqual(inc[0]["id"], first)

    def test_peak_z_tracked_across_passes(self):
        t = 1_000_000
        app.evaluate_incidents(_bundle(("gpu_power", 4.0, "spike")), t)
        app.evaluate_incidents(_bundle(("gpu_power", 8.0, "spike")), t + 20)
        app.evaluate_incidents(_bundle(("gpu_power", 3.5, "spike")), t + 40)
        m = app.list_incidents()[0]["members"][0]
        self.assertAlmostEqual(m["peak_z"], 8.0)        # peak retained


class TestIncidentApi(unittest.TestCase):
    def setUp(self):
        _clean()
        self.c = app.app.test_client()

    def test_api_shape_and_200(self):
        app.evaluate_incidents(_bundle("gpu_util", "gpu_power"), 1_000_000)
        r = self.c.get("/api/incidents")
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertIn("incidents", j)
        self.assertIn("summary", j)
        self.assertEqual(j["summary"]["open"], 1)
        inc = j["incidents"][0]
        for k in ("id", "state", "severity", "opened_at", "members", "member_count"):
            self.assertIn(k, inc)
        self.assertEqual(inc["state"], "open")

    def test_api_empty_is_200(self):
        r = self.c.get("/api/incidents")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["incidents"], [])
        self.assertEqual(r.get_json()["summary"], {"open": 0, "top": None})

    def test_forecast_embeds_summary(self):
        app.evaluate_incidents(_bundle("gpu_power"), 1_000_000)
        r = self.c.get("/api/forecast")
        self.assertEqual(r.status_code, 200)
        self.assertIn("incidents", r.get_json())
        self.assertEqual(r.get_json()["incidents"]["open"], 1)

    def test_no_secret_leak(self):
        # configure secrets, then ensure the incidents payload never echoes them
        app.save_settings({"discord_webhook_url": "https://discord.test/SECRET",
                           "ntfy_topic": "topsecret-topic", "telegram_token": "TG-SECRET"})
        try:
            app.evaluate_incidents(_bundle("gpu_power", "gpu_temp"), 1_000_000)
            body = self.c.get("/api/incidents").get_data(as_text=True)
            for needle in ("SECRET", "topsecret-topic", "TG-SECRET", "discord.test"):
                self.assertNotIn(needle, body)
        finally:
            app.save_settings({"discord_webhook_url": "", "ntfy_topic": "", "telegram_token": ""})


class TestIncidentDetailApi(unittest.TestCase):
    def setUp(self):
        _clean()
        self.c = app.app.test_client()

    def tearDown(self):
        _clean()

    def test_detail_shape_and_200(self):
        app.evaluate_incidents(_bundle("gpu_util", "gpu_power", "gpu_temp"), 1_000_000)
        iid = app.list_incidents()[0]["id"]
        r = self.c.get("/api/incidents/" + iid)
        self.assertEqual(r.status_code, 200)
        inc = r.get_json()["incident"]
        for k in ("id", "state", "severity", "opened_at", "updated_at", "members",
                  "member_count", "active_count", "timeline"):
            self.assertIn(k, inc)
        self.assertEqual(inc["id"], iid)
        self.assertEqual(inc["member_count"], 3)
        # full member detail present
        m = inc["members"][0]
        for k in ("series", "direction", "peak_z", "unit", "peak_value", "baseline",
                  "first_seen", "last_seen", "active"):
            self.assertIn(k, m)
        # timeline: an 'opened' event + one 'member_joined' per member
        events = [e["event"] for e in inc["timeline"]]
        self.assertEqual(events[0], "opened")
        self.assertEqual(events.count("member_joined"), 3)

    def test_detail_timeline_includes_cleared(self):
        t = 1_000_000
        app.evaluate_incidents(_bundle("gpu_power"), t)
        iid = app.list_incidents()[0]["id"]
        for k in range(app._INCIDENT_CLEAR_CONFIRM):
            app.evaluate_incidents(_bundle(), t + 20 + k * 20)
        inc = self.c.get("/api/incidents/" + iid).get_json()["incident"]
        self.assertEqual(inc["state"], "cleared")
        self.assertIn("cleared", [e["event"] for e in inc["timeline"]])

    def test_detail_404_unknown(self):
        r = self.c.get("/api/incidents/nope-not-real")
        self.assertEqual(r.status_code, 404)
        self.assertIn("error", r.get_json())          # clean JSON, not a stacktrace

    def test_detail_404_garbage(self):
        r = self.c.get("/api/incidents/" + "x" * 200)
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.get_json().get("error"), "unknown incident")

    def test_detail_no_secret_leak(self):
        app.save_settings({"discord_webhook_url": "https://discord.test/SECRET",
                           "ntfy_topic": "topsecret-topic", "telegram_token": "TG-SECRET"})
        try:
            app.evaluate_incidents(_bundle("gpu_power", "gpu_temp"), 1_000_000)
            iid = app.list_incidents()[0]["id"]
            body = self.c.get("/api/incidents/" + iid).get_data(as_text=True)
            for needle in ("SECRET", "topsecret-topic", "TG-SECRET", "discord.test"):
                self.assertNotIn(needle, body)
        finally:
            app.save_settings({"discord_webhook_url": "", "ntfy_topic": "", "telegram_token": ""})


class TestIncidentRule(unittest.TestCase):
    """The incident-aware alert rule type: ONE notification for the whole correlated
    event (vs the human side's one-per-series), edge-triggered fire on open + one
    recovery on clear, threshold-gated, off-by-default, no double-send."""

    def setUp(self):
        _clean()
        app.save_settings({"discord_webhook_url": "", "telegram_token": "",
                           "telegram_chat_id": "", "webhook_url": "", "ntfy_topic": "hlm-test"})

    def tearDown(self):
        app.save_settings({"ntfy_topic": ""})
        _clean()

    def _sig(self):
        # Mirror the sampler: list_incidents() reflects whatever evaluate_incidents wrote.
        return {"anomalies": {"items": []}, "incidents": app.list_incidents()}

    def test_validate_rejects_bad_severity(self):
        rid, err = app.create_rule({"name": "i", "ctype": "incident",
                                    "params": {"severity": "bogus"}, "enabled": True})
        self.assertIsNone(rid)
        self.assertIn("severity", err)

    def test_fires_once_on_open_and_recovers_once_on_clear(self):
        app.create_rule({"name": "inc", "ctype": "incident",
                         "params": {"severity": "warning"}, "enabled": True, "cooldown_min": 0})
        t = 1_000_000
        with patch("app._post_text", return_value=(200, b"")) as pt:
            # incident opens with 3 correlated series → ONE fire
            app.evaluate_incidents(_bundle("gpu_util", "gpu_power", "gpu_temp"), t)
            app.evaluate_rules(self._sig())
            # still open next pass (cooldown 0 but edge already armed) → no re-fire
            app.evaluate_incidents(_bundle("gpu_util", "gpu_power", "gpu_temp"), t + 20)
            app.evaluate_rules(self._sig())
            # clear it
            for k in range(app._INCIDENT_CLEAR_CONFIRM):
                app.evaluate_incidents(_bundle(), t + 40 + k * 20)
            app.evaluate_rules(self._sig())      # recovery
            app.evaluate_rules(self._sig())      # already cleared → no double-send
        statuses = [h["status"] for h in app.list_alert_history()]
        self.assertEqual(statuses.count("sent"), 1)        # exactly one alarm
        self.assertEqual(statuses.count("recovered"), 1)   # exactly one recovery
        # the ONE alarm names the correlated series count
        sent = [h for h in app.list_alert_history() if h["status"] == "sent"][0]
        self.assertIn("3 correlated", sent["detail"])

    def test_threshold_gates_below_severity(self):
        # rule wants critical; a warning-only incident must NOT fire
        app.create_rule({"name": "inc", "ctype": "incident",
                         "params": {"severity": "critical"}, "enabled": True, "cooldown_min": 0})
        with patch("app._post_text", return_value=(200, b"")) as pt:
            app.evaluate_incidents(_bundle(("gpu_power", 3.5, "spike")), 1)   # mild single → warning
            self.assertEqual(app.list_incidents()[0]["severity"], "warning")
            self.assertEqual(app.evaluate_rules(self._sig()), 0)
        pt.assert_not_called()

    def test_fires_at_threshold_critical(self):
        app.create_rule({"name": "inc", "ctype": "incident",
                         "params": {"severity": "critical"}, "enabled": True, "cooldown_min": 0})
        with patch("app._post_text", return_value=(200, b"")) as pt:
            app.evaluate_incidents(_bundle("gpu_util", "gpu_power", "gpu_temp"), 1)  # broad → critical
            self.assertEqual(app.list_incidents()[0]["severity"], "critical")
            self.assertEqual(app.evaluate_rules(self._sig()), 1)
        self.assertEqual(pt.call_count, 1)

    def test_no_rule_no_op(self):
        with patch("app._post_text", return_value=(200, b"")) as pt:
            app.evaluate_incidents(_bundle("gpu_util", "gpu_power", "gpu_temp"), 1)
            self.assertEqual(app.evaluate_rules(self._sig()), 0)   # no rules → nothing
        pt.assert_not_called()

    def test_inert_without_channel(self):
        app.save_settings({"ntfy_topic": ""})                      # no channel
        app.create_rule({"name": "inc", "ctype": "incident",
                         "params": {"severity": "warning"}, "enabled": True, "cooldown_min": 0})
        with patch("app._post_text", return_value=(200, b"")) as pt:
            app.evaluate_incidents(_bundle("gpu_power", "gpu_temp"), 1)
            self.assertEqual(app.evaluate_rules(self._sig()), 0)
        pt.assert_not_called()

    def test_existing_anomaly_rule_unaffected(self):
        # an incident rule alongside an anomaly rule: both arm independently
        app.create_rule({"name": "anom", "ctype": "anomaly", "params": {"series": "any"},
                         "enabled": True, "cooldown_min": 0})
        sig = {"anomalies": {"items": [
            {"key": "gpu_power", "unit": "W", "value": 320.0, "baseline": 200.0,
             "z": 4.1, "direction": "spike"}]}, "incidents": []}
        with patch("app._post_text", return_value=(200, b"")) as pt:
            self.assertEqual(app.evaluate_rules(sig), 1)   # anomaly rule still fires
        self.assertEqual(pt.call_count, 1)


class TestRecoveryNotifications(unittest.TestCase):
    SIG_ON = {"anomalies": {"items": [
        {"key": "gpu_power", "unit": "W", "value": 320.0, "baseline": 200.0,
         "z": 4.1, "direction": "spike"}]}}
    SIG_OFF = {"anomalies": {"items": []}}

    def setUp(self):
        _clean()
        app.save_settings({"discord_webhook_url": "", "telegram_token": "",
                           "telegram_chat_id": "", "webhook_url": "", "ntfy_topic": "hlm-test"})

    def tearDown(self):
        app.save_settings({"ntfy_topic": ""})
        _clean()

    def test_recovery_fires_once_on_clear(self):
        app.create_rule({"name": "a", "ctype": "anomaly", "params": {"series": "any"},
                         "enabled": True, "cooldown_min": 0})
        with patch("app._post_text", return_value=(200, b"")) as pt:
            app.evaluate_rules(self.SIG_ON)     # fires alarm
            app.evaluate_rules(self.SIG_OFF)    # recovery
            app.evaluate_rules(self.SIG_OFF)    # already cleared -> no double-send
        # 2 sends total: 1 alarm + 1 recovery
        self.assertEqual(pt.call_count, 2)
        statuses = [h["status"] for h in app.list_alert_history()]
        self.assertIn("recovered", statuses)
        self.assertEqual(statuses.count("recovered"), 1)
        # recovery message is marked as cleared, not a fresh alarm
        rec = [h for h in app.list_alert_history() if h["status"] == "recovered"][0]
        self.assertIn("cleared", rec["title"])
        self.assertEqual(rec["level"], "info")

    def test_no_recovery_without_prior_fire(self):
        # condition never went active → clearing sends nothing
        app.create_rule({"name": "a", "ctype": "anomaly", "params": {"series": "any"},
                         "enabled": True, "cooldown_min": 0})
        with patch("app._post_text", return_value=(200, b"")) as pt:
            app.evaluate_rules(self.SIG_OFF)
            app.evaluate_rules(self.SIG_OFF)
        pt.assert_not_called()

    def test_recovery_inert_without_channel(self):
        app.save_settings({"ntfy_topic": ""})           # no channel configured
        app.create_rule({"name": "a", "ctype": "anomaly", "params": {"series": "any"},
                         "enabled": True, "cooldown_min": 0})
        with patch("app._post_text", return_value=(200, b"")) as pt:
            self.assertEqual(app.evaluate_rules(self.SIG_ON), 0)
            self.assertEqual(app.evaluate_rules(self.SIG_OFF), 0)
        pt.assert_not_called()

    def test_recovery_skipped_when_snoozed_never_fired(self):
        rid, _ = app.create_rule({"name": "a", "ctype": "anomaly", "params": {"series": "any"},
                                  "enabled": True, "cooldown_min": 0})
        app.update_rule(rid, {"snooze_min": 30})
        with patch("app._post_text", return_value=(200, b"")) as pt:
            app.evaluate_rules(self.SIG_ON)     # suppressed by snooze, never sent
            app.evaluate_rules(self.SIG_OFF)    # must NOT send a recovery
        pt.assert_not_called()


class TestIncidentsNoOpAndDemo(unittest.TestCase):
    def setUp(self):
        _clean()

    def test_no_anomalies_no_writes(self):
        app.evaluate_incidents(_bundle(), int(time.time()))
        with app.LOCK:
            n = app.DB.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]
        self.assertEqual(n, 0)

    def test_groups_demo_seeded_anomalies(self):
        # The demo seed lays down history engineered to trip ~4 anomalies at once.
        was = app.DEMO_MODE
        app.DEMO_MODE = True
        try:
            app._seed_demo_data()
            now = int(time.time())
            with app.LOCK:
                anoms = app._zscore_anomalies(app.DB.cursor(), now)
            if not (anoms.get("items")):
                self.skipTest("demo seed produced no anomalies in this environment")
            app.evaluate_incidents(anoms, now)
            inc = app.list_incidents()
            self.assertEqual(len(inc), 1)               # all folded into ONE incident
            self.assertEqual(inc[0]["member_count"], len(anoms["items"]))
        finally:
            app.DEMO_MODE = was


if __name__ == "__main__":
    unittest.main()
