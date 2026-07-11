"""Tests for the Lab Copilot Alert Advisor — proactive alert-rule recommendations
derived from the monitor's OWN live signals.

Covers the load-bearing guarantees the reviewer checks:
  • Every recommended spec is a VALID rule: it passes the REAL _validate_rule and
    survives a real create->delete round-trip (draft -> validate -> create ->
    delete) proving the one-click create path accepts it verbatim.
  • Deterministic ranking + specs with NO LLM: the default GET /api/alerts/advisor
    poll path NEVER calls _ollama_generate (tripwire). The LLM only runs on the
    explicit ?llm=1 action and its absence degrades gracefully.
  • No duplicates: a candidate already covered by an existing rule is dropped.
  • Read-only: hitting the advisor persists NO rule and writes NO state.
"""
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


# A rich, condition-met bundle in the exact shape _live_signal_bundle produces:
# disk/vram carry `status`, incidents is a list of {state, severity, members},
# uptime carries the cert-warn + SLO sub-objects the eval path reads.
SIG_MET = {
    "anomalies": {"items": [{"key": "gpu_power", "unit": "W", "value": 320.0,
                             "baseline": 200.0, "z": 4.1, "direction": "spike"}]},
    "disk": [{"mount": "/backup", "pct": 88, "gb_per_day": 1.6, "eta_days": 9.0,
              "status": "filling"}],
    "vram": {"status": "filling", "pct": 90, "mb_per_min": 50.0, "eta_min": 2880},
    "cost_month": {"enabled": True, "currency": "€", "projected_month": 42.0,
                   "month_to_date": 20.0},
    "incidents": [{"state": "open", "severity": "critical", "opened_at": 1,
                   "members": [{"series": "gpu_power", "active": True,
                                "direction": "spike"}]}],
    "uptime": [
        {"id": "cert1", "label": "tls", "type": "cert", "enabled": True,
         "state": "up", "cert_warn": True, "days_to_expiry": 5, "cert_warn_days": 14,
         "subject_cn": "example.com"},
        {"id": "slo1", "label": "api", "type": "http", "enabled": True, "state": "up",
         "slo": {"data_sufficient": True, "over_budget": True, "burn_1h": 30.0,
                 "burn_6h": 20.0, "budget_consumed_pct": 140.0, "target": 0.99,
                 "window_days_actual": 30}},
    ],
}

SIG_QUIET = {
    "anomalies": {"items": []},
    "disk": [{"mount": "/", "pct": 40, "gb_per_day": 0.0, "eta_days": None,
              "status": "steady"}],
    "vram": {"status": "steady", "pct": 30, "mb_per_min": 0.0, "eta_min": None},
    "cost_month": {"enabled": True, "currency": "€", "projected_month": 10.0,
                   "month_to_date": 5.0},
    "incidents": [],
    "uptime": [
        {"id": "slo1", "label": "api", "type": "http", "enabled": True, "state": "up",
         "slo": {"data_sufficient": True, "over_budget": False, "burn_1h": 0.3,
                 "burn_6h": 0.2, "budget_consumed_pct": 5.0, "target": 0.99}},
    ],
}


class TestAdvisorCandidates(unittest.TestCase):
    """Pure _advisor_candidates over seeded bundles: deterministic, correct set."""

    def test_met_bundle_yields_each_family(self):
        cands = app._advisor_candidates(SIG_MET, [], now=1000)
        ctypes = {c["ctype"] for c in cands}
        for want in ("disk_eta", "vram_eta", "anomaly", "incident",
                     "cert_expiry", "slo_burn"):
            self.assertIn(want, ctypes, f"expected a {want} suggestion")

    def test_quiet_bundle_yields_nothing(self):
        self.assertEqual(app._advisor_candidates(SIG_QUIET, [], now=1000), [])

    def test_every_spec_passes_real_validation(self):
        """The load-bearing proof: EVERY recommended spec passes the exact
        _validate_rule() the create path uses — no invalid draft can leak out."""
        cands = app._advisor_candidates(SIG_MET, [], now=1000)
        self.assertTrue(cands)
        for c in cands:
            clean, err = app._validate_rule(c["spec"])
            self.assertIsNone(err, f"{c['ctype']} spec rejected: {err}")
            self.assertEqual(clean["ctype"], c["ctype"])

    def test_disk_rationale_carries_numbers(self):
        c = next(c for c in app._advisor_candidates(SIG_MET, [], now=1000)
                 if c["ctype"] == "disk_eta")
        self.assertIn("/backup", c["rationale"])
        self.assertIn("1.6", c["rationale"])
        self.assertFalse(c["already_covered"])

    def test_ranking_is_deterministic(self):
        a = app._advisor_candidates(SIG_MET, [], now=1000)
        b = app._advisor_candidates(SIG_MET, [], now=1000)
        self.assertEqual([c["ctype"] for c in a], [c["ctype"] for c in b])


class TestAdvisorNoDuplicates(unittest.TestCase):
    def test_existing_rule_marks_covered(self):
        existing = [{"ctype": "disk_eta", "params": {"days": 14}},
                    {"ctype": "anomaly", "params": {"series": "gpu_power"}}]
        cands = app._advisor_candidates(SIG_MET, existing, now=1000)
        by = {c["ctype"]: c for c in cands}
        self.assertTrue(by["disk_eta"]["already_covered"])
        self.assertTrue(by["anomaly"]["already_covered"])
        # A family the user has NOT covered stays actionable.
        self.assertFalse(by["cert_expiry"]["already_covered"])

    def test_any_series_anomaly_subsumes_specific(self):
        existing = [{"ctype": "anomaly", "params": {"series": "any"}}]
        cands = app._advisor_candidates(SIG_MET, existing, now=1000)
        by = {c["ctype"]: c for c in cands}
        self.assertTrue(by["anomaly"]["already_covered"])

    def test_any_checkid_subsumes_specific(self):
        existing = [{"ctype": "cert_expiry", "params": {"check_id": "any"}}]
        cands = app._advisor_candidates(SIG_MET, existing, now=1000)
        by = {c["ctype"]: c for c in cands}
        self.assertTrue(by["cert_expiry"]["already_covered"])


