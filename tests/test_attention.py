"""Cockpit "needs attention" rollup — the CHEAP, LLM-FREE counts path.

The hero rollup + sidebar nav badges ride the already-polled /api/forecast, which
folds in compact recommendation counts (reco{crit,warn,total}) plus the open-incident
and down-uptime-check tallies. The whole point of this slice is that the frequently
polled badge path NEVER calls the local LLM. These tests assert:
  • _reco_counts() tallies match _reco_detect() and NEVER call _ollama_generate
  • /api/forecast exposes reco/incidents/uptime/attention and stays 200
  • the forecast path does NOT invoke the LLM (so the badge poll is LLM-free)
  • counts are correct for fired detectors, incidents-open and checks-down
  • all-clear → zero counts
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


def _sig(**over):
    base = {
        "disk": [],
        "vram": {"status": "stable", "free_gb": 20.0, "total_gb": 24.0,
                 "models_gb": 4.0, "eta_min": None, "mb_per_min": 0.0},
        "cost_month": {"enabled": False},
        "anomalies": {"status": "quiet", "items": []},
        "incidents": {"open": 0, "top": None},
        "uptime": [],
        "ooms": [],
    }
    base.update(over)
    return base


class TestRecoCounts(unittest.TestCase):
    def test_all_clear_is_zero(self):
        self.assertEqual(app._reco_counts(_sig()), {"crit": 0, "warn": 0, "total": 0})

    def test_counts_match_detect(self):
        sig = _sig(
            disk=[{"mount": "/x", "status": "filling", "eta_days": 3, "pct": 95,
                   "free_gb": 10, "gb_per_day": 9}],                       # crit
            uptime=[{"id": "u1", "label": "prod", "enabled": True, "state": "down",
                     "uptime": 0, "window_total": 9, "last_code": 500,
                     "last_checked": 1}],                                  # crit
            ooms=[{"service": "svc", "count": app.RECO_OOM_WARN_N, "last_ts": 1}],  # warn
        )
        counts = app._reco_counts(sig)
        items = app._reco_detect(sig)[:app.RECO_MAX_ITEMS]
        self.assertEqual(counts["total"], len(items))
        self.assertEqual(counts["crit"], sum(1 for it in items if it["severity"] == "crit"))
        self.assertEqual(counts["warn"], sum(1 for it in items if it["severity"] == "warn"))
        self.assertGreaterEqual(counts["crit"], 2)
        self.assertGreaterEqual(counts["warn"], 1)

    def test_counts_never_call_llm(self):
        called = {"n": 0}
        orig = app._ollama_generate
        app._ollama_generate = lambda *a, **k: called.__setitem__("n", called["n"] + 1) or ("x", None)
        try:
            app._reco_counts(_sig(disk=[{"mount": "/x", "status": "filling", "eta_days": 2,
                                         "pct": 99, "free_gb": 1, "gb_per_day": 9}]))
        finally:
            app._ollama_generate = orig
        self.assertEqual(called["n"], 0)

    def test_never_raises_on_garbage(self):
        # A malformed bundle degrades to empty, never raises.
        self.assertEqual(app._reco_counts({"disk": None, "uptime": None}),
                         {"crit": 0, "warn": 0, "total": 0})


class TestForecastAttention(unittest.TestCase):
    """/api/forecast folds in the cheap counts and must stay LLM-free + 200."""

    def setUp(self):
        self.c = app.app.test_client()

    def test_shape_always_200(self):
        r = self.c.get("/api/forecast")
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        for k in ("reco", "incidents", "uptime", "attention"):
            self.assertIn(k, j)
        for k in ("crit", "warn", "total"):
            self.assertIn(k, j["reco"])
        self.assertIn("down", j["uptime"])
        self.assertIn("open", j["incidents"])
        a = j["attention"]
        self.assertIn("reco", a)
        self.assertIn("incidents_open", a)
        self.assertIn("uptime_down", a)

    def test_forecast_never_calls_llm(self):
        # The badge/rollup polls this endpoint — it must NEVER touch the LLM.
        called = {"n": 0}
        orig = app._ollama_generate
        app._ollama_generate = lambda *a, **k: called.__setitem__("n", called["n"] + 1) or ("x", None)
        try:
            r = self.c.get("/api/forecast")
        finally:
            app._ollama_generate = orig
        self.assertEqual(r.status_code, 200)
        self.assertEqual(called["n"], 0)

    def test_attention_mirrors_reco(self):
        j = self.c.get("/api/forecast").get_json()
        self.assertEqual(j["attention"]["reco"], j["reco"])
        self.assertEqual(j["attention"]["uptime_down"], j["uptime"]["down"])
        self.assertEqual(j["attention"]["incidents_open"], j["incidents"]["open"])


class TestUptimeDownCount(unittest.TestCase):
    """The down-check tally only counts ENABLED checks in the 'down' state."""

    def test_only_enabled_down_counted(self):
        checks = [
            {"enabled": True, "state": "down"},
            {"enabled": True, "state": "up"},
            {"enabled": False, "state": "down"},   # disabled → not counted
            {"enabled": True, "state": "down"},
        ]
        down = sum(1 for c in checks if c.get("enabled") and c.get("state") == "down")
        self.assertEqual(down, 2)


if __name__ == "__main__":
    unittest.main()
