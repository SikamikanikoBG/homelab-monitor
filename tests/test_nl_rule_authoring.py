"""Tests for NL alert-rule authoring (E1 Lab Copilot).

"Describe an alert in plain English" → the local LLM DRAFTS a structured rule,
the server validates/clamps it against the REAL rule schema, returns a PROPOSAL
(never saved). Saving happens only through the existing validated create path.

Deterministic in CI: `_ollama_generate` is monkeypatched to return canned JSON,
so no live LLM is required. Coverage:
  • well-formed NL → valid proposed rule matching the schema
  • the endpoint does NOT persist (rules count unchanged)
  • garbage / non-JSON LLM response → safe rejection (no 500, valid=False, no row)
  • out-of-range threshold → clamped
  • unknown ctype → flagged (valid=False), unknown channel/series/check → corrected
  • LLM disabled / unreachable → graceful (200, not 500)
  • NO LLM on the poll path: /api/data, /api/health, and the rules-list GET never
    call _ollama_generate
  • saving the proposal via the real create path yields a valid persisted rule
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


def _count_rules():
    with app.LOCK:
        return app.DB.execute("SELECT COUNT(*) FROM alert_rules").fetchone()[0]


class TestFromTextDraft(unittest.TestCase):
    def setUp(self):
        self._en = app.COPILOT_ENABLED
        self._gen = app._ollama_generate
        app.COPILOT_ENABLED = True
        self.c = app.app.test_client()

    def tearDown(self):
        app.COPILOT_ENABLED = self._en
        app._ollama_generate = self._gen

    def _stub(self, payload):
        """payload: dict|str returned as the model's JSON response text."""
        text = payload if isinstance(payload, str) else json.dumps(payload)

        def fake(prompt, timeout=None, capture=None, fmt=None):
            if isinstance(capture, list):
                capture.append({"tok": 1})
            return text, None
        app._ollama_generate = fake

    def _stub_err(self, err):
        def fake(prompt, timeout=None, capture=None, fmt=None):
            return None, err
        app._ollama_generate = fake

    # ── well-formed NL → valid proposal, matching the real schema ──────────────
    def test_wellformed_returns_valid_proposal(self):
        self._stub({"name": "GPU temp spike", "ctype": "anomaly",
                    "params": {"series": "gpu_temp"}, "channel": "all",
                    "level": "warning", "cooldown_min": 30,
                    "summary": "Alert when GPU temperature is anomalous."})
        before = _count_rules()
        r = self.c.post("/api/alerts/rules/from_text",
                        json={"text": "tell me if GPU temperature goes over 85 for 10 minutes"})
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertTrue(j["valid"])
        self.assertEqual(j["llm_status"], "ok")
        p = j["proposal"]
        self.assertEqual(p["ctype"], "anomaly")
        self.assertEqual(p["params"]["series"], "gpu_temp")
        self.assertEqual(p["level"], "warning")
        self.assertEqual(p["cooldown_min"], 30)
        self.assertFalse(p["enabled"])                # never armed
        self.assertTrue(j["summary"])
        # PROPOSAL ONLY — nothing persisted.
        self.assertEqual(_count_rules(), before)

    def test_proposal_passes_the_real_validator_and_saves(self):
        self._stub({"name": "Disk fills soon", "ctype": "disk_eta",
                    "params": {"days": 5}, "channel": "all", "level": "critical",
                    "cooldown_min": 120, "summary": "x"})
        r = self.c.post("/api/alerts/rules/from_text", json={"text": "warn me if a disk fills within 5 days"})
        p = r.get_json()["proposal"]
        # Defense in depth: the drafted proposal must be accepted verbatim by the
        # SAME validator the manual create path uses.
        clean, err = app._validate_rule({**p, "enabled": True})
        self.assertIsNone(err)
        # Saving through the real path produces exactly one new valid rule.
        before = _count_rules()
        rid, cerr = app.create_rule({**p, "enabled": True})
        self.assertIsNone(cerr)
        self.assertEqual(_count_rules(), before + 1)
        app.delete_rule(rid)

    # ── garbage / non-JSON → safe rejection, no 500, no persistence ────────────
    def test_garbage_non_json_is_safe(self):
        self._stub("this is not json at all {{{")
        before = _count_rules()
        r = self.c.post("/api/alerts/rules/from_text", json={"text": "asdf"})
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertFalse(j["valid"])
        self.assertTrue(j["assumptions"])            # explains it couldn't map
        self.assertEqual(_count_rules(), before)

    def test_unknown_ctype_flagged_not_persisted(self):
        self._stub({"name": "x", "ctype": "launch_missile",
                    "params": {}, "summary": "x"})
        before = _count_rules()
        r = self.c.post("/api/alerts/rules/from_text", json={"text": "do something weird"})
        j = r.get_json()
        self.assertFalse(j["valid"])
        self.assertEqual(j["proposal"]["ctype"], "")   # unknown → blanked, form editable
        self.assertEqual(_count_rules(), before)

    # ── clamping / correction ─────────────────────────────────────────────────
    def test_out_of_range_days_clamped(self):
        self._stub({"name": "x", "ctype": "vram_eta",
                    "params": {"days": 999999}, "summary": "x"})
        r = self.c.post("/api/alerts/rules/from_text", json={"text": "vram soon"})
        j = r.get_json()
        self.assertTrue(j["valid"])
        self.assertLessEqual(j["proposal"]["params"]["days"], 3650)
        self.assertTrue(any("Days" in a for a in j["assumptions"]))

    def test_negative_cooldown_clamped(self):
        self._stub({"name": "x", "ctype": "anomaly",
                    "params": {"series": "any"}, "cooldown_min": -50, "summary": "x"})
        j = self.c.post("/api/alerts/rules/from_text", json={"text": "x"}).get_json()
        self.assertEqual(j["proposal"]["cooldown_min"], 0)

    def test_unknown_series_corrected_to_any(self):
        self._stub({"name": "x", "ctype": "anomaly",
                    "params": {"series": "cpu_temperature_zzz"}, "summary": "x"})
        j = self.c.post("/api/alerts/rules/from_text", json={"text": "x"}).get_json()
        self.assertEqual(j["proposal"]["params"]["series"], "any")
        self.assertTrue(j["valid"])

    def test_unknown_channel_corrected_to_all(self):
        self._stub({"name": "x", "ctype": "anomaly",
                    "params": {"series": "any"}, "channel": "carrier_pigeon",
                    "summary": "x"})
        j = self.c.post("/api/alerts/rules/from_text", json={"text": "x"}).get_json()
        self.assertEqual(j["proposal"]["channel"], "all")

    def test_unknown_check_id_corrected_to_any(self):
        self._stub({"name": "x", "ctype": "uptime_down",
                    "params": {"check_id": "does-not-exist"}, "summary": "x"})
        j = self.c.post("/api/alerts/rules/from_text", json={"text": "x"}).get_json()
        self.assertEqual(j["proposal"]["params"]["check_id"], "any")
        self.assertTrue(j["valid"])

    def test_slo_burn_defaults_filled(self):
        self._stub({"name": "x", "ctype": "slo_burn",
                    "params": {"policy": "multi_window"}, "summary": "x"})
        j = self.c.post("/api/alerts/rules/from_text", json={"text": "x"}).get_json()
        p = j["proposal"]["params"]
        self.assertEqual(p["policy"], "multi_window")
        self.assertEqual(p["fast_burn"], 14.4)
        self.assertEqual(p["slow_burn"], 6.0)
        self.assertTrue(j["valid"])

    # ── graceful degrade ──────────────────────────────────────────────────────
    def test_empty_text_no_llm_call(self):
        called = {"n": 0}

        def fake(*a, **k):
            called["n"] += 1
            return "{}", None
        app._ollama_generate = fake
        r = self.c.post("/api/alerts/rules/from_text", json={"text": "   "})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["llm_status"], "no_text")
        self.assertEqual(called["n"], 0)             # no LLM for empty input

    def test_llm_unreachable_graceful(self):
        self._stub_err("unreachable")
        r = self.c.post("/api/alerts/rules/from_text", json={"text": "gpu hot"})
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertFalse(j["valid"])
        self.assertEqual(j["llm_status"], "unreachable")
        self.assertIsNone(j["proposal"])

    def test_disabled_graceful_no_llm_call(self):
        app.COPILOT_ENABLED = False
        called = {"n": 0}

        def fake(*a, **k):
            called["n"] += 1
            return "{}", None
        app._ollama_generate = fake
        r = self.c.post("/api/alerts/rules/from_text", json={"text": "gpu hot"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["llm_status"], "disabled")
        self.assertEqual(called["n"], 0)

    def test_bad_payload_no_500(self):
        r = self.c.post("/api/alerts/rules/from_text", data="not json",
                        content_type="application/json")
        self.assertEqual(r.status_code, 200)


class TestNoLLMOnPollPaths(unittest.TestCase):
    """The rules-list GET and the dashboard poll endpoints must NEVER touch the
    LLM — the drafting call is confined to the explicit from_text POST."""
    def setUp(self):
        self._gen = app._ollama_generate
        self._en = app.COPILOT_ENABLED
        app.COPILOT_ENABLED = True
        self.calls = {"n": 0}

        def tripwire(*a, **k):
            self.calls["n"] += 1
            return "{}", None
        app._ollama_generate = tripwire
        self.c = app.app.test_client()

    def tearDown(self):
        app._ollama_generate = self._gen
        app.COPILOT_ENABLED = self._en

    def test_rules_list_no_llm(self):
        r = self.c.get("/api/alerts/rules")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.calls["n"], 0)

    def test_data_and_health_no_llm(self):
        for path in ("/api/data", "/api/health"):
            self.c.get(path)
        self.assertEqual(self.calls["n"], 0)


if __name__ == "__main__":
    unittest.main()
