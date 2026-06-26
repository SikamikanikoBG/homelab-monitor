"""Unit tests for the opt-in alerting rule engine: rule CRUD + validation, rule
evaluation (fires / doesn't), cooldown + dedupe, snooze, channel payload assembly
(HTTP mocked — nothing is actually POSTed), graceful behavior when a channel is
unset/unreachable, and alert history + ack."""
import os
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


def _clean_db():
    with app.LOCK:
        app.DB.execute("DELETE FROM alert_rules")
        app.DB.execute("DELETE FROM alert_history")
        app.DB.commit()


class TestRuleCrud(unittest.TestCase):
    def setUp(self):
        _clean_db()

    def test_create_validates_and_persists(self):
        rid, err = app.create_rule({"name": "vram", "ctype": "vram_eta",
                                    "params": {"days": 2}, "enabled": True})
        self.assertIsNone(err)
        rules = app.list_rules()
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["id"], rid)
        self.assertTrue(rules[0]["enabled"])
        self.assertEqual(rules[0]["params"]["days"], 2)

    def test_create_rejects_unknown_type(self):
        rid, err = app.create_rule({"name": "x", "ctype": "nope"})
        self.assertIsNone(rid)
        self.assertIn("condition type", err)

    def test_create_rejects_missing_name(self):
        _, err = app.create_rule({"ctype": "anomaly"})
        self.assertIn("name", err)

    def test_cost_budget_requires_number(self):
        _, err = app.create_rule({"name": "b", "ctype": "cost_budget", "params": {"budget": "abc"}})
        self.assertIn("budget", err)

    def test_anomaly_series_validated(self):
        _, err = app.create_rule({"name": "a", "ctype": "anomaly", "params": {"series": "bogus"}})
        self.assertIn("series", err)
        rid, err = app.create_rule({"name": "a", "ctype": "anomaly", "params": {"series": "gpu_util"}})
        self.assertIsNone(err)

    def test_update_toggle_and_delete(self):
        rid, _ = app.create_rule({"name": "a", "ctype": "anomaly", "params": {"series": "any"}})
        ok, err = app.update_rule(rid, {"enabled": True})
        self.assertTrue(ok)
        self.assertTrue(app.list_rules()[0]["enabled"])
        self.assertTrue(app.delete_rule(rid))
        self.assertEqual(app.list_rules(), [])

    def test_update_unknown_rule(self):
        ok, err = app.update_rule("nope", {"enabled": True})
        self.assertFalse(ok)
        self.assertEqual(err, "not found")

    def test_empty_patch_is_noop_not_disable(self):
        # An empty PATCH body must NOT silently disable a rule.
        rid, _ = app.create_rule({"name": "a", "ctype": "anomaly",
                                  "params": {"series": "any"}, "enabled": True})
        self.assertTrue(app.list_rules()[0]["enabled"])
        ok, err = app.update_rule(rid, {})
        self.assertFalse(ok)
        self.assertEqual(err, "empty update")
        # Still enabled — nothing was touched.
        self.assertTrue(app.list_rules()[0]["enabled"])

    def test_empty_patch_endpoint_400(self):
        rid, _ = app.create_rule({"name": "a", "ctype": "anomaly",
                                  "params": {"series": "any"}, "enabled": True})
        c = app.app.test_client()
        r = c.patch(f"/api/alerts/rules/{rid}", json={})
        self.assertEqual(r.status_code, 400)
        self.assertTrue(app.list_rules()[0]["enabled"])


SIG_ANOMALY = {"anomalies": {"items": [
    {"key": "gpu_power", "unit": "W", "value": 320.0, "baseline": 200.0,
     "z": 4.1, "direction": "spike"}]}}
SIG_QUIET = {"anomalies": {"items": []}}
SIG_DISK = {"disk": [{"mount": "/", "pct": 96, "gb_per_day": 5.0, "eta_days": 1.5}]}
SIG_VRAM = {"vram": {"status": "filling", "pct": 90, "mb_per_min": 50.0, "eta_min": 1440}}
SIG_COST = {"cost_month": {"enabled": True, "currency": "$",
                           "projected_month": 42.0, "month_to_date": 20.0}}


class TestRuleEvaluation(unittest.TestCase):
    def _rule(self, **kw):
        base = {"id": "r1", "name": "R", "enabled": True, "ctype": "anomaly",
                "params": {}, "channel": "all", "level": "warning",
                "cooldown_min": 60, "last_fired_at": None, "last_state": None,
                "snoozed_until": None}
        base.update(kw)
        return base

    def test_anomaly_fires(self):
        fired, title, _ = app._eval_rule(self._rule(ctype="anomaly", params={"series": "any"}), SIG_ANOMALY)
        self.assertTrue(fired)
        self.assertIn("gpu_power", title)

    def test_anomaly_series_filter_no_match(self):
        fired, *_ = app._eval_rule(self._rule(ctype="anomaly", params={"series": "gpu_temp"}), SIG_ANOMALY)
        self.assertFalse(fired)

    def test_anomaly_quiet_no_fire(self):
        fired, *_ = app._eval_rule(self._rule(ctype="anomaly", params={"series": "any"}), SIG_QUIET)
        self.assertFalse(fired)

    def test_disk_eta_fires_under_threshold(self):
        fired, title, _ = app._eval_rule(self._rule(ctype="disk_eta", params={"days": 7}), SIG_DISK)
        self.assertTrue(fired)
        self.assertIn("/", title)

    def test_disk_eta_no_fire_above_threshold(self):
        fired, *_ = app._eval_rule(self._rule(ctype="disk_eta", params={"days": 1}), SIG_DISK)
        self.assertFalse(fired)

    def test_vram_eta_fires(self):
        fired, *_ = app._eval_rule(self._rule(ctype="vram_eta", params={"days": 2}), SIG_VRAM)
        self.assertTrue(fired)

    def test_cost_budget_fires_over(self):
        fired, *_ = app._eval_rule(self._rule(ctype="cost_budget", params={"budget": 30}), SIG_COST)
        self.assertTrue(fired)

    def test_cost_budget_no_fire_under(self):
        fired, *_ = app._eval_rule(self._rule(ctype="cost_budget", params={"budget": 100}), SIG_COST)
        self.assertFalse(fired)

    def test_cost_budget_disabled_pricing(self):
        fired, *_ = app._eval_rule(self._rule(ctype="cost_budget", params={"budget": 1}),
                                   {"cost_month": {"enabled": False}})
        self.assertFalse(fired)


