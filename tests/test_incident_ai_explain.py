"""Tests for the persisted, proactive incident AI-explanation layer (E1).

Covers the non-negotiable architecture constraint — the LLM NEVER runs on the
incident read / metrics poll path — plus the additive migration, the explicit
explain/regenerate action, aggressive caching, the OFF-by-default opt-in
auto-explain (which only ever fires off a dedicated, decoupled worker — never
from evaluate_incidents/collect/health_scan), graceful no-LLM degradation, the
bounded/sanitised persisted text, and the public-status privacy contract (the
correlated-incident AI explanation must not reach the unauthenticated /status).
"""
import json
import os
import queue
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


def _clean():
    with app.LOCK:
        app.DB.execute("DELETE FROM incidents")
        app.DB.execute("DELETE FROM incident_members")
        app.DB.execute("DELETE FROM settings")
        app.DB.commit()


def _bundle(*keys):
    items = [{"key": k, "unit": "W", "value": 300.0, "baseline": 200.0, "z": 4.0,
              "stddev": 25.0, "direction": "spike", "magnitude": 100.0, "samples": 40}
             for k in keys]
    return {"status": "quiet", "checked": 5, "threshold": 3.0, "window_h": 6, "items": items}


def _open_incident(*keys):
    """Open a fresh correlated incident and return its id."""
    if not keys:
        keys = ("gpu_util", "gpu_power")
    iid = app.evaluate_incidents(_bundle(*keys), 1_000_000)
    return iid


def _structured_gen(explanation="GPU utilisation and power spiked together.",
                    action="Check the busiest container.", counter=None):
    def gen(prompt, timeout=None, capture=None, fmt=None):
        if counter is not None:
            counter["n"] += 1
        return json.dumps({"explanation": explanation, "severity": "warning",
                           "action": action}), None
    return gen


def _counting_unreachable(counter):
    def gen(prompt, timeout=None, capture=None, fmt=None):
        counter["n"] += 1
        return None, "unreachable"
    return gen


class TestMigration(unittest.TestCase):
    def setUp(self):
        _clean()

    def _cols(self):
        return {r[1] for r in app.DB.execute("PRAGMA table_info(incidents)").fetchall()}

    def test_columns_present(self):
        cols = self._cols()
        for c in ("ai_explanation", "ai_explained_at", "ai_model"):
            self.assertIn(c, cols)

    def test_migration_idempotent(self):
        # Re-applying the schema migrations must be a no-op, never raise.
        app._apply_schema_migrations(app.DB)
        app._apply_schema_migrations(app.DB)
        cols = self._cols()
        for c in ("ai_explanation", "ai_explained_at", "ai_model"):
            self.assertIn(c, cols)


class TestReadPathNoLLM(unittest.TestCase):
    """The core constraint: incident READS + the incident-evaluation poll path
    NEVER call the LLM. Every generate must be monkeypatched to a call-counter and
    asserted NOT called on any of these paths."""

    def setUp(self):
        _clean()
        self.c = app.app.test_client()
        self._orig = app._ollama_generate
        self.calls = {"n": 0}
        app._ollama_generate = _counting_unreachable(self.calls)
        self.iid = _open_incident("gpu_util", "gpu_power")
        # Seed a persisted explanation directly (as a prior generation would have).
        with app.LOCK:
            app.DB.execute("UPDATE incidents SET ai_explanation=?, ai_explained_at=?, ai_model=? WHERE id=?",
                           ("CACHED cause text", 1_000_500, "gemma3:1b", self.iid))
            app.DB.commit()

    def tearDown(self):
        app._ollama_generate = self._orig

    def test_list_incidents_returns_cache_zero_llm(self):
        rows = app.list_incidents()
        self.assertEqual(rows[0]["ai_explanation"], "CACHED cause text")
        self.assertEqual(rows[0]["ai_model"], "gemma3:1b")
        self.assertEqual(self.calls["n"], 0)

    def test_get_incident_returns_cache_zero_llm(self):
        inc = app.get_incident(self.iid)
        self.assertEqual(inc["ai_explanation"], "CACHED cause text")
        self.assertEqual(self.calls["n"], 0)

    def test_api_incidents_zero_llm(self):
        r = self.c.get("/api/incidents")
        self.assertEqual(r.status_code, 200)
        self.assertIn("CACHED cause text", r.get_data(as_text=True))
        self.assertEqual(self.calls["n"], 0)

    def test_api_incident_one_zero_llm(self):
        r = self.c.get("/api/incidents/" + self.iid)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["incident"]["ai_explanation"], "CACHED cause text")
        self.assertEqual(self.calls["n"], 0)

    def test_evaluate_incidents_extend_and_clear_zero_llm(self):
        # Extend (another series joins) then drive to clear — the exact poll-path
        # calls the collector makes — and assert not one LLM call happened.
        app.evaluate_incidents(_bundle("gpu_util", "gpu_power", "gpu_temp"), 1_000_060)
        for i in range(app._INCIDENT_CLEAR_CONFIRM + 1):
            app.evaluate_incidents(_bundle(), 1_000_100 + i)
        self.assertEqual(self.calls["n"], 0)


