"""Tests for 🧭 "What to fix first" — the AI-prioritized remediation plan layered on
the deterministic Lab Health Score (/api/health/fixplan).

The hard invariants a reviewer checks:
  • The deterministic plan is worst-first — its order + priorities match the health
    score's own factor order (by |delta|), and every item deep-links to an EXISTING
    tab from the mapping (no invented routes).
  • NO LLM on any poll path: the default GET (no ?llm) NEVER calls _ollama_generate;
    neither does /api/health_score, /api/forecast, /api/data. Tripwire.
  • Deterministic priority + deep-links are the source of truth: a HOSTILE / garbage
    LLM response can't inject a fake factor, reorder priority, or change a link — it
    can only reword the prose of items it correctly maps by number; unmapped items
    are dropped.
  • Graceful degrade: LLM off / unreachable / garbage → the deterministic plan still
    returns, llm_status is honest.
  • All-clear when healthy: no firing factors → a calm single "nothing to fix" item,
    never a fabricated problem, and the LLM is NOT invoked for it.
  • Read-only / never persists / never mutates: the endpoint touches no DB writes.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


# A health-score dict with several firing factors of KNOWN, distinct magnitudes so
# we can assert the worst-first order deterministically without live signals.
def _fake_health():
    return {
        "score": 41, "band": "at_risk", "tier": "crit",
        "factors": [
            {"key": "uptime", "label": "Uptime checks", "delta": -20,
             "detail": "1 of 3 checks down", "meta": {}},
            {"key": "disk", "label": "Disk capacity", "delta": -12,
             "detail": "/backup fills in ~3d", "meta": {}},
            {"key": "anomalies", "label": "Active anomalies", "delta": -10,
             "detail": "2 anomalies firing", "meta": {}},
            {"key": "thermal", "label": "GPU thermals", "delta": -9,
             "detail": "GPU at 86°C", "meta": {}},
        ],
        "caps": dict(app._HS_CAPS), "generated_at": 0,
    }


class TestBuildFixplanDeterministic(unittest.TestCase):
    def test_order_matches_worst_first_factors(self):
        plan = app.build_fixplan(_fake_health())
        self.assertEqual([p["key"] for p in plan],
                         ["uptime", "disk", "anomalies", "thermal"])
        self.assertEqual([p["priority"] for p in plan], [1, 2, 3, 4])

    def test_every_item_deep_links_to_existing_tab(self):
        # The set of real dashboard tab ids (from _HS_FIX targets) — none invented.
        plan = app.build_fixplan(_fake_health())
        valid_tabs = {"overview", "gpu", "uptime", "disks"}
        for p in plan:
            self.assertIn("deep_link", p)
            self.assertIn(p["deep_link"]["tab"], valid_tabs)
            self.assertEqual(p["deep_link"]["tab"], app._HS_FIX[p["key"]]["tab"])
            self.assertEqual(p["deep_link"]["anchor"], app._HS_FIX[p["key"]]["anchor"])

    def test_carries_live_detail_and_prose(self):
        plan = app.build_fixplan(_fake_health())
        disk = next(p for p in plan if p["key"] == "disk")
        self.assertEqual(disk["detail"], "/backup fills in ~3d")
        self.assertTrue(disk["title"] and disk["why"] and disk["action"])
        self.assertEqual(disk["severity"], "warn")

    def test_unknown_factor_key_is_ignored(self):
        h = _fake_health()
        h["factors"].append({"key": "made_up", "delta": -99, "detail": "x", "meta": {}})
        plan = app.build_fixplan(h)
        self.assertNotIn("made_up", [p["key"] for p in plan])

    def test_all_clear_when_no_factors(self):
        plan = app.build_fixplan({"factors": []})
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0]["key"], "all_clear")
        self.assertEqual(plan[0]["severity"], "ok")


class TestFixplanLLMValidation(unittest.TestCase):
    """The LLM can ONLY reword prose of items it maps by number; it can't inject a
    factor, reorder, or change a deep-link."""

    def test_hostile_llm_cannot_inject_factor_or_link(self):
        plan = app.build_fixplan(_fake_health())
        keys_before = [p["key"] for p in plan]
        links_before = [dict(p["deep_link"]) for p in plan]
        # A hostile payload: a bogus out-of-range item (fake factor), a poisoned
        # deep_link, and a legit reword of item 1.
        hostile = (
            '{"items":['
            '{"n":99,"title":"PWNED","why":"evil","action":"rm -rf",'
            '"key":"exfil","deep_link":{"tab":"http://evil"}},'
            '{"n":1,"title":"Restore the down check","why":"users are affected",'
            '"action":"open Uptime","deep_link":{"tab":"http://evil"},"severity":"ok"}'
            ']}')
        n = app._fixplan_apply_llm(plan, hostile)
        self.assertEqual(n, 1)                      # only item 1 enriched
        # No new factor / no reorder / links + keys + severities untouched.
        self.assertEqual([p["key"] for p in plan], keys_before)
        self.assertEqual([dict(p["deep_link"]) for p in plan], links_before)
        self.assertEqual(len(plan), 4)
        self.assertEqual(plan[0]["title_llm"], "Restore the down check")
        self.assertEqual(plan[0]["severity"], "crit")   # NOT overwritten to "ok"
        self.assertNotIn("PWNED", str([p.get("title_llm") for p in plan]))

    def test_garbage_llm_leaves_deterministic_prose(self):
        plan = app.build_fixplan(_fake_health())
        for bad in ("not json at all", "", "[]", '{"nope":1}', '{"items":"x"}'):
            n = app._fixplan_apply_llm(plan, bad)
            self.assertEqual(n, 0)
        # Deterministic titles intact, no *_llm keys leaked.
        self.assertTrue(all("title_llm" not in p for p in plan))
        self.assertTrue(all(p["title"] for p in plan))

    def test_empty_or_glyph_reword_does_not_clobber_prose(self):
        # A tiny model sometimes emits a lone glyph / replacement char or blank
        # string; that must NOT replace the good deterministic prose.
        plan = app.build_fixplan(_fake_health())
        junk = ('{"items":[{"n":1,"title":"�","why":"  ","action":"x"},'
                '{"n":2,"title":"ok","why":"Real reason here","action":"Do this now"}]}')
        app._fixplan_apply_llm(plan, junk)
        # Item 1: every field was junk → NOTHING enriched, deterministic stands.
        self.assertNotIn("title_llm", plan[0])
        self.assertNotIn("why_llm", plan[0])
        self.assertNotIn("action_llm", plan[0])
        # Item 2: the substantive fields landed; the too-short title did not.
        self.assertNotIn("title_llm", plan[1])
        self.assertEqual(plan[1]["why_llm"], "Real reason here")
        self.assertEqual(plan[1]["action_llm"], "Do this now")


class TestFixplanEndpoint(unittest.TestCase):
    def setUp(self):
        self.c = app.app.test_client()

    def test_default_poll_is_llm_free(self):
        # Tripwire: the plain GET must never touch the LLM.
        with patch("app._ollama_generate",
                   side_effect=AssertionError("LLM on poll path")) as og:
            r = self.c.get("/api/health/fixplan")
            self.assertEqual(r.status_code, 200)
            j = r.get_json()
            self.assertTrue(j["ok"])
            self.assertFalse(j["llm_used"])
            self.assertEqual(j["llm_status"], "skipped")
            self.assertIn("plan", j)
            self.assertIn("score", j)
            self.assertEqual(og.call_count, 0)

    def test_sibling_poll_paths_stay_llm_free(self):
        with patch("app._ollama_generate",
                   side_effect=AssertionError("LLM on poll path")) as og:
            for path in ("/api/health_score", "/api/forecast", "/api/data"):
                self.assertEqual(self.c.get(path).status_code, 200)
            self.assertEqual(og.call_count, 0)

    def test_plan_matches_live_health_order(self):
        # Force a known health state, then assert the endpoint's plan mirrors it.
        h = _fake_health()
        with patch("app._gather_health_signals", return_value={}), \
             patch("app.compute_health_score", return_value=h):
            r = self.c.get("/api/health/fixplan")
            j = r.get_json()
        self.assertEqual([p["key"] for p in j["plan"]],
                         ["uptime", "disk", "anomalies", "thermal"])
        self.assertEqual(j["score"], 41)
        self.assertEqual(j["band"], "at_risk")
        self.assertFalse(j["all_clear"])

    def test_llm_flag_enriches_when_ok(self):
        h = _fake_health()
        good = '{"items":[{"n":1,"title":"Fix the down check now"}]}'
        with patch("app._gather_health_signals", return_value={}), \
             patch("app.compute_health_score", return_value=h), \
             patch("app.COPILOT_ENABLED", True), \
             patch("app._ollama_generate", return_value=(good, None)) as og:
            r = self.c.post("/api/health/fixplan?llm=1")
            j = r.get_json()
        self.assertEqual(og.call_count, 1)
        self.assertTrue(j["llm_used"])
        self.assertEqual(j["llm_status"], "ok")
        self.assertEqual(j["plan"][0]["title_llm"], "Fix the down check now")

    def test_llm_flag_degrades_when_unreachable(self):
        h = _fake_health()
        with patch("app._gather_health_signals", return_value={}), \
             patch("app.compute_health_score", return_value=h), \
             patch("app.COPILOT_ENABLED", True), \
             patch("app._ollama_generate", return_value=(None, "unreachable")) as og:
            r = self.c.post("/api/health/fixplan?llm=1")
            j = r.get_json()
        self.assertEqual(og.call_count, 1)
        self.assertFalse(j["llm_used"])
        self.assertEqual(j["llm_status"], "unreachable")
        # Deterministic plan still fully present + ordered.
        self.assertEqual([p["key"] for p in j["plan"]],
                         ["uptime", "disk", "anomalies", "thermal"])

    def test_llm_flag_reports_disabled_without_calling(self):
        h = _fake_health()
        with patch("app._gather_health_signals", return_value={}), \
             patch("app.compute_health_score", return_value=h), \
             patch("app.COPILOT_ENABLED", False), \
             patch("app._ollama_generate",
                   side_effect=AssertionError("must not call when disabled")) as og:
            r = self.c.post("/api/health/fixplan?llm=1")
            j = r.get_json()
        self.assertEqual(og.call_count, 0)
        self.assertEqual(j["llm_status"], "disabled")
        self.assertFalse(j["llm_used"])

    def test_all_clear_never_calls_llm_even_with_flag(self):
        clear = {"score": 100, "band": "excellent", "tier": "ok",
                 "factors": [], "caps": dict(app._HS_CAPS), "generated_at": 0}
        with patch("app._gather_health_signals", return_value={}), \
             patch("app.compute_health_score", return_value=clear), \
             patch("app.COPILOT_ENABLED", True), \
             patch("app._ollama_generate",
                   side_effect=AssertionError("no LLM for all-clear")) as og:
            r = self.c.post("/api/health/fixplan?llm=1")
            j = r.get_json()
        self.assertEqual(og.call_count, 0)
        self.assertTrue(j["all_clear"])
        self.assertEqual(len(j["plan"]), 1)
        self.assertEqual(j["plan"][0]["key"], "all_clear")

    def test_endpoint_never_writes_db(self):
        # Read-only guarantee: hammering the endpoint (with and without the LLM flag)
        # changes NO row in ANY table — snapshot every table's row count before/after.
        def _row_totals():
            with app.LOCK:
                tables = [r[0] for r in app.DB.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
                return {t: app.DB.execute(
                    "SELECT COUNT(*) FROM \"%s\"" % t).fetchone()[0] for t in tables}
        before = _row_totals()
        with patch("app._ollama_generate", return_value=(None, "unreachable")):
            for _ in range(3):
                self.assertEqual(self.c.get("/api/health/fixplan").status_code, 200)
                self.assertEqual(
                    self.c.post("/api/health/fixplan?llm=1").status_code, 200)
        self.assertEqual(before, _row_totals())


if __name__ == "__main__":
    unittest.main()