class TestEvaluateRulesCooldownAndChannels(unittest.TestCase):
    def setUp(self):
        _clean_db()
        # one configured channel: ntfy
        app.save_settings({"discord_webhook_url": "", "telegram_token": "",
                           "telegram_chat_id": "", "webhook_url": "", "ntfy_topic": "hlm-test"})

    def tearDown(self):
        app.save_settings({"ntfy_topic": ""})
        _clean_db()

    def test_no_enabled_rules_is_noop(self):
        with patch("app._post_text") as pt:
            self.assertEqual(app.evaluate_rules(SIG_ANOMALY), 0)
            pt.assert_not_called()

    def test_no_channel_configured_is_noop(self):
        app.save_settings({"ntfy_topic": ""})
        app.create_rule({"name": "a", "ctype": "anomaly", "params": {"series": "any"}, "enabled": True})
        with patch("app._post_text") as pt:
            self.assertEqual(app.evaluate_rules(SIG_ANOMALY), 0)
            pt.assert_not_called()

    def test_fires_once_then_cooldown_dedupes(self):
        app.create_rule({"name": "a", "ctype": "anomaly", "params": {"series": "any"},
                         "enabled": True, "cooldown_min": 60})
        with patch("app._post_text", return_value=(200, b"")) as pt:
            n1 = app.evaluate_rules(SIG_ANOMALY)   # fires
            n2 = app.evaluate_rules(SIG_ANOMALY)   # cooled-down -> deduped
        self.assertEqual(n1, 1)
        self.assertEqual(n2, 0)
        self.assertEqual(pt.call_count, 1)
        hist = app.list_alert_history()
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["status"], "sent")
        self.assertEqual(hist[0]["channel"], "ntfy")

    def test_cooldown_zero_refires(self):
        app.create_rule({"name": "a", "ctype": "anomaly", "params": {"series": "any"},
                         "enabled": True, "cooldown_min": 0})
        with patch("app._post_text", return_value=(200, b"")) as pt:
            self.assertEqual(app.evaluate_rules(SIG_ANOMALY), 1)
            self.assertEqual(app.evaluate_rules(SIG_ANOMALY), 1)
        self.assertEqual(pt.call_count, 2)

    def test_snooze_suppresses(self):
        rid, _ = app.create_rule({"name": "a", "ctype": "anomaly", "params": {"series": "any"},
                                  "enabled": True, "cooldown_min": 0})
        app.update_rule(rid, {"snooze_min": 30})
        with patch("app._post_text", return_value=(200, b"")) as pt:
            self.assertEqual(app.evaluate_rules(SIG_ANOMALY), 0)
            pt.assert_not_called()

    def test_unreachable_channel_records_error_not_raises(self):
        app.create_rule({"name": "a", "ctype": "anomaly", "params": {"series": "any"},
                         "enabled": True, "cooldown_min": 60})
        with patch("app._post_text", side_effect=OSError("connection refused")):
            n = app.evaluate_rules(SIG_ANOMALY)   # still "fired" (attempted), no raise
        self.assertEqual(n, 1)
        hist = app.list_alert_history()
        self.assertEqual(hist[0]["status"], "error")


class TestChannelPayloadAssembly(unittest.TestCase):
    def test_webhook_envelope(self):
        with patch("app._post_json", return_value=(200, b"{}")) as pj:
            app.send_webhook("https://example.test/hook", "warning", "T", "D")
        url, payload = pj.call_args[0]
        self.assertEqual(url, "https://example.test/hook")
        self.assertEqual(payload["source"], "homelab-monitor")
        self.assertEqual(payload["level"], "warning")
        self.assertEqual(payload["title"], "T")
        self.assertEqual(payload["detail"], "D")

    def test_discord_embed(self):
        with patch("app._post_json", return_value=(200, b"{}")) as pj:
            app.send_discord("https://discord.test/wh", "critical", "Boom", "details")
        _, payload = pj.call_args[0]
        self.assertEqual(payload["embeds"][0]["title"], "Boom")
        self.assertEqual(payload["embeds"][0]["description"], "details")

    def test_ntfy_headers(self):
        with patch("app._post_text", return_value=(200, b"")) as pt:
            app.send_ntfy("https://ntfy.sh", "topic", "warning", "Title", "Body")
        url, text = pt.call_args[0][0], pt.call_args[0][1]
        self.assertEqual(url, "https://ntfy.sh/topic")
        self.assertEqual(text, "Body")
        hdr = pt.call_args[0][2]
        self.assertEqual(hdr["Title"], "Title")

    def test_dispatch_single_channel_unconfigured_skips(self):
        s = {**app.SETTING_DEFAULTS, "webhook_url": ""}
        res = app.dispatch_alert(s, "info", "T", "D", channel="webhook")
        self.assertEqual(res, [("webhook", False, "not configured")])

    def test_dispatch_all_fans_out_only_configured(self):
        s = {**app.SETTING_DEFAULTS, "webhook_url": "https://e.test/h", "ntfy_topic": ""}
        with patch("app._post_json", return_value=(200, b"{}")):
            res = app.dispatch_alert(s, "info", "T", "D")  # channel='all'
        chans = {c for c, _, _ in res}
        self.assertEqual(chans, {"webhook"})