class TestExplicitExplain(unittest.TestCase):
    def setUp(self):
        _clean()
        self.c = app.app.test_client()
        self._orig = app._ollama_generate
        self.calls = {"n": 0}
        app._ollama_generate = _structured_gen(counter=self.calls)
        self.iid = _open_incident("gpu_util", "gpu_power")

    def tearDown(self):
        app._ollama_generate = self._orig

    def test_explain_generates_and_persists(self):
        r = self.c.post("/api/incidents/" + self.iid + "/explain", json={})
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertEqual(j["source"], "llm")
        self.assertFalse(j["cached"])
        self.assertIn("spiked", j["explanation"])
        self.assertGreaterEqual(self.calls["n"], 1)
        # Persisted: a subsequent cache-only read carries it.
        inc = app.get_incident(self.iid)
        self.assertTrue(inc["ai_explanation"])
        self.assertEqual(inc["ai_model"], app.COPILOT_MODEL)
        self.assertIn("Suggested next step:", inc["ai_explanation"])

    def test_second_call_is_cache_hit_zero_new_llm(self):
        self.c.post("/api/incidents/" + self.iid + "/explain", json={})
        n_after_first = self.calls["n"]
        r = self.c.post("/api/incidents/" + self.iid + "/explain", json={})
        j = r.get_json()
        self.assertTrue(j["cached"])
        self.assertEqual(self.calls["n"], n_after_first)   # NO new generation

    def test_regenerate_forces_new_generation(self):
        self.c.post("/api/incidents/" + self.iid + "/explain", json={})
        n_after_first = self.calls["n"]
        app._ollama_generate = _structured_gen(explanation="Different cause now.",
                                               action="", counter=self.calls)
        r = self.c.post("/api/incidents/" + self.iid + "/explain",
                        json={"regenerate": True})
        j = r.get_json()
        self.assertFalse(j["cached"])
        self.assertGreater(self.calls["n"], n_after_first)
        self.assertIn("Different cause", app.get_incident(self.iid)["ai_explanation"])

    def test_unknown_incident_404(self):
        r = self.c.post("/api/incidents/does-not-exist/explain", json={})
        self.assertEqual(r.status_code, 404)


class TestGracefulNoLLM(unittest.TestCase):
    def setUp(self):
        _clean()
        self.c = app.app.test_client()
        self._orig = app._ollama_generate
        self.calls = {"n": 0}
        app._ollama_generate = _counting_unreachable(self.calls)
        self.iid = _open_incident("gpu_util", "gpu_power")

    def tearDown(self):
        app._ollama_generate = self._orig

    def test_llm_down_persists_nothing_and_degrades(self):
        r = self.c.post("/api/incidents/" + self.iid + "/explain", json={})
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertEqual(j["source"], "facts")
        self.assertEqual(j["llm_status"], "unreachable")
        self.assertNotIn("explanation", {k: v for k, v in j.items() if k == "explanation" and v})
        # Nothing persisted — the read path still returns no cached explanation.
        self.assertIsNone(app.get_incident(self.iid)["ai_explanation"])


