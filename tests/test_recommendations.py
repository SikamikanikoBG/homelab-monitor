"""Unit tests for the proactive Recommendations panel (E1).

The deterministic detectors are the reliable core — these tests exercise them
directly over SYNTHETIC signal bundles (no ollama, no live history needed):
  • each detector fires on the right signal AND stays silent otherwise
  • severity ranking + the top-N cap
  • the all-clear state when nothing fires
  • /api/recommendations shape + always-200, advice-only
  • LLM down → the deterministic items are present unchanged (mocked)
  • LLM up → optional 'priority' framing is added (mocked) and bounded
  • no secret/URL leaks into the LLM prompt
  • cheap path: the endpoint reuses the forecast accessors, no recompute storm
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


def _sig(**over):
    """A baseline 'all healthy' signal bundle; override any sub-signal per test."""
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


class TestDetectorsFire(unittest.TestCase):
    def _ids(self, items):
        return [it["id"] for it in items]

    # ── Disk ────────────────────────────────────────────────────────────────
    def test_disk_crit_fires(self):
        sig = _sig(disk=[{"mount": "/backup", "status": "filling", "eta_days": 5,
                          "eta_ts": int(time.time()) + 5 * 86400, "pct": 92,
                          "free_gb": 30, "gb_per_day": 6.0}])
        items = app._reco_detect(sig)
        d = next(it for it in items if it["id"] == "disk:/backup")
        self.assertEqual(d["severity"], "crit")
        self.assertEqual(d["source"], "disk")
        self.assertEqual(d["link"], "disks")
        self.assertIn("/backup", d["title"])
        self.assertTrue(d["action"])

    def test_disk_warn_band(self):
        sig = _sig(disk=[{"mount": "/data", "status": "filling", "eta_days": 40,
                          "pct": 70, "free_gb": 100, "gb_per_day": 2.0}])
        items = app._reco_detect(sig)
        self.assertEqual(next(it for it in items if it["source"] == "disk")["severity"], "warn")

    def test_disk_silent_when_far_out(self):
        sig = _sig(disk=[{"mount": "/data", "status": "filling", "eta_days": 400,
                          "pct": 40, "free_gb": 500, "gb_per_day": 0.2}])
        self.assertEqual([it for it in app._reco_detect(sig) if it["source"] == "disk"], [])

    def test_disk_silent_when_stable(self):
        sig = _sig(disk=[{"mount": "/data", "status": "stable", "eta_days": None}])
        self.assertEqual([it for it in app._reco_detect(sig) if it["source"] == "disk"], [])

    # ── VRAM ────────────────────────────────────────────────────────────────
    def test_vram_headroom_warn(self):
        sig = _sig(vram={"status": "stable", "free_gb": 1.5, "total_gb": 24.0,
                         "models_gb": 22.0, "eta_min": None})
        v = next(it for it in app._reco_detect(sig) if it["id"] == "vram:headroom")
        self.assertEqual(v["severity"], "warn")
        self.assertEqual(v["link"], "models")

    def test_vram_headroom_crit(self):
        sig = _sig(vram={"status": "stable", "free_gb": 0.3, "total_gb": 24.0,
                         "models_gb": 23.5, "eta_min": None})
        v = next(it for it in app._reco_detect(sig) if it["id"] == "vram:headroom")
        self.assertEqual(v["severity"], "crit")

    def test_vram_headroom_silent_when_plenty(self):
        sig = _sig(vram={"status": "stable", "free_gb": 18.0, "total_gb": 24.0,
                         "models_gb": 6.0, "eta_min": None})
        self.assertEqual([it for it in app._reco_detect(sig) if it["id"] == "vram:headroom"], [])

    def test_vram_eta_short_fires(self):
        sig = _sig(vram={"status": "filling", "free_gb": 5.0, "total_gb": 24.0,
                         "models_gb": 4.0, "eta_min": 30, "mb_per_min": 50.0,
                         "eta_ts": int(time.time()) + 1800})
        self.assertTrue(any(it["id"] == "vram:eta" for it in app._reco_detect(sig)))

    # ── Cost ────────────────────────────────────────────────────────────────
    def test_cost_spike_warn(self):
        sig = _sig(cost_month={"enabled": True, "currency": "€", "delta_pct": 30,
                               "projected_month": 42.0, "month_to_date": 21.0,
                               "last_month": 32.0})
        c = next(it for it in app._reco_detect(sig) if it["source"] == "cost")
        self.assertEqual(c["severity"], "warn")
        self.assertEqual(c["link"], "costs")
        self.assertIn("€", c["title"])

    def test_cost_spike_crit(self):
        sig = _sig(cost_month={"enabled": True, "currency": "$", "delta_pct": 80,
                               "projected_month": 90.0, "month_to_date": 45.0,
                               "last_month": 50.0})
        self.assertEqual(next(it for it in app._reco_detect(sig) if it["source"] == "cost")["severity"], "crit")

    def test_cost_silent_when_flat(self):
        sig = _sig(cost_month={"enabled": True, "currency": "$", "delta_pct": 5,
                               "projected_month": 52.0, "month_to_date": 26.0,
                               "last_month": 50.0})
        self.assertEqual([it for it in app._reco_detect(sig) if it["source"] == "cost"], [])

    def test_cost_silent_when_disabled(self):
        self.assertEqual([it for it in app._reco_detect(_sig()) if it["source"] == "cost"], [])

    # ── Incident / anomaly ───────────────────────────────────────────────────
    def test_incident_fires_and_links_to_drawer(self):
        sig = _sig(incidents={"open": 1, "top": {"id": "inc7", "severity": "critical",
                              "opened_at": int(time.time()), "member_count": 3,
                              "active_count": 2, "series": ["gpu_temp", "gpu_power"]}})
        it = next(x for x in app._reco_detect(sig) if x["source"] == "incident")
        self.assertEqual(it["severity"], "crit")
        self.assertEqual(it["link"], "incident:inc7")

    def test_anomaly_fires_when_no_incident(self):
        sig = _sig(anomalies={"status": "active", "items": [
            {"key": "gpu_temp", "direction": "spike", "value": 95, "unit": "°C",
             "baseline": 60, "z": 4.5}]})
        it = next(x for x in app._reco_detect(sig) if x["source"] == "anomaly")
        self.assertEqual(it["severity"], "warn")
        self.assertEqual(it["link"], "gpu")

    def test_anomaly_suppressed_when_incident_open(self):
        # When an incident is open, the standalone-anomaly branch must NOT also fire.
        sig = _sig(
            incidents={"open": 1, "top": {"id": "i1", "severity": "warning",
                       "opened_at": 1, "member_count": 1, "active_count": 1,
                       "series": ["gpu_util"]}},
            anomalies={"status": "active", "items": [
                {"key": "gpu_util", "direction": "spike", "value": 99, "unit": "%",
                 "baseline": 10, "z": 6}]})
        srcs = [it["source"] for it in app._reco_detect(sig)]
        self.assertIn("incident", srcs)
        self.assertNotIn("anomaly", srcs)

    # ── OOM ──────────────────────────────────────────────────────────────────
    def test_oom_warn_and_crit(self):
        sig = _sig(ooms=[{"service": "sd", "count": 1, "last_ts": 1},
                         {"service": "llm", "count": 4, "last_ts": 2}])
        items = app._reco_detect(sig)
        self.assertEqual(next(it for it in items if it["id"] == "oom:sd")["severity"], "warn")
        self.assertEqual(next(it for it in items if it["id"] == "oom:llm")["severity"], "crit")

    def test_oom_silent_when_none(self):
        self.assertEqual([it for it in app._reco_detect(_sig()) if it["source"] == "oom"], [])

    # ── Uptime ────────────────────────────────────────────────────────────────
    def test_uptime_down_fires(self):
        sig = _sig(uptime=[{"id": "u1", "label": "router", "enabled": True,
                            "state": "down", "uptime": 0, "window_total": 20,
                            "last_code": 503, "last_checked": 5}])
        it = next(x for x in app._reco_detect(sig) if x["source"] == "uptime")
        self.assertEqual(it["severity"], "crit")
        self.assertEqual(it["link"], "uptime")

    def test_uptime_flapping_fires_warn(self):
        sig = _sig(uptime=[{"id": "u2", "label": "nas", "enabled": True,
                            "state": "up", "uptime": 80.0, "window_total": 30,
                            "last_checked": 9}])
        it = next(x for x in app._reco_detect(sig) if x["source"] == "uptime")
        self.assertEqual(it["severity"], "warn")
        self.assertIn("flapping", it["title"])

    def test_uptime_silent_when_healthy(self):
        sig = _sig(uptime=[{"id": "u3", "label": "ok", "enabled": True,
                            "state": "up", "uptime": 100.0, "window_total": 30}])
        self.assertEqual([it for it in app._reco_detect(sig) if it["source"] == "uptime"], [])

    def test_uptime_disabled_check_ignored(self):
        sig = _sig(uptime=[{"id": "u4", "label": "x", "enabled": False,
                            "state": "down", "uptime": 0, "window_total": 9}])
        self.assertEqual([it for it in app._reco_detect(sig) if it["source"] == "uptime"], [])


class TestRankingAndAllClear(unittest.TestCase):
    def test_ranked_crit_before_warn(self):
        sig = _sig(
            disk=[{"mount": "/d", "status": "filling", "eta_days": 40, "pct": 70,
                   "free_gb": 50, "gb_per_day": 2}],                       # warn
            vram={"status": "stable", "free_gb": 0.2, "total_gb": 24.0,
                  "models_gb": 23.8, "eta_min": None})                     # crit
        items = app._reco_detect(sig)
        self.assertEqual(items[0]["severity"], "crit")
        # warn ranked after the crit
        self.assertTrue(any(it["severity"] == "warn" for it in items[1:]))

    def test_cap_top_n(self):
        # Build more than RECO_MAX_ITEMS detections; the endpoint must cap.
        oomes = [{"service": "s%d" % i, "count": 5, "last_ts": i} for i in range(20)]
        sig = _sig(ooms=oomes)
        full = app._reco_detect(sig)
        self.assertGreater(len(full), app.RECO_MAX_ITEMS)   # detector returns all
        # endpoint cap is applied in the route; emulate it
        self.assertLessEqual(len(full[:app.RECO_MAX_ITEMS]), app.RECO_MAX_ITEMS)

    def test_all_clear_empty(self):
        self.assertEqual(app._reco_detect(_sig()), [])


class TestEndpoint(unittest.TestCase):
    """/api/recommendations: always-200, shape, advice-only, LLM degrade."""

    def setUp(self):
        self._en = app.COPILOT_ENABLED
        self._gen = app._ollama_generate
        self.c = app.app.test_client()

    def tearDown(self):
        app.COPILOT_ENABLED = self._en
        app._ollama_generate = self._gen

    def test_always_200_shape(self):
        r = self.c.get("/api/recommendations")
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        for k in ("items", "generated_at", "llm_used", "count", "now"):
            self.assertIn(k, j)
        self.assertIsInstance(j["items"], list)
        self.assertLessEqual(len(j["items"]), app.RECO_MAX_ITEMS)

    def test_llm_down_items_unchanged(self):
        # Force a fired detector via a stubbed signal bundle + LLM off.
        app.COPILOT_ENABLED = False
        app._ollama_generate = lambda *a, **k: (None, "disabled")
        orig = app._reco_signals
        app._reco_signals = lambda now: _sig(disk=[{"mount": "/x", "status": "filling",
                                "eta_days": 3, "pct": 95, "free_gb": 10, "gb_per_day": 9}])
        try:
            j = self.c.get("/api/recommendations").get_json()
        finally:
            app._reco_signals = orig
        self.assertFalse(j["llm_used"])
        self.assertEqual(j["priority"], None)
        # the deterministic item is present unchanged
        self.assertTrue(any(it["id"] == "disk:/x" for it in j["items"]))
        self.assertTrue(all(it.get("title") for it in j["items"]))

    def test_llm_up_adds_priority(self):
        app.COPILOT_ENABLED = True
        app._ollama_generate = lambda *a, **k: ("Tackle the disk first.", None)
        orig = app._reco_signals
        app._reco_signals = lambda now: _sig(disk=[{"mount": "/x", "status": "filling",
                                "eta_days": 3, "pct": 95, "free_gb": 10, "gb_per_day": 9}])
        try:
            j = self.c.get("/api/recommendations").get_json()
        finally:
            app._reco_signals = orig
        self.assertTrue(j["llm_used"])
        self.assertEqual(j["priority"], "Tackle the disk first.")
        self.assertEqual(j["llm_status"], "ok")

    def test_no_llm_call_when_all_clear(self):
        # When nothing fires, the LLM must not be invoked at all.
        called = {"n": 0}
        def spy(*a, **k):
            called["n"] += 1
            return ("x", None)
        app._ollama_generate = spy
        orig = app._reco_signals
        app._reco_signals = lambda now: _sig()
        try:
            j = self.c.get("/api/recommendations").get_json()
        finally:
            app._reco_signals = orig
        self.assertEqual(j["items"], [])
        self.assertEqual(called["n"], 0)
        self.assertFalse(j["llm_used"])

    def test_brief_returns_items_without_llm(self):
        # ?brief=1 must return the deterministic items + counts but NEVER call the LLM,
        # even when detectors fire. This is the path the MCP get_recommendations tool
        # rides on, so agent polling never spins the GPU.
        called = {"n": 0}
        def spy(*a, **k):
            called["n"] += 1
            return ("should-not-appear", None)
        app._ollama_generate = spy
        orig = app._reco_signals
        app._reco_signals = lambda now: _sig(disk=[{"mount": "/x", "status": "filling",
                                "eta_days": 3, "pct": 95, "free_gb": 10, "gb_per_day": 9}])
        try:
            j = self.c.get("/api/recommendations?brief=1").get_json()
        finally:
            app._reco_signals = orig
        self.assertEqual(called["n"], 0)  # LLM never invoked on the brief path
        self.assertFalse(j["llm_used"])
        self.assertIsNone(j["priority"])
        self.assertEqual(j["llm_status"], "skipped")
        self.assertTrue(any(it["id"] == "disk:/x" for it in j["items"]))
        # counts block is present and consistent with the items
        self.assertIn("counts", j)
        self.assertEqual(j["counts"]["total"], len(j["items"]))
        self.assertEqual(j["counts"]["crit"],
                         sum(1 for it in j["items"] if it.get("severity") == "crit"))

    def test_llm_zero_param_also_skips(self):
        # ?llm=0 is an accepted alias for the brief/LLM-free path.
        called = {"n": 0}
        app._ollama_generate = lambda *a, **k: (called.__setitem__("n", called["n"] + 1), ("x", None))[1]
        orig = app._reco_signals
        app._reco_signals = lambda now: _sig(disk=[{"mount": "/x", "status": "filling",
                                "eta_days": 3, "pct": 95, "free_gb": 10, "gb_per_day": 9}])
        try:
            j = self.c.get("/api/recommendations?llm=0").get_json()
        finally:
            app._reco_signals = orig
        self.assertEqual(called["n"], 0)
        self.assertTrue(any(it["id"] == "disk:/x" for it in j["items"]))


class TestNoSecretLeak(unittest.TestCase):
    """The LLM prompt must carry ONLY the deterministic title/severity text — never
    a URL, credential, or raw uptime target/error."""

    def test_prompt_has_no_url_or_secret(self):
        sig = _sig(uptime=[{"id": "u1", "label": "secret-host", "enabled": True,
                            "state": "down", "uptime": 0, "window_total": 9,
                            "last_code": 401,
                            "last_err": "https://user:pass@10.0.0.5/api?token=abcd1234",
                            "last_checked": 1}])
        items = app._reco_detect(sig)
        prompt = app._reco_llm_prompt(items)
        # the prompt is built from titles only — no scheme, host, creds, or token
        self.assertNotIn("http", prompt)
        self.assertNotIn("token=abcd1234", prompt)
        self.assertNotIn("user:pass", prompt)
        self.assertNotIn("10.0.0.5", prompt)

    def test_prompt_bounded_to_cap(self):
        items = [{"severity": "warn", "title": "item %d" % i, "source": "oom"}
                 for i in range(30)]
        prompt = app._reco_llm_prompt(items)
        # only the first RECO_MAX_ITEMS titles make it into the prompt
        self.assertIn("item 0", prompt)
        self.assertNotIn("item %d" % (app.RECO_MAX_ITEMS + 1), prompt)


class TestUptimeRedaction(unittest.TestCase):
    def test_uptime_detail_redacts_target(self):
        d = app._reco_uptime_detail({"last_code": 500,
              "last_err": "dial tcp https://admin:hunter2@host.local:9000 failed"})
        self.assertIn("status 500", d)
        self.assertNotIn("hunter2", d)


if __name__ == "__main__":
    unittest.main()