class TestAlertHistoryApiAndAck(unittest.TestCase):
    def setUp(self):
        _clean_db()
        self.c = app.app.test_client()

    def test_history_trimmed_to_cap(self):
        for i in range(app._ALERT_HISTORY_CAP + 25):
            app.record_alert("r", "n", "info", "ntfy", "sent", "t", "d")
        with app.LOCK:
            cnt = app.DB.execute("SELECT COUNT(*) FROM alert_history").fetchone()[0]
        self.assertEqual(cnt, app._ALERT_HISTORY_CAP)

    def test_ack_endpoint(self):
        app.record_alert("r", "n", "info", "ntfy", "sent", "t", "d")
        hid = app.list_alert_history()[0]["id"]
        r = self.c.post(f"/api/alerts/history/{hid}/ack")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(app.list_alert_history()[0]["acked"])

    def test_rules_api_roundtrip(self):
        r = self.c.post("/api/alerts/rules", json={"name": "d", "ctype": "disk_eta",
                                                   "params": {"days": 3}, "enabled": True})
        self.assertEqual(r.status_code, 201)
        rid = r.get_json()["id"]
        j = self.c.get("/api/alerts/rules").get_json()
        self.assertEqual(len(j["rules"]), 1)
        self.assertIn("anomaly", j["types"])
        self.assertEqual(self.c.delete(f"/api/alerts/rules/{rid}").status_code, 200)

    def test_channel_test_no_channel_400(self):
        app.save_settings({"discord_webhook_url": "", "ntfy_topic": "",
                           "telegram_token": "", "webhook_url": ""})
        r = self.c.post("/api/alerts/channels/test", json={"channel": "all"})
        self.assertEqual(r.status_code, 400)


def _clean_maint():
    with app.LOCK:
        app.DB.execute("DELETE FROM maintenance_windows")
        app.DB.commit()


class TestMaintenanceCrudAndValidation(unittest.TestCase):
    def setUp(self):
        _clean_maint()

    def tearDown(self):
        _clean_maint()

    def test_recurring_create_and_list(self):
        mid, err = app.create_maintenance({"label": "nightly", "recurring": True,
                                           "daily_start": "02:00", "daily_end": "02:30"})
        self.assertIsNone(err)
        ws = app.list_maintenance()
        self.assertEqual(len(ws), 1)
        self.assertTrue(ws[0]["recurring"])
        self.assertEqual(ws[0]["daily_start"], "02:00")
        self.assertEqual(ws[0]["daily_end"], "02:30")

    def test_recurring_overnight_wrap_allowed(self):
        mid, err = app.create_maintenance({"label": "ovn", "recurring": True,
                                           "daily_start": "23:00", "daily_end": "01:00"})
        self.assertIsNone(err)
        self.assertIsNotNone(mid)

    def test_oneoff_create(self):
        now = int(time.time())
        mid, err = app.create_maintenance({"label": "planned", "recurring": False,
                                           "start_ts": now, "end_ts": now + 3600})
        self.assertIsNone(err)
        self.assertIsNotNone(mid)

    def test_missing_label_rejected(self):
        _, err = app.create_maintenance({"recurring": True, "daily_start": "02:00", "daily_end": "03:00"})
        self.assertIn("label", err)

    def test_bad_hhmm_rejected(self):
        for bad in ("2400", "25:00", "12:60", "ab:cd", "9:5", "", None):
            _, err = app.create_maintenance({"label": "x", "recurring": True,
                                             "daily_start": bad, "daily_end": "03:00"})
            self.assertIsNotNone(err, f"expected reject for {bad!r}")

    def test_recurring_equal_times_rejected(self):
        _, err = app.create_maintenance({"label": "x", "recurring": True,
                                         "daily_start": "02:00", "daily_end": "02:00"})
        self.assertIn("same", err.lower())

    def test_oneoff_bad_range_rejected(self):
        now = int(time.time())
        _, err = app.create_maintenance({"label": "x", "recurring": False,
                                         "start_ts": now, "end_ts": now})
        self.assertIn("after", err)

    def test_oneoff_non_numeric_rejected(self):
        _, err = app.create_maintenance({"label": "x", "recurring": False,
                                         "start_ts": "soon", "end_ts": "later"})
        self.assertIsNotNone(err)

    def test_toggle_and_delete(self):
        mid, _ = app.create_maintenance({"label": "x", "recurring": True,
                                         "daily_start": "02:00", "daily_end": "03:00"})
        ok, err = app.update_maintenance(mid, {"enabled": False})
        self.assertTrue(ok)
        self.assertFalse(app.list_maintenance()[0]["enabled"])
        self.assertTrue(app.delete_maintenance(mid))
        self.assertEqual(app.list_maintenance(), [])

    def test_update_unknown_404(self):
        ok, err = app.update_maintenance("nope", {"enabled": True})
        self.assertFalse(ok)
        self.assertEqual(err, "not found")

    def test_api_roundtrip_always_200_clean_400(self):
        c = app.app.test_client()
        r = c.post("/api/alerts/maintenance", json={"label": "n", "recurring": True,
                                                    "daily_start": "02:00", "daily_end": "02:30"})
        self.assertEqual(r.status_code, 201)
        mid = r.get_json()["id"]
        j = c.get("/api/alerts/maintenance").get_json()
        self.assertEqual(len(j["windows"]), 1)
        self.assertIn("active", j)
        # bad create -> clean 400
        r = c.post("/api/alerts/maintenance", json={"label": "bad", "recurring": True,
                                                    "daily_start": "99:99", "daily_end": "02:30"})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.get_json()["ok"])
        self.assertEqual(c.delete(f"/api/alerts/maintenance/{mid}").status_code, 200)
        self.assertEqual(c.delete(f"/api/alerts/maintenance/{mid}").status_code, 404)


