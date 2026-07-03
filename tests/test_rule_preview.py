"""Tests for the read-only, side-effect-free rule dry-run:
POST /api/alerts/rules/preview + the pure _preview_rule() it uses.

Covers: would_fire correctness (condition met / not met) for every supported
ctype; a clear rejection (no 500) for an invalid spec; and — the load-bearing
guarantees — that a preview dispatches NO notification, writes NO cooldown/
last-fired/snooze state, creates/changes NO rule row or alert-history row,
persists nothing, and never touches the LLM path."""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


def _clean_db():
    with app.LOCK:
        app.DB.execute("DELETE FROM alert_rules")
        app.DB.execute("DELETE FROM alert_history")
        app.DB.commit()


# ── Seeded signal bundles: a condition-met and a condition-not-met per ctype ──
SIG_MET = {
    "anomalies": {"items": [{"key": "gpu_power", "unit": "W", "value": 320.0,
                             "baseline": 200.0, "z": 4.1, "direction": "spike"}]},
    "disk": [{"mount": "/", "pct": 96, "gb_per_day": 5.0, "eta_days": 1.5}],
    "vram": {"status": "filling", "pct": 90, "mb_per_min": 50.0, "eta_min": 1440},
    "cost_month": {"enabled": True, "currency": "$", "projected_month": 42.0,
                   "month_to_date": 20.0},
    "incidents": [{"state": "open", "severity": "critical", "opened_at": 1,
                   "members": [{"series": "gpu_power", "active": True, "direction": "spike"}]}],
    "uptime": [
        {"id": "c1", "label": "web", "type": "http", "enabled": True, "state": "down",
         "last_code": 500},
        {"id": "cert1", "label": "tls", "type": "cert", "enabled": True, "state": "up",
         "cert_warn": True, "days_to_expiry": 5, "cert_warn_days": 14,
         "subject_cn": "example.com"},
        {"id": "slo1", "label": "api", "type": "http", "enabled": True, "state": "up",
         "slo": {"data_sufficient": True, "over_budget": True, "burn_1h": 30.0,
                 "burn_6h": 20.0, "budget_consumed_pct": 140.0, "target": 0.99,
                 "window_days_actual": 30}},
    ],
}

SIG_QUIET = {
    "anomalies": {"items": []},
    "disk": [{"mount": "/", "pct": 40, "gb_per_day": 0.0, "eta_days": None}],
    "vram": {"status": "steady", "pct": 30, "mb_per_min": 0.0, "eta_min": None},
    "cost_month": {"enabled": True, "currency": "$", "projected_month": 10.0,
                   "month_to_date": 5.0},
    "incidents": [],
    "uptime": [
        {"id": "c1", "label": "web", "type": "http", "enabled": True, "state": "up",
         "last_code": 200},
        {"id": "cert1", "label": "tls", "type": "cert", "enabled": True, "state": "up",
         "cert_warn": False, "days_to_expiry": 90, "cert_warn_days": 14},
        {"id": "slo1", "label": "api", "type": "http", "enabled": True, "state": "up",
         "slo": {"data_sufficient": True, "over_budget": False, "burn_1h": 0.3,
                 "burn_6h": 0.2, "budget_consumed_pct": 5.0, "target": 0.99,
                 "window_days_actual": 30}},
    ],
}


def _spec(ctype, params):
    clean, err = app._validate_rule({"name": "p", "ctype": ctype, "params": params,
                                     "enabled": False})
    assert err is None, err
    return clean


class TestPreviewPerType(unittest.TestCase):
    """Each supported ctype: condition met -> would_fire True; not met -> False,
    each with an explanatory detail. Pure _preview_rule against seeded bundles."""

    CASES = [
        ("anomaly", {"series": "any"}),
        ("disk_eta", {"days": 7}),
        ("vram_eta", {"days": 2}),
        ("cost_budget", {"budget": 30}),
        ("incident", {"severity": "warning"}),
        ("uptime_down", {"check_id": "any"}),
        ("cert_expiry", {"check_id": "any"}),
        ("slo_burn", {"check_id": "any", "policy": "single", "burn_threshold": 1.0}),
    ]

    def test_condition_met_fires(self):
        for ct, params in self.CASES:
            wf, detail, observed = app._preview_rule(_spec(ct, params), SIG_MET)
            self.assertTrue(wf, f"{ct} should fire when met (got {wf}: {detail})")
            self.assertTrue(detail)

    def test_condition_not_met_no_fire(self):
        for ct, params in self.CASES:
            wf, detail, observed = app._preview_rule(_spec(ct, params), SIG_QUIET)
            self.assertFalse(wf, f"{ct} should not fire when quiet (got {wf}: {detail})")
            self.assertTrue(detail)

    def test_detail_carries_numbers(self):
        # A "not firing" cost detail should surface projected vs budget.
        wf, detail, observed = app._preview_rule(_spec("cost_budget", {"budget": 100}), SIG_MET)
        self.assertFalse(wf)
        self.assertIn("42", detail)
        self.assertIn("100", detail)
        self.assertEqual(observed["projected_month"], 42.0)

    def test_slo_multi_window_fast_burn(self):
        clean = _spec("slo_burn", {"check_id": "any", "policy": "multi_window",
                                   "fast_burn": 14.4, "slow_burn": 6.0})
        wf, detail, _ = app._preview_rule(clean, SIG_MET)
        self.assertTrue(wf)

    def test_cost_budget_disabled_is_null(self):
        wf, detail, observed = app._preview_rule(_spec("cost_budget", {"budget": 1}),
                                                 {"cost_month": {"enabled": False}})
        self.assertIsNone(wf)
        self.assertIn("Cost tracking is off", detail)

    def test_slo_no_data_is_null(self):
        sig = {"uptime": [{"id": "s", "label": "s", "type": "http", "enabled": True,
                           "state": "up", "slo": {"data_sufficient": False}}]}
        wf, detail, _ = app._preview_rule(
            _spec("slo_burn", {"check_id": "any", "policy": "single", "burn_threshold": 1.0}), sig)
        self.assertIsNone(wf)


