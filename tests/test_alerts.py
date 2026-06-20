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


if __name__ == "__main__":
    unittest.main()