class TestInMaintenance(unittest.TestCase):
    def setUp(self):
        _clean_maint()

    def tearDown(self):
        _clean_maint()

    def _at(self, hh, mm):
        """An epoch whose localtime is hh:mm today (uses the test machine's TZ)."""
        lt = list(time.localtime())
        lt[3], lt[4], lt[5] = hh, mm, 0
        lt[8] = -1
        return int(time.mktime(time.struct_time(tuple(lt))))

    def test_no_windows_inactive(self):
        active, until = app._in_maintenance(int(time.time()))
        self.assertFalse(active)
        self.assertIsNone(until)

    def test_recurring_inside_window(self):
        app.create_maintenance({"label": "n", "recurring": True,
                                "daily_start": "02:00", "daily_end": "03:00"})
        active, until = app._in_maintenance(self._at(2, 30))
        self.assertTrue(active)
        self.assertIsNotNone(until)

    def test_recurring_outside_window(self):
        app.create_maintenance({"label": "n", "recurring": True,
                                "daily_start": "02:00", "daily_end": "03:00"})
        active, _ = app._in_maintenance(self._at(5, 0))
        self.assertFalse(active)

    def test_recurring_overnight_wrap_true_both_sides(self):
        app.create_maintenance({"label": "ovn", "recurring": True,
                                "daily_start": "23:00", "daily_end": "01:00"})
        self.assertTrue(app._in_maintenance(self._at(23, 30))[0])  # before midnight
        self.assertTrue(app._in_maintenance(self._at(0, 30))[0])   # after midnight
        self.assertFalse(app._in_maintenance(self._at(12, 0))[0])  # midday

    def test_disabled_window_inactive(self):
        mid, _ = app.create_maintenance({"label": "n", "recurring": True,
                                         "daily_start": "00:00", "daily_end": "23:59"})
        app.update_maintenance(mid, {"enabled": False})
        self.assertFalse(app._in_maintenance(int(time.time()))[0])

    def test_oneoff_active_and_outside(self):
        now = int(time.time())
        app.create_maintenance({"label": "p", "recurring": False,
                                "start_ts": now - 60, "end_ts": now + 60})
        self.assertTrue(app._in_maintenance(now)[0])
        self.assertFalse(app._in_maintenance(now + 600)[0])
        self.assertFalse(app._in_maintenance(now - 600)[0])


class TestMaintenanceMutesAlerting(unittest.TestCase):
    """Suppression + arm/disarm preservation. Channel send + maintenance state mocked."""
    def setUp(self):
        _clean_db()
        _clean_maint()
        app._MAINT_SUPPRESS_LOGGED.clear()
        app.save_settings({"discord_webhook_url": "", "telegram_token": "",
                           "telegram_chat_id": "", "webhook_url": "", "ntfy_topic": "hlm-test"})

    def tearDown(self):
        app.save_settings({"ntfy_topic": ""})
        _clean_db()
        _clean_maint()

    def _state(self, rid):
        with app.LOCK:
            return app.DB.execute("SELECT last_state FROM alert_rules WHERE id=?", (rid,)).fetchone()[0]

    def test_no_window_unchanged(self):
        app.create_rule({"name": "a", "ctype": "anomaly", "params": {"series": "any"},
                         "enabled": True, "cooldown_min": 60})
        with patch("app._post_text", return_value=(200, b"")) as pt:
            self.assertEqual(app.evaluate_rules(SIG_ANOMALY), 1)
        pt.assert_called()

    def test_fire_suppressed_during_window(self):
        rid, _ = app.create_rule({"name": "a", "ctype": "anomaly", "params": {"series": "any"},
                                  "enabled": True, "cooldown_min": 60})
        with patch("app._in_maintenance", return_value=(True, None)), \
             patch("app._post_text", return_value=(200, b"")) as pt:
            n = app.evaluate_rules(SIG_ANOMALY)
        self.assertEqual(n, 0)
        pt.assert_not_called()
        # NOT armed — last_state untouched so no spurious recovery is owed.
        self.assertIsNone(self._state(rid))
        hist = app.list_alert_history()
        self.assertEqual(hist[0]["status"], "suppressed")

    def test_alarm_starts_and_ends_inside_window_never_notifies(self):
        rid, _ = app.create_rule({"name": "a", "ctype": "anomaly", "params": {"series": "any"},
                                  "enabled": True, "cooldown_min": 0})
        with patch("app._in_maintenance", return_value=(True, None)), \
             patch("app._post_text", return_value=(200, b"")) as pt:
            app.evaluate_rules(SIG_ANOMALY)   # active inside window -> suppressed
            app.evaluate_rules(SIG_QUIET)     # cleared inside window -> no recovery
        pt.assert_not_called()
        self.assertIsNone(self._state(rid))
        # No 'sent' or 'recovered' rows — only the suppressed flag.
        statuses = {h["status"] for h in app.list_alert_history()}
        self.assertNotIn("sent", statuses)
        self.assertNotIn("recovered", statuses)

    def test_condition_still_active_when_window_ends_fires_after(self):
        rid, _ = app.create_rule({"name": "a", "ctype": "anomaly", "params": {"series": "any"},
                                  "enabled": True, "cooldown_min": 0})
        with patch("app._in_maintenance", return_value=(True, None)), \
             patch("app._post_text", return_value=(200, b"")) as pt:
            self.assertEqual(app.evaluate_rules(SIG_ANOMALY), 0)  # suppressed during window
        pt.assert_not_called()
        with patch("app._in_maintenance", return_value=(False, None)), \
             patch("app._post_text", return_value=(200, b"")) as pt:
            self.assertEqual(app.evaluate_rules(SIG_ANOMALY), 1)  # window ended -> fires
        pt.assert_called()
        self.assertEqual(self._state(rid), "active")

    def test_suppressed_history_logged_once_per_span_not_per_pass(self):
        # A long window with a persistently-active alarm must record ONE suppressed
        # row, not one per sampler pass (which would evict real alert history).
        app.create_rule({"name": "a", "ctype": "anomaly", "params": {"series": "any"},
                         "enabled": True, "cooldown_min": 0})
        with patch("app._in_maintenance", return_value=(True, None)), \
             patch("app._post_text", return_value=(200, b"")):
            for _ in range(5):
                app.evaluate_rules(SIG_ANOMALY)   # active, every pass, inside window
        sup = [h for h in app.list_alert_history() if h["status"] == "suppressed"]
        self.assertEqual(len(sup), 1)
        # New suppressed span after the alarm clears → logs once more.
        with patch("app._in_maintenance", return_value=(True, None)), \
             patch("app._post_text", return_value=(200, b"")):
            app.evaluate_rules(SIG_QUIET)         # clears → span ends, re-arms logging
            app.evaluate_rules(SIG_ANOMALY)       # active again → one new suppressed row
        sup = [h for h in app.list_alert_history() if h["status"] == "suppressed"]
        self.assertEqual(len(sup), 2)

    def test_recovery_deferred_until_window_ends_then_sends_once(self):
        rid, _ = app.create_rule({"name": "a", "ctype": "anomaly", "params": {"series": "any"},
                                  "enabled": True, "cooldown_min": 0})
        # Fire BEFORE maintenance (armed for recovery).
        with patch("app._in_maintenance", return_value=(False, None)), \
             patch("app._post_text", return_value=(200, b"")):
            app.evaluate_rules(SIG_ANOMALY)
        self.assertEqual(self._state(rid), "active")
        # Condition clears WHILE in maintenance: no recovery sent, stays armed.
        with patch("app._in_maintenance", return_value=(True, None)), \
             patch("app._post_text", return_value=(200, b"")) as pt:
            app.evaluate_rules(SIG_QUIET)
        pt.assert_not_called()
        self.assertEqual(self._state(rid), "active")
        # Window ends, condition still clear: ONE recovery now.
        with patch("app._in_maintenance", return_value=(False, None)), \
             patch("app._post_text", return_value=(200, b"")) as pt:
            app.evaluate_rules(SIG_QUIET)
        self.assertEqual(pt.call_count, 1)
        self.assertEqual(self._state(rid), "clear")
        self.assertTrue(any(h["status"] == "recovered" for h in app.list_alert_history()))