class TestPreviewEndpoint(unittest.TestCase):
    def setUp(self):
        _clean_db()
        app.save_settings({"discord_webhook_url": "", "telegram_token": "",
                           "telegram_chat_id": "", "webhook_url": "", "ntfy_topic": "hlm-test"})

    def tearDown(self):
        app.save_settings({"ntfy_topic": ""})
        _clean_db()

    def _client(self):
        return app.app.test_client()

    def test_endpoint_would_fire_true(self):
        c = self._client()
        with patch("app._live_signal_bundle", return_value=SIG_MET):
            r = c.post("/api/alerts/rules/preview",
                       json={"ctype": "cost_budget", "params": {"budget": 1}})
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertTrue(j["ok"] and j["valid"])
        self.assertTrue(j["would_fire"])
        self.assertTrue(j["detail"])

    def test_endpoint_would_fire_false(self):
        c = self._client()
        with patch("app._live_signal_bundle", return_value=SIG_QUIET):
            r = c.post("/api/alerts/rules/preview",
                       json={"ctype": "cost_budget", "params": {"budget": 1000}})
        j = r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertFalse(j["would_fire"])

    def test_endpoint_no_name_needed(self):
        # A form spec with no name yet must still preview (name is irrelevant here).
        c = self._client()
        with patch("app._live_signal_bundle", return_value=SIG_MET):
            r = c.post("/api/alerts/rules/preview",
                       json={"ctype": "uptime_down", "params": {"check_id": "any"}})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["valid"])

    def test_invalid_spec_clear_rejection_no_500(self):
        c = self._client()
        r = c.post("/api/alerts/rules/preview", json={"ctype": "bogus", "params": {}})
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertFalse(j["valid"])
        self.assertIsNone(j["would_fire"])
        self.assertTrue(j["detail"])

    def test_invalid_params_rejected(self):
        c = self._client()
        r = c.post("/api/alerts/rules/preview",
                   json={"ctype": "cost_budget", "params": {"budget": "abc"}})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.get_json()["valid"])

    # ── The load-bearing guarantees ──────────────────────────────────────────
    def test_preview_never_dispatches(self):
        """Spy the low-level notification sender: it must NEVER be called during a
        preview, even for a spec whose condition is met."""
        c = self._client()
        with patch("app._post_text") as pt, \
             patch("app._live_signal_bundle", return_value=SIG_MET):
            for _ in range(3):
                c.post("/api/alerts/rules/preview",
                       json={"ctype": "cost_budget", "params": {"budget": 1}})
                c.post("/api/alerts/rules/preview",
                       json={"ctype": "uptime_down", "params": {"check_id": "any"}})
        pt.assert_not_called()

    def test_preview_never_calls_llm(self):
        """No LLM on this path — it's deterministic signal evaluation."""
        c = self._client()
        with patch("app._ollama_generate") as og, \
             patch("app._live_signal_bundle", return_value=SIG_MET):
            c.post("/api/alerts/rules/preview",
                   json={"ctype": "anomaly", "params": {"series": "any"}})
        og.assert_not_called()

    def test_preview_writes_no_state_and_no_rows(self):
        """A real enabled rule with a cooldown must be byte-for-byte unchanged after
        any number of previews: no last_fired_at/last_state/snooze write, no new
        rule row, no alert-history row. Rules count + history count unchanged."""
        rid, _ = app.create_rule({"name": "real", "ctype": "anomaly",
                                  "params": {"series": "any"}, "enabled": True,
                                  "cooldown_min": 60})

        def _snapshot():
            with app.LOCK:
                row = app.DB.execute(
                    "SELECT last_fired_at, last_state, snoozed_until, params, name, "
                    "channel, level, cooldown_min FROM alert_rules WHERE id=?",
                    (rid,)).fetchone()
                nrules = app.DB.execute("SELECT COUNT(*) FROM alert_rules").fetchone()[0]
                nhist = app.DB.execute("SELECT COUNT(*) FROM alert_history").fetchone()[0]
            return tuple(row), nrules, nhist

        before = _snapshot()
        c = self._client()
        with patch("app._post_text") as pt, \
             patch("app._live_signal_bundle", return_value=SIG_MET):
            # Preview the EXISTING rule's spec (met) and brand-new specs many times.
            for _ in range(5):
                c.post("/api/alerts/rules/preview",
                       json={"name": "real", "ctype": "anomaly", "params": {"series": "any"}})
                c.post("/api/alerts/rules/preview",
                       json={"ctype": "cost_budget", "params": {"budget": 1}})
                c.post("/api/alerts/rules/preview",
                       json={"ctype": "slo_burn", "params": {"check_id": "any"}})
        after = _snapshot()
        self.assertEqual(before, after)
        pt.assert_not_called()
        # Exactly the one rule we created — no preview ever persisted a spec.
        self.assertEqual(len(app.list_rules()), 1)

    def test_preview_creates_no_rule(self):
        c = self._client()
        n0 = len(app.list_rules())
        with patch("app._live_signal_bundle", return_value=SIG_MET):
            for _ in range(4):
                c.post("/api/alerts/rules/preview",
                       json={"name": "throwaway", "ctype": "vram_eta",
                             "params": {"days": 2}})
        self.assertEqual(len(app.list_rules()), n0)


if __name__ == "__main__":
    unittest.main()