class TestAdvisorEndpoint(unittest.TestCase):
    def setUp(self):
        _clean_db()

    def tearDown(self):
        _clean_db()

    def _client(self):
        return app.app.test_client()

    def test_default_get_never_calls_llm(self):
        """TRIPWIRE: the default GET poll path is deterministic — the LLM is never
        touched, no matter what the signals say."""
        c = self._client()
        with patch("app._ollama_generate") as og, \
             patch("app._live_signal_bundle", return_value=SIG_MET):
            r = c.get("/api/alerts/advisor")
        self.assertEqual(r.status_code, 200)
        og.assert_not_called()
        j = r.get_json()
        self.assertTrue(j["ok"])
        self.assertFalse(j["llm_used"])
        self.assertEqual(j["llm_status"], "skipped")
        self.assertTrue(j["recommendations"])

    def test_covered_dropped_from_response(self):
        """A duplicate of an existing rule is NOT returned as a suggestion."""
        app.create_rule({"name": "disk", "ctype": "disk_eta",
                         "params": {"days": 14}, "enabled": True})
        c = self._client()
        with patch("app._live_signal_bundle", return_value=SIG_MET):
            r = c.get("/api/alerts/advisor")
        ctypes = {x["ctype"] for x in r.get_json()["recommendations"]}
        self.assertNotIn("disk_eta", ctypes)

    def test_llm_action_enriches_but_degrades_gracefully(self):
        """?llm=1 opts into enrichment; when ollama is unreachable the deterministic
        rationale still stands and llm_status carries the reason."""
        c = self._client()
        with patch("app._ollama_generate", return_value=(None, "unreachable")) as og, \
             patch("app._live_signal_bundle", return_value=SIG_MET):
            r = c.get("/api/alerts/advisor?llm=1")
        og.assert_called_once()
        j = r.get_json()
        self.assertFalse(j["llm_used"])
        self.assertEqual(j["llm_status"], "unreachable")
        # Deterministic rationale still present on every reco.
        self.assertTrue(all(x.get("rationale") for x in j["recommendations"]))

    def test_llm_action_merges_rewrite(self):
        c = self._client()
        # A numbered rewrite for the first item only.
        fake = "1. Friendlier line for item one.\n"
        with patch("app._ollama_generate", return_value=(fake, None)), \
             patch("app._live_signal_bundle", return_value=SIG_MET):
            r = c.get("/api/alerts/advisor?llm=1")
        j = r.get_json()
        self.assertTrue(j["llm_used"])
        self.assertEqual(j["llm_status"], "ok")
        self.assertEqual(j["recommendations"][0]["rationale_llm"],
                         "Friendlier line for item one.")

    def test_endpoint_persists_nothing(self):
        """Read-only: hammering the advisor (with and without LLM) creates NO rule
        and writes NO alert-history row."""
        c = self._client()
        with patch("app._ollama_generate", return_value=("1. x", None)), \
             patch("app._live_signal_bundle", return_value=SIG_MET), \
             patch("app._post_text") as pt:
            for _ in range(4):
                c.get("/api/alerts/advisor")
                c.get("/api/alerts/advisor?llm=1")
        self.assertEqual(len(app.list_rules()), 0)
        with app.LOCK:
            nhist = app.DB.execute("SELECT COUNT(*) FROM alert_history").fetchone()[0]
        self.assertEqual(nhist, 0)
        pt.assert_not_called()

    def test_specific_checkid_kept_when_db_backed(self):
        """When the targeted uptime check is a REAL DB row, the advisor keeps the
        specific check_id (not 'any') and the spec passes create -> delete."""
        cid, err = app.create_uptime_check({"label": "tls-real", "type": "cert",
                                            "target": "https://example.com"})
        self.assertIsNone(err)
        try:
            sig = {"anomalies": {"items": []}, "disk": [], "vram": {},
                   "cost_month": {}, "incidents": [],
                   "uptime": [{"id": cid, "label": "tls-real", "type": "cert",
                               "enabled": True, "state": "up", "cert_warn": True,
                               "days_to_expiry": 5, "cert_warn_days": 14}]}
            cands = app._advisor_candidates(sig, [], now=1000)
            cert = next(c for c in cands if c["ctype"] == "cert_expiry")
            self.assertEqual(cert["spec"]["params"]["check_id"], cid)
            rid, cerr = app.create_rule(cert["spec"])
            self.assertIsNone(cerr)
            self.assertTrue(app.delete_rule(rid))
        finally:
            app.delete_uptime_check(cid)

    def test_recommended_spec_survives_create_delete(self):
        """End-to-end proof the one-click path works: take a live recommendation's
        spec and push it through the REAL create path, then delete it."""
        c = self._client()
        with patch("app._live_signal_bundle", return_value=SIG_MET):
            r = c.get("/api/alerts/advisor")
        recos = r.get_json()["recommendations"]
        self.assertTrue(recos)
        for reco in recos:
            rid, err = app.create_rule(reco["spec"])
            self.assertIsNone(err, f"{reco['ctype']} rejected by create: {err}")
            self.assertTrue(rid)
            self.assertTrue(app.delete_rule(rid))


if __name__ == "__main__":
    unittest.main()