def _clean_uptime():
    with app.LOCK:
        app.DB.execute("DELETE FROM uptime_checks")
        app.DB.execute("DELETE FROM uptime_results")
        app.DB.commit()


# Uptime-state signal shapes (mirror uptime_overview().checks rows).
def SIG_UP(cid="c1", state="down", enabled=True, label="API", code=503, err="HTTP 503"):
    return {"uptime": [{"id": cid, "label": label, "enabled": enabled, "state": state,
                        "last_code": code, "last_err": err}]}


class TestUptimeDownValidation(unittest.TestCase):
    def setUp(self):
        _clean_db(); _clean_uptime()

    def tearDown(self):
        _clean_db(); _clean_uptime()

    def test_any_mode_default(self):
        clean, err = app._validate_rule({"name": "u", "ctype": "uptime_down", "params": {}})
        self.assertIsNone(err)
        self.assertEqual(clean["params"]["check_id"], "any")

    def test_any_literal(self):
        clean, err = app._validate_rule({"name": "u", "ctype": "uptime_down",
                                         "params": {"check_id": "any"}})
        self.assertIsNone(err)
        self.assertEqual(clean["params"]["check_id"], "any")

    def test_existing_check_id_ok(self):
        cid, _ = app.create_uptime_check({"label": "x", "type": "tcp", "target": "h:1"})
        clean, err = app._validate_rule({"name": "u", "ctype": "uptime_down",
                                         "params": {"check_id": cid}})
        self.assertIsNone(err)
        self.assertEqual(clean["params"]["check_id"], cid)

    def test_unknown_check_id_rejected(self):
        _, err = app._validate_rule({"name": "u", "ctype": "uptime_down",
                                     "params": {"check_id": "nope"}})
        self.assertIsNotNone(err)

    def test_garbage_check_id_rejected(self):
        _, err = app._validate_rule({"name": "u", "ctype": "uptime_down",
                                     "params": {"check_id": 123}})
        self.assertIsNotNone(err)


