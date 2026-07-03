"""Tests for the Lab Health Score — a deterministic, LLM-free 0-100 index over the
signals the app already produces, plus its explainable breakdown.

Coverage:
  • a quiet, healthy lab scores ~100 with an empty ("all clear") breakdown and NO
    fabricated penalties from absent signals;
  • each category deducts for a seeded bad condition (down check, open incident,
    active anomaly, imminent disk/VRAM ETA, hot/throttling GPU, SLO burn/over-budget);
  • the breakdown reconciles exactly: score == clamp(100 + Σ deltas, 0, 100);
  • the score clamps to [0, 100] under many simultaneous problems;
  • NULL / absent signals never penalize or crash;
  • the score API + /api/forecast make ZERO LLM calls (monkeypatched generate);
  • the score is NOT exposed on the public /status surfaces (authed only).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


def _clamp(x):
    return max(0, min(100, x))


def _reconciles(res):
    total = sum(f["delta"] for f in res["factors"])
    return res["score"] == _clamp(100 + total)


class TestHealthyLab(unittest.TestCase):
    def test_empty_signals_is_100_all_clear(self):
        r = app.compute_health_score({})
        self.assertEqual(r["score"], 100)
        self.assertEqual(r["band"], "excellent")
        self.assertEqual(r["tier"], "ok")
        self.assertEqual(r["factors"], [])
        self.assertTrue(_reconciles(r))

    def test_quiet_lab_no_fabricated_penalties(self):
        # Every signal present but BENIGN: up checks, no incidents, quiet anomaly
        # detector, stable disks/VRAM, cool GPU. Must stay 100 with no factors.
        sig = {
            "uptime": [{"enabled": True, "state": "up",
                        "slo": {"data_sufficient": True, "over_budget": False, "burning": False}},
                       {"enabled": False, "state": "down"}],  # disabled → ignored
            "incidents": {"open": 0, "critical": 0},
            "anomalies": {"status": "quiet", "items": []},
            "disk": [{"mount": "/", "status": "stable", "eta_days": None},
                     {"mount": "/data", "status": "collecting", "eta_days": None}],
            "vram": {"status": "stable", "eta_min": None},
            "gpu": {"temp": 51, "throttled": False, "gpus": [{"temp": 51}]},
        }
        r = app.compute_health_score(sig)
        self.assertEqual(r["score"], 100)
        self.assertEqual(r["factors"], [])
        self.assertTrue(_reconciles(r))


class TestCategoryPenalties(unittest.TestCase):
    def _keys(self, r):
        return {f["key"] for f in r["factors"]}

    def _factor(self, r, key):
        return next(f for f in r["factors"] if f["key"] == key)

    def test_down_check_penalizes_uptime(self):
        r = app.compute_health_score({"uptime": [
            {"enabled": True, "state": "down"},
            {"enabled": True, "state": "up"}]})
        self.assertIn("uptime", self._keys(r))
        self.assertEqual(self._factor(r, "uptime")["delta"], -20)
        self.assertLess(r["score"], 100)
        self.assertTrue(_reconciles(r))

    def test_open_incident_penalizes(self):
        r = app.compute_health_score({"incidents": {"open": 1, "critical": 0}})
        self.assertIn("incidents", self._keys(r))
        self.assertTrue(_reconciles(r))
        # a critical open incident hurts more than a non-critical one
        r2 = app.compute_health_score({"incidents": {"open": 1, "critical": 1}})
        self.assertLess(self._factor(r2, "incidents")["delta"],
                        self._factor(r, "incidents")["delta"])

    def test_active_anomaly_penalizes(self):
        r = app.compute_health_score({"anomalies": {"items": [{"key": "gpu_temp"}]}})
        self.assertIn("anomalies", self._keys(r))
        self.assertTrue(_reconciles(r))

    def test_imminent_disk_eta_penalizes_more_than_far(self):
        near = app.compute_health_score({"disk": [
            {"mount": "/", "status": "filling", "eta_days": 0.5}]})
        far = app.compute_health_score({"disk": [
            {"mount": "/", "status": "filling", "eta_days": 25}]})
        self.assertIn("disk", self._keys(near))
        self.assertLess(self._factor(near, "disk")["delta"],
                        self._factor(far, "disk")["delta"])
        self.assertTrue(_reconciles(near))

    def test_full_disk_hits_cap(self):
        r = app.compute_health_score({"disk": [{"mount": "/", "status": "full"}]})
        self.assertEqual(self._factor(r, "disk")["delta"], -app._HS_CAPS["disk"])

    def test_imminent_vram_eta_penalizes(self):
        r = app.compute_health_score({"vram": {"status": "filling", "eta_min": 20}})
        self.assertIn("vram", self._keys(r))
        self.assertTrue(_reconciles(r))

    def test_hot_gpu_penalizes(self):
        cool = app.compute_health_score({"gpu": {"temp": 60}})
        hot = app.compute_health_score({"gpu": {"temp": 91}})
        self.assertEqual(cool["factors"], [])
        self.assertIn("thermal", {f["key"] for f in hot["factors"]})
        self.assertEqual(self._factor(hot, "thermal")["delta"], -app._HS_CAPS["thermal"])

    def test_throttling_gpu_penalizes_without_temp(self):
        r = app.compute_health_score({"gpu": {"temp": None, "throttled": True}})
        self.assertIn("thermal", self._keys(r))

    def test_power_cap_at_cool_temp_is_not_a_thermal_penalty(self):
        # A routine power/util cap on a COOL, idle GPU sitting at its configured
        # power limit is normal operation, NOT a health problem — it must NOT deduct
        # under the thermal category (else the demo score is understated).
        for reasons in (["Power cap"], ["Power brake"], ["Power cap", "Power brake"]):
            r = app.compute_health_score({"gpu": {
                "temp": 43, "throttled": True, "throttle": reasons}})
            self.assertEqual(r["factors"], [], reasons)
            self.assertEqual(r["score"], 100, reasons)

    def test_thermal_throttle_reason_penalizes_even_when_not_yet_hot(self):
        # An explicitly-thermal slowdown IS a real signal and must deduct, even when
        # the reported temp is below the temperature bands.
        r = app.compute_health_score({"gpu": {
            "temp": 60, "throttled": True, "throttle": ["HW thermal"]}})
        self.assertIn("thermal", self._keys(r))
        self.assertEqual(self._factor(r, "thermal")["delta"], -9)
        self.assertTrue(_reconciles(r))

    def test_slo_over_budget_and_burn_penalize(self):
        r = app.compute_health_score({"uptime": [
            {"enabled": True, "state": "up",
             "slo": {"data_sufficient": True, "over_budget": True, "burning": True}}]})
        self.assertIn("slo", self._keys(r))
        self.assertTrue(_reconciles(r))

    def test_slo_ignored_when_data_insufficient(self):
        # A sparse SLO window must NOT invent a penalty (no fabricated deduction).
        r = app.compute_health_score({"uptime": [
            {"enabled": True, "state": "up",
             "slo": {"data_sufficient": False, "over_budget": True, "burning": True}}]})
        self.assertNotIn("slo", self._keys(r))
        self.assertEqual(r["score"], 100)


class TestReconcileAndClamp(unittest.TestCase):
    def test_many_problems_reconcile_and_clamp_to_zero(self):
        sig = {
            "uptime": [{"enabled": True, "state": "down"},
                       {"enabled": True, "state": "down"},
                       {"enabled": True, "state": "down",
                        "slo": {"data_sufficient": True, "over_budget": True, "burning": True}}],
            "incidents": {"open": 3, "critical": 2},
            "anomalies": {"items": [{"key": "a"}, {"key": "b"}, {"key": "c"}, {"key": "d"}]},
            "disk": [{"mount": "/", "status": "full"}],
            "vram": {"status": "full"},
            "gpu": {"temp": 95, "throttled": True},
        }
        r = app.compute_health_score(sig)
        self.assertEqual(r["score"], 0)          # clamped, never negative
        self.assertEqual(r["band"], "at_risk")
        self.assertEqual(r["tier"], "crit")
        # Even clamped, the displayed score reconciles to clamp(100 + Σ deltas).
        self.assertTrue(_reconciles(r))
        total = sum(f["delta"] for f in r["factors"])
        self.assertLess(total, -100)             # deductions genuinely exceed 100

    def test_every_factor_delta_is_negative_and_within_cap(self):
        sig = {
            "uptime": [{"enabled": True, "state": "down"}],
            "incidents": {"open": 9, "critical": 9},
            "anomalies": {"items": [{"key": str(i)} for i in range(50)]},
            "disk": [{"mount": "/", "status": "full"}],
            "vram": {"status": "full"},
            "gpu": {"temp": 99, "throttled": True},
        }
        r = app.compute_health_score(sig)
        for f in r["factors"]:
            self.assertLess(f["delta"], 0)
            self.assertLessEqual(abs(f["delta"]), app._HS_CAPS[f["key"]])

    def test_factors_sorted_worst_first(self):
        sig = {"uptime": [{"enabled": True, "state": "down"}],
               "anomalies": {"items": [{"key": "x"}]}}
        r = app.compute_health_score(sig)
        deltas = [f["delta"] for f in r["factors"]]
        self.assertEqual(deltas, sorted(deltas))


class TestBands(unittest.TestCase):
    def test_band_thresholds(self):
        self.assertEqual(app._hs_band(100), ("excellent", "ok"))
        self.assertEqual(app._hs_band(90), ("excellent", "ok"))
        self.assertEqual(app._hs_band(89), ("good", "ok"))
        self.assertEqual(app._hs_band(75), ("good", "ok"))
        self.assertEqual(app._hs_band(74), ("fair", "warn"))
        self.assertEqual(app._hs_band(50), ("fair", "warn"))
        self.assertEqual(app._hs_band(49), ("at_risk", "crit"))
        self.assertEqual(app._hs_band(0), ("at_risk", "crit"))


class TestNullSafety(unittest.TestCase):
    def test_none_signals_do_not_crash_or_penalize(self):
        r = app.compute_health_score({
            "uptime": None, "incidents": None, "anomalies": None,
            "disk": None, "vram": None, "gpu": None})
        self.assertEqual(r["score"], 100)
        self.assertEqual(r["factors"], [])

    def test_garbage_shapes_do_not_crash(self):
        r = app.compute_health_score({
            "uptime": ["not-a-dict", None, 5],
            "incidents": {"open": "x"},          # unparseable → treated as 0
            "anomalies": {"items": "nope"},
            "disk": [None, 7, {"status": "filling", "eta_days": None}],
            "vram": {"status": "filling", "eta_min": None},
            "gpu": {"temp": "hot", "gpus": ["bad", {"temp": None}]}})
        self.assertIsInstance(r["score"], int)
        self.assertTrue(_reconciles(r))

    def test_missing_gpu_never_dings_thermal(self):
        # A GPU-less host (empty gpu signal) must not be penalized for thermals.
        r = app.compute_health_score({"gpu": {}})
        self.assertEqual(r["factors"], [])


class _LLMGuard:
    """Monkeypatch every LLM entry point to raise/track, proving zero calls."""
    def __init__(self):
        self.calls = 0

    def __enter__(self):
        self._orig_gen = app._ollama_generate
        self._orig_stream = app._ollama_generate_stream

        def _boom(*a, **k):
            self.calls += 1
            raise AssertionError("LLM must not be called on the health-score path")

        app._ollama_generate = _boom
        app._ollama_generate_stream = _boom
        return self

    def __exit__(self, *exc):
        app._ollama_generate = self._orig_gen
        app._ollama_generate_stream = self._orig_stream


class TestApiEndpoints(unittest.TestCase):
    def setUp(self):
        self.c = app.app.test_client()

    def test_health_score_endpoint_shape_and_reconcile(self):
        with _LLMGuard() as g:
            r = self.c.get("/api/health_score")
            self.assertEqual(r.status_code, 200)
            j = r.get_json()
            self.assertIn("score", j)
            self.assertIn("band", j)
            self.assertIn("factors", j)
            self.assertIn("caps", j)
            self.assertTrue(0 <= j["score"] <= 100)
            self.assertTrue(_reconciles(j))
            self.assertEqual(g.calls, 0)

    def test_forecast_embeds_health_and_is_llm_free(self):
        with _LLMGuard() as g:
            r = self.c.get("/api/forecast")
            self.assertEqual(r.status_code, 200)
            j = r.get_json()
            self.assertIn("health", j)
            self.assertIn("score", j["health"])
            self.assertTrue(_reconciles(j["health"]))
            self.assertEqual(g.calls, 0)

    def test_score_not_on_public_status(self):
        # The public status surfaces must not leak the score / its endpoint fields.
        for path in ("/api/status", "/status"):
            r = self.c.get(path)
            body = r.get_data(as_text=True)
            self.assertNotIn("health_score", body)
            self.assertNotIn("\"score\"", body)


if __name__ == "__main__":
    unittest.main()