class TestSanitize(unittest.TestCase):
    def test_collapses_and_bounds(self):
        self.assertIsNone(app._sanitize_explanation(""))
        self.assertIsNone(app._sanitize_explanation("   \n  "))
        self.assertIsNone(app._sanitize_explanation(None))
        self.assertEqual(app._sanitize_explanation("a\n\n  b\tc"), "a b c")
        long = "x" * 5000
        self.assertEqual(len(app._sanitize_explanation(long)), app._INCIDENT_EXPLAIN_MAX)


class TestAutoExplain(unittest.TestCase):
    def setUp(self):
        _clean()
        # Reset the dedicated worker queue/state between tests.
        app._INCIDENT_EXPLAIN_Q = queue.Queue()
        app._INCIDENT_EXPLAIN_QUEUED = set()
        self._ensure_orig = app._ensure_incident_explain_worker
        app._ensure_incident_explain_worker = lambda: None   # never spawn a real thread
        self._gen_orig = app._ollama_generate

    def tearDown(self):
        app._ensure_incident_explain_worker = self._ensure_orig
        app._ollama_generate = self._gen_orig

    def test_default_off(self):
        self.assertEqual(app.SETTING_DEFAULTS["incident_auto_explain"], "0")
        self.assertEqual(app.get_settings()["incident_auto_explain"], "0")

    def test_enqueue_noop_when_off(self):
        iid = _open_incident("gpu_util", "gpu_power")
        app._enqueue_incident_explain(iid)
        self.assertTrue(app._INCIDENT_EXPLAIN_Q.empty())
        self.assertEqual(app._INCIDENT_EXPLAIN_QUEUED, set())

    def test_enqueue_queues_when_on(self):
        app.save_settings({"incident_auto_explain": "1"})
        iid = _open_incident("gpu_util", "gpu_power")
        # evaluate_incidents already enqueued it on open (newly_opened path).
        self.assertIn(iid, app._INCIDENT_EXPLAIN_QUEUED)
        self.assertEqual(app._INCIDENT_EXPLAIN_Q.get_nowait(), iid)
        # De-dup: a second enqueue of the same id does not double-queue.
        app._enqueue_incident_explain(iid)
        self.assertTrue(app._INCIDENT_EXPLAIN_Q.empty())

    def test_evaluate_incidents_never_calls_llm_even_when_on(self):
        app.save_settings({"incident_auto_explain": "1"})
        calls = {"n": 0}
        app._ollama_generate = _counting_unreachable(calls)
        _open_incident("gpu_util", "gpu_power", "gpu_temp")
        # The open happened on the poll path → the LLM must NOT have been touched
        # synchronously; only an id was queued for the dedicated worker.
        self.assertEqual(calls["n"], 0)

    def test_worker_body_generates_via_generate_fn(self):
        # The worker's unit of work is generate_incident_explanation — exercise it
        # directly (that is exactly what the decoupled thread runs) and confirm it
        # persists off the read path.
        calls = {"n": 0}
        app._ollama_generate = _structured_gen(counter=calls)
        iid = _open_incident("gpu_util", "gpu_power")
        out = app.generate_incident_explanation(iid, force=False)
        self.assertEqual(out["source"], "llm")
        self.assertGreaterEqual(calls["n"], 1)
        self.assertTrue(app.get_incident(iid)["ai_explanation"])


class TestPublicStatusPrivacy(unittest.TestCase):
    """The correlated-incident AI explanation lives ONLY on the authenticated
    dashboard; it must never appear on the unauthenticated public /status."""

    def setUp(self):
        _clean()
        self.c = app.app.test_client()
        self._sp = app.STATUS_PAGE
        app.STATUS_PAGE = True
        self.iid = _open_incident("gpu_util", "gpu_power")
        self.marker = "SECRET-AI-CAUSE-MARKER-xyz"
        with app.LOCK:
            app.DB.execute("UPDATE incidents SET ai_explanation=? WHERE id=?", (self.marker, self.iid))
            app.DB.commit()

    def tearDown(self):
        app.STATUS_PAGE = self._sp

    def test_marker_absent_from_public_status(self):
        r = self.c.get("/api/status")
        # 200 (page on) or 404 (page off) — either way the marker must not appear.
        self.assertNotIn(self.marker, r.get_data(as_text=True))

    def test_marker_present_on_authed_incidents(self):
        r = self.c.get("/api/incidents")
        self.assertIn(self.marker, r.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