class TestUptimeDownEval(unittest.TestCase):
    def _rule(self, **kw):
        base = {"id": "r1", "name": "U", "enabled": True, "ctype": "uptime_down",
                "params": {"check_id": "any"}, "channel": "all", "level": "warning",
                "cooldown_min": 60, "last_fired_at": None, "last_state": None,
                "snoozed_until": None}
        base.update(kw)
        return base

    def test_targeted_down_fires(self):
        fired, title, detail = app._eval_rule(
            self._rule(params={"check_id": "c1"}), SIG_UP(cid="c1", state="down"))
        self.assertTrue(fired)
        self.assertIn("API", title)
        self.assertIn("503", detail)

    def test_targeted_up_no_fire(self):
        fired, *_ = app._eval_rule(
            self._rule(params={"check_id": "c1"}), SIG_UP(cid="c1", state="up"))
        self.assertFalse(fired)

    def test_targeted_unknown_no_fire(self):
        # check with no results yet -> state "unknown" -> must NOT alarm
        fired, *_ = app._eval_rule(
            self._rule(params={"check_id": "c1"}), SIG_UP(cid="c1", state="unknown"))
        self.assertFalse(fired)

    def test_targeted_missing_check_no_fire(self):
        fired, *_ = app._eval_rule(
            self._rule(params={"check_id": "gone"}), SIG_UP(cid="c1", state="down"))
        self.assertFalse(fired)

    def test_any_mode_fires_when_any_down(self):
        sig = {"uptime": [
            {"id": "a", "label": "A", "enabled": True, "state": "up"},
            {"id": "b", "label": "B", "enabled": True, "state": "down", "last_code": 500}]}
        fired, title, detail = app._eval_rule(self._rule(), sig)
        self.assertTrue(fired)
        self.assertIn("B", detail)

    def test_any_mode_quiet_when_all_up(self):
        sig = {"uptime": [{"id": "a", "label": "A", "enabled": True, "state": "up"}]}
        fired, *_ = app._eval_rule(self._rule(), sig)
        self.assertFalse(fired)

    def test_any_mode_ignores_disabled_check(self):
        sig = {"uptime": [{"id": "a", "label": "A", "enabled": False, "state": "down"}]}
        fired, *_ = app._eval_rule(self._rule(), sig)
        self.assertFalse(fired)

    def test_detail_redacts_creds_in_error(self):
        sig = SIG_UP(cid="c1", state="down", code=None,
                     err="connect to https://user:secret@host failed")
        fired, _, detail = app._eval_rule(self._rule(params={"check_id": "c1"}), sig)
        self.assertTrue(fired)
        self.assertNotIn("secret", detail)


class TestUptimeDownEngineIntegration(unittest.TestCase):
    """Reuses the shared engine: cooldown, recovery edge, maintenance suppression.
    Channel send + maintenance state mocked; uptime state supplied via signals."""
    def setUp(self):
        # _clean_maint(): the no-args evaluate_rules() path reads maintenance state
        # from the real DB; a maintenance window left enabled by a prior test (whose
        # recurring/one-off span happens to cover the current wall-clock) would put
        # the engine in the suppression branch and make a down check return 0 fires
        # instead of 1 — an intermittent, time-of-day-correlated cross-test leak.
        _clean_db(); _clean_uptime(); _clean_maint()
        app._MAINT_SUPPRESS_LOGGED.clear()
        app.save_settings({"discord_webhook_url": "", "telegram_token": "",
                           "telegram_chat_id": "", "webhook_url": "", "ntfy_topic": "hlm-test"})
        # a real check so check_id validation in create_rule passes
        self.cid, _ = app.create_uptime_check({"label": "API", "type": "tcp", "target": "h:1"})

    def tearDown(self):
        app.save_settings({"ntfy_topic": ""})
        _clean_db(); _clean_uptime(); _clean_maint()

    def _state(self, rid):
        with app.LOCK:
            return app.DB.execute("SELECT last_state FROM alert_rules WHERE id=?", (rid,)).fetchone()[0]

    def test_off_when_no_channel(self):
        app.save_settings({"ntfy_topic": ""})
        app.create_rule({"name": "u", "ctype": "uptime_down", "params": {"check_id": "any"},
                         "enabled": True, "cooldown_min": 60})
        with patch("app._post_text") as pt:
            self.assertEqual(app.evaluate_rules(SIG_UP(cid=self.cid, state="down")), 0)
        pt.assert_not_called()

    def test_fires_on_down(self):
        app.create_rule({"name": "u", "ctype": "uptime_down", "params": {"check_id": self.cid},
                         "enabled": True, "cooldown_min": 60})
        with patch("app._post_text", return_value=(200, b"")) as pt:
            self.assertEqual(app.evaluate_rules(SIG_UP(cid=self.cid, state="down")), 1)
        pt.assert_called()

    def test_no_fire_when_up_or_unknown(self):
        app.create_rule({"name": "u", "ctype": "uptime_down", "params": {"check_id": self.cid},
                         "enabled": True, "cooldown_min": 60})
        with patch("app._post_text", return_value=(200, b"")) as pt:
            self.assertEqual(app.evaluate_rules(SIG_UP(cid=self.cid, state="unknown")), 0)
            self.assertEqual(app.evaluate_rules(SIG_UP(cid=self.cid, state="up")), 0)
        pt.assert_not_called()

    def test_recovery_sent_once_on_back_up(self):
        rid, _ = app.create_rule({"name": "u", "ctype": "uptime_down",
                                  "params": {"check_id": self.cid}, "enabled": True, "cooldown_min": 0})
        with patch("app._post_text", return_value=(200, b"")):
            app.evaluate_rules(SIG_UP(cid=self.cid, state="down"))   # fire
        self.assertEqual(self._state(rid), "active")
        with patch("app._post_text", return_value=(200, b"")) as pt:
            app.evaluate_rules(SIG_UP(cid=self.cid, state="up"))     # recovery
        self.assertEqual(pt.call_count, 1)
        self.assertEqual(self._state(rid), "clear")
        self.assertTrue(any(h["status"] == "recovered" for h in app.list_alert_history()))

    def test_suppressed_during_maintenance(self):
        rid, _ = app.create_rule({"name": "u", "ctype": "uptime_down",
                                  "params": {"check_id": self.cid}, "enabled": True, "cooldown_min": 60})
        with patch("app._in_maintenance", return_value=(True, None)), \
             patch("app._post_text", return_value=(200, b"")) as pt:
            self.assertEqual(app.evaluate_rules(SIG_UP(cid=self.cid, state="down")), 0)
        pt.assert_not_called()
        self.assertIsNone(self._state(rid))
        self.assertEqual(app.list_alert_history()[0]["status"], "suppressed")

    def test_fires_from_real_db_without_prebuilt_bundle(self):
        """Regression: the collector calls evaluate_rules with a PRE-BUILT bundle that
        (historically) omitted the 'uptime' key, so uptime_down rules silently never
        fired in production even though the unit tests — which always hand-built a bundle
        WITH 'uptime' — passed. The engine must back-fill per-check states from the DB
        when an uptime_down rule is enabled but the bundle lacks 'uptime'."""
        app.create_rule({"name": "u", "ctype": "uptime_down",
                         "params": {"check_id": self.cid}, "enabled": True, "cooldown_min": 0})
        ts = int(time.time())
        with app.LOCK:
            app.DB.execute("INSERT INTO uptime_results(check_id,ts,up,latency_ms,code,err) "
                           "VALUES(?,?,?,?,?,?)", (self.cid, ts, 0, None, 503, "HTTP 503"))
            app.DB.commit()
        # (a) No-args path: engine self-sources the whole bundle including uptime.
        with patch("app._post_text", return_value=(200, b"")) as pt:
            self.assertEqual(app.evaluate_rules(), 1)
        pt.assert_called()
        # (b) Collector-shaped bundle WITHOUT 'uptime' (mirrors collector's sig dict):
        #     the back-fill must still let the down check fire (cooldown=0 so it re-fires).
        collector_sig = {"anomalies": {"items": []}, "incidents": []}
        self.assertNotIn("uptime", collector_sig)
        with patch("app._post_text", return_value=(200, b"")) as pt:
            self.assertEqual(app.evaluate_rules(collector_sig), 1)
        pt.assert_called()


# ── cert_expiry: WARNs while a cert check is up-but-inside its pre-expiry window ──
def SIG_CERT(cid="cc1", state="up", enabled=True, label="example.com:443",
             cert_warn=True, days=12, warn_days=30, subject_cn="example.com"):
    """A cert-type check row as uptime_overview().checks would emit it."""
    return {"uptime": [{"id": cid, "label": label, "type": "cert", "enabled": enabled,
                        "state": state, "cert_warn": cert_warn, "days_to_expiry": days,
                        "cert_warn_days": warn_days, "subject_cn": subject_cn,
                        "last_code": None, "last_err": None}]}


class TestCertExpiryValidation(unittest.TestCase):
    def setUp(self):
        _clean_db(); _clean_uptime()

    def tearDown(self):
        _clean_db(); _clean_uptime()

    def test_any_mode_default(self):
        clean, err = app._validate_rule({"name": "c", "ctype": "cert_expiry", "params": {}})
        self.assertIsNone(err)
        self.assertEqual(clean["params"]["check_id"], "any")

    def test_existing_cert_check_ok(self):
        cid, _ = app.create_uptime_check({"label": "x", "type": "cert", "target": "example.com:443"})
        clean, err = app._validate_rule({"name": "c", "ctype": "cert_expiry",
                                         "params": {"check_id": cid}})
        self.assertIsNone(err)
        self.assertEqual(clean["params"]["check_id"], cid)

    def test_non_cert_check_id_rejected(self):
        cid, _ = app.create_uptime_check({"label": "x", "type": "tcp", "target": "h:1"})
        _, err = app._validate_rule({"name": "c", "ctype": "cert_expiry",
                                     "params": {"check_id": cid}})
        self.assertIsNotNone(err)

    def test_unknown_check_id_rejected(self):
        _, err = app._validate_rule({"name": "c", "ctype": "cert_expiry",
                                     "params": {"check_id": "nope"}})
        self.assertIsNotNone(err)

    def test_garbage_check_id_rejected(self):
        _, err = app._validate_rule({"name": "c", "ctype": "cert_expiry",
                                     "params": {"check_id": 123}})
        self.assertIsNotNone(err)


class TestCertExpiryEval(unittest.TestCase):
    def _rule(self, **kw):
        base = {"id": "r1", "name": "C", "enabled": True, "ctype": "cert_expiry",
                "params": {"check_id": "any"}, "channel": "all", "level": "warning",
                "cooldown_min": 60, "last_fired_at": None, "last_state": None,
                "snoozed_until": None}
        base.update(kw)
        return base

    def test_fires_in_warn_window(self):
        fired, title, detail = app._eval_rule(
            self._rule(params={"check_id": "cc1"}), SIG_CERT(cid="cc1", days=12))
        self.assertTrue(fired)
        self.assertIn("example.com", title)
        self.assertIn("12d", detail)
        self.assertIn("30d", detail)  # warn threshold surfaced

    def test_no_fire_outside_window(self):
        # days_to_expiry > warn_days -> uptime_overview sets cert_warn False -> no fire
        fired, *_ = app._eval_rule(
            self._rule(params={"check_id": "cc1"}), SIG_CERT(cid="cc1", cert_warn=False, days=90))
        self.assertFalse(fired)

    def test_no_double_fire_when_hard_down(self):
        # A hard-expired / unreachable cert reads state 'down' (uptime_down's job).
        # cert_expiry requires state 'up', so it stays quiet — no double notification.
        fired, *_ = app._eval_rule(
            self._rule(params={"check_id": "cc1"}),
            SIG_CERT(cid="cc1", state="down", cert_warn=False))
        self.assertFalse(fired)

    def test_targeted_missing_check_no_fire(self):
        fired, *_ = app._eval_rule(
            self._rule(params={"check_id": "gone"}), SIG_CERT(cid="cc1"))
        self.assertFalse(fired)

    def test_any_mode_ignores_non_cert_check(self):
        # An http/tcp check in the bundle must never satisfy a cert_expiry rule.
        sig = {"uptime": [{"id": "u1", "label": "API", "type": "tcp", "enabled": True,
                           "state": "up", "cert_warn": True}]}
        fired, *_ = app._eval_rule(self._rule(), sig)
        self.assertFalse(fired)

    def test_any_mode_ignores_disabled_check(self):
        fired, *_ = app._eval_rule(self._rule(), SIG_CERT(enabled=False))
        self.assertFalse(fired)

    def test_any_mode_lists_multiple_soonest_first(self):
        sig = {"uptime": [
            {"id": "a", "label": "a.com", "type": "cert", "enabled": True, "state": "up",
             "cert_warn": True, "days_to_expiry": 20, "cert_warn_days": 30},
            {"id": "b", "label": "b.com", "type": "cert", "enabled": True, "state": "up",
             "cert_warn": True, "days_to_expiry": 3, "cert_warn_days": 30}]}
        fired, title, detail = app._eval_rule(self._rule(), sig)
        self.assertTrue(fired)
        self.assertIn("2", title)            # count of 2
        self.assertIn("b.com", detail)       # soonest named
        self.assertIn("3d", detail)

    def test_detail_is_credential_safe(self):
        # Detail surfaces only label/day counts/subject CN — never the raw target.
        sig = SIG_CERT(cid="cc1", label="prod-api", subject_cn="prod-api.internal")
        fired, _, detail = app._eval_rule(self._rule(params={"check_id": "cc1"}), sig)
        self.assertTrue(fired)
        self.assertNotIn("secret", detail.lower())
        self.assertNotIn("://", detail)


class TestCertExpiryEngineIntegration(unittest.TestCase):
    """Shared engine: cooldown, recovery edge, maintenance, and the uptime back-fill."""
    def setUp(self):
        _clean_db(); _clean_uptime(); _clean_maint()
        app._MAINT_SUPPRESS_LOGGED.clear()
        app.save_settings({"discord_webhook_url": "", "telegram_token": "",
                           "telegram_chat_id": "", "webhook_url": "", "ntfy_topic": "hlm-test"})
        self.cid, _ = app.create_uptime_check({"label": "example.com:443", "type": "cert",
                                               "target": "example.com:443"})

    def tearDown(self):
        app.save_settings({"ntfy_topic": ""})
        _clean_db(); _clean_uptime(); _clean_maint()

    def _state(self, rid):
        with app.LOCK:
            return app.DB.execute("SELECT last_state FROM alert_rules WHERE id=?", (rid,)).fetchone()[0]

    def test_fires_in_window(self):
        app.create_rule({"name": "c", "ctype": "cert_expiry", "params": {"check_id": self.cid},
                         "enabled": True, "cooldown_min": 60})
        with patch("app._post_text", return_value=(200, b"")) as pt:
            self.assertEqual(app.evaluate_rules(SIG_CERT(cid=self.cid, days=10)), 1)
        pt.assert_called()

    def test_no_fire_outside_window(self):
        app.create_rule({"name": "c", "ctype": "cert_expiry", "params": {"check_id": self.cid},
                         "enabled": True, "cooldown_min": 60})
        with patch("app._post_text", return_value=(200, b"")) as pt:
            self.assertEqual(app.evaluate_rules(SIG_CERT(cid=self.cid, cert_warn=False, days=90)), 0)
        pt.assert_not_called()

    def test_recovery_on_renew(self):
        # Renew cert -> days back above warn -> cert_warn False -> rule clears -> ONE recovery.
        rid, _ = app.create_rule({"name": "c", "ctype": "cert_expiry",
                                  "params": {"check_id": self.cid}, "enabled": True, "cooldown_min": 0})
        with patch("app._post_text", return_value=(200, b"")):
            app.evaluate_rules(SIG_CERT(cid=self.cid, days=5))   # fire
        self.assertEqual(self._state(rid), "active")
        with patch("app._post_text", return_value=(200, b"")) as pt:
            app.evaluate_rules(SIG_CERT(cid=self.cid, cert_warn=False, days=365))  # renewed
        self.assertEqual(pt.call_count, 1)
        self.assertEqual(self._state(rid), "clear")
        self.assertTrue(any(h["status"] == "recovered" for h in app.list_alert_history()))

    def test_suppressed_during_maintenance(self):
        rid, _ = app.create_rule({"name": "c", "ctype": "cert_expiry",
                                  "params": {"check_id": self.cid}, "enabled": True, "cooldown_min": 60})
        with patch("app._in_maintenance", return_value=(True, None)), \
             patch("app._post_text", return_value=(200, b"")) as pt:
            self.assertEqual(app.evaluate_rules(SIG_CERT(cid=self.cid, days=5)), 0)
        pt.assert_not_called()
        self.assertIsNone(self._state(rid))

    def test_backfill_when_bundle_omits_uptime(self):
        """Regression guard (same class of bug that bit uptime_down): a cert_expiry
        rule must still evaluate when a pre-built bundle omits 'uptime' — the engine
        back-fills per-check states from the DB."""
        app.create_rule({"name": "c", "ctype": "cert_expiry",
                         "params": {"check_id": self.cid}, "enabled": True, "cooldown_min": 0})
        ts = int(time.time())
        with app.LOCK:
            # state 'up' with a near days_to_expiry -> cert_warn True at overview time.
            app.DB.execute("INSERT INTO uptime_results(check_id,ts,up,latency_ms,code,err,days_to_expiry) "
                           "VALUES(?,?,?,?,?,?,?)", (self.cid, ts, 1, 5.0, None, None, 7))
            app.DB.commit()
        collector_sig = {"anomalies": {"items": []}, "incidents": []}
        self.assertNotIn("uptime", collector_sig)
        with patch("app._post_text", return_value=(200, b"")) as pt:
            self.assertEqual(app.evaluate_rules(collector_sig), 1)
        pt.assert_called()


if __name__ == "__main__":
    unittest.main()
