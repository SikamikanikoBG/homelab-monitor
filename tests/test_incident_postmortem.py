"""Tests for the AI incident postmortem layer (E1) — the capstone of the
incidents+AI thread.

Covers the non-negotiable invariants for this AI slice:
  • NO LLM on any poll path (/api/data, /api/incidents, /api/incidents/<id>
    default view, incident evaluation) — a call-counter must stay at 0.
  • The postmortem is built from REAL incident data: the deterministic timeline /
    duration / member list come from the DB, never the LLM.
  • At-most-once atomic claim: a resolution triggers exactly one generation, never
    a storm; regeneration only on explicit force.
  • Resolved-only: an OPEN incident has no postmortem (resolved:false, null).
  • Graceful degrade: LLM off/garbage → no prose, deterministic skeleton still
    renders, honest llm_status, never a 500.
  • Never on the public surface: the postmortem prose must not reach /api/status.
  • Never mutates the incident lifecycle / host.
  • Additive, idempotent migration.
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


def _open_incident(*keys, at=1_000_000):
    if not keys:
        keys = ("gpu_util", "gpu_power")
    return app.evaluate_incidents(_bundle(*keys), at)


def _resolve(iid, start=1_000_100):
    """Drive the open incident to the 'cleared' state via the clear-debounce."""
    for i in range(app._INCIDENT_CLEAR_CONFIRM + 1):
        app.evaluate_incidents(_bundle(), start + i)
    return app.get_incident(iid)


def _pm_gen(cause="A model load spiked GPU power and utilisation together.",
            impact="Inference latency likely rose briefly.",
            action="Cap concurrent model loads.", counter=None):
    def gen(prompt, timeout=None, capture=None, fmt=None):
        if counter is not None:
            counter["n"] += 1
        return json.dumps({"probable_cause": cause, "impact": impact,
                           "recommended_action": action}), None
    return gen


def _unreachable(counter=None):
    def gen(prompt, timeout=None, capture=None, fmt=None):
        if counter is not None:
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
        for c in ("postmortem_json", "postmortem_at", "postmortem_model"):
            self.assertIn(c, cols)

    def test_migration_idempotent(self):
        app._apply_schema_migrations(app.DB)
        app._apply_schema_migrations(app.DB)
        cols = self._cols()
        for c in ("postmortem_json", "postmortem_at", "postmortem_model"):
            self.assertIn(c, cols)

    def test_default_off(self):
        self.assertEqual(app.SETTING_DEFAULTS["incident_auto_postmortem"], "0")
        self.assertEqual(app.get_settings()["incident_auto_postmortem"], "0")


class TestNoLLMOnPoll(unittest.TestCase):
    """The core constraint: the incident read + evaluation poll paths NEVER call
    the LLM. Every generate is monkeypatched to a counter and asserted 0."""

    def setUp(self):
        _clean()
        self.c = app.app.test_client()
        self._orig = app._ollama_generate
        self.calls = {"n": 0}
        app._ollama_generate = _unreachable(self.calls)

    def tearDown(self):
        app._ollama_generate = self._orig

    def test_resolution_never_calls_llm_default_off(self):
        iid = _open_incident("gpu_util", "gpu_power")
        _resolve(iid)   # transitions to cleared on the poll path
        self.assertEqual(self.calls["n"], 0)

    def test_api_incident_one_of_resolved_zero_llm(self):
        iid = _open_incident("gpu_util", "gpu_power")
        _resolve(iid)
        r = self.c.get("/api/incidents/" + iid)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.calls["n"], 0)

    def test_get_postmortem_default_get_zero_llm(self):
        iid = _open_incident("gpu_util", "gpu_power")
        _resolve(iid)
        r = self.c.get("/api/incidents/" + iid + "/postmortem")
        self.assertEqual(r.status_code, 200)
        # A plain GET (no ?generate) is cache-only — never touches the LLM.
        self.assertEqual(self.calls["n"], 0)
        j = r.get_json()
        self.assertTrue(j["resolved"])
        self.assertIsNone(j["postmortem"]["probable_cause"])
        self.assertEqual(j["llm_status"], "ungenerated")

    def test_resolution_never_calls_llm_even_when_opt_in_on(self):
        # Opt-in ON but the auto path only ENQUEUES — the poll itself must be
        # LLM-free (the worker is stubbed out so nothing drains the queue here).
        app.save_settings({"incident_auto_postmortem": "1"})
        app._INCIDENT_PM_Q = queue.Queue()
        app._INCIDENT_PM_QUEUED = set()
        _orig = app._ensure_incident_postmortem_worker
        app._ensure_incident_postmortem_worker = lambda: None
        try:
            iid = _open_incident("gpu_util", "gpu_power")
            _resolve(iid)
            self.assertEqual(self.calls["n"], 0)
            # …but the resolved id WAS enqueued for the off-poll worker.
            self.assertIn(iid, app._INCIDENT_PM_QUEUED)
        finally:
            app._ensure_incident_postmortem_worker = _orig


class TestDeterministicFacts(unittest.TestCase):
    """The postmortem is built from REAL incident data: timeline/duration/members
    come from the DB, not the LLM."""

    def setUp(self):
        _clean()
        self._orig = app._ollama_generate
        app._ollama_generate = _pm_gen()

    def tearDown(self):
        app._ollama_generate = self._orig

    def test_deterministic_timeline_duration_members(self):
        iid = _open_incident("gpu_util", "gpu_power", at=1_000_000)
        _resolve(iid, start=1_003_600)   # ~1h later
        res = app.get_incident_postmortem(iid, generate=True)
        det = res["postmortem"]["deterministic"]
        # duration derived purely from opened_at → cleared_at
        self.assertIsNotNone(det["duration_s"])
        self.assertGreater(det["duration_s"], 3000)
        self.assertEqual(det["member_count"], 2)
        series = {m["series"] for m in det["members"]}
        self.assertEqual(series, {"gpu_util", "gpu_power"})
        # timeline has opened + member joins + cleared
        events = {e["event"] for e in det["timeline"]}
        self.assertIn("opened", events)
        self.assertIn("cleared", events)

    def test_facts_grounding_uses_member_labels_only(self):
        iid = _open_incident("gpu_util", "gpu_power")
        inc = _resolve(iid)
        det = app._incident_postmortem_facts_deterministic(inc)
        facts = app._incident_postmortem_grounding(inc, det)
        blob = "\n".join(facts)
        # series keys / labels only, and no invented topology
        self.assertIn("resolved", blob.lower())
        self.assertNotIn("SELECT", blob)


class TestGeneration(unittest.TestCase):
    def setUp(self):
        _clean()
        self.c = app.app.test_client()
        self._orig = app._ollama_generate
        self.calls = {"n": 0}
        app._ollama_generate = _pm_gen(counter=self.calls)

    def tearDown(self):
        app._ollama_generate = self._orig

    def test_generate_persists_prose(self):
        iid = _open_incident("gpu_util", "gpu_power")
        _resolve(iid)
        r = self.c.post("/api/incidents/" + iid + "/postmortem")
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertTrue(j["resolved"])
        pm = j["postmortem"]
        self.assertTrue(pm["generated"])
        self.assertIn("GPU power", pm["probable_cause"])
        self.assertIn("latency", pm["impact"])
        self.assertTrue(pm["recommended_action"])
        self.assertGreaterEqual(self.calls["n"], 1)
        # Persisted: a subsequent plain GET returns it with ZERO new LLM call.
        n = self.calls["n"]
        r2 = self.c.get("/api/incidents/" + iid + "/postmortem")
        j2 = r2.get_json()
        self.assertTrue(j2["postmortem"]["generated"])
        self.assertTrue(j2.get("cached"))
        self.assertEqual(self.calls["n"], n)

    def test_at_most_once_no_storm(self):
        iid = _open_incident("gpu_util", "gpu_power")
        _resolve(iid)
        # First generate persists; a second (non-force) generate must NOT re-call
        # the model (cache hit) — at-most-once.
        app.get_incident_postmortem(iid, generate=True)
        n = self.calls["n"]
        app.get_incident_postmortem(iid, generate=True)   # cache hit
        self.assertEqual(self.calls["n"], n)

    def test_regenerate_forces_new_generation(self):
        iid = _open_incident("gpu_util", "gpu_power")
        _resolve(iid)
        self.c.post("/api/incidents/" + iid + "/postmortem")
        n = self.calls["n"]
        app._ollama_generate = _pm_gen(cause="A different resolved cause.",
                                       counter=self.calls)
        r = self.c.post("/api/incidents/" + iid + "/postmortem",
                        json={"regenerate": True})
        j = r.get_json()
        self.assertGreater(self.calls["n"], n)
        self.assertIn("different resolved cause",
                      j["postmortem"]["probable_cause"].lower())

    def test_unknown_incident_404(self):
        r = self.c.post("/api/incidents/does-not-exist/postmortem")
        self.assertEqual(r.status_code, 404)


class TestResolvedOnly(unittest.TestCase):
    def setUp(self):
        _clean()
        self.c = app.app.test_client()
        self._orig = app._ollama_generate
        self.calls = {"n": 0}
        app._ollama_generate = _pm_gen(counter=self.calls)

    def tearDown(self):
        app._ollama_generate = self._orig

    def test_open_incident_has_no_postmortem(self):
        iid = _open_incident("gpu_util", "gpu_power")   # still OPEN
        # Even an explicit POST generate on an open incident is a clean null — never
        # an LLM call.
        r = self.c.post("/api/incidents/" + iid + "/postmortem")
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertFalse(j["resolved"])
        self.assertIsNone(j["postmortem"])
        self.assertEqual(self.calls["n"], 0)


class TestGracefulDegrade(unittest.TestCase):
    def setUp(self):
        _clean()
        self.c = app.app.test_client()
        self._orig = app._ollama_generate
        self.calls = {"n": 0}
        app._ollama_generate = _unreachable(self.calls)

    def tearDown(self):
        app._ollama_generate = self._orig

    def test_llm_down_skeleton_only_no_persist_no_500(self):
        iid = _open_incident("gpu_util", "gpu_power")
        _resolve(iid)
        r = self.c.post("/api/incidents/" + iid + "/postmortem")
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertTrue(j["resolved"])
        pm = j["postmortem"]
        self.assertFalse(pm["generated"])
        self.assertIsNone(pm["probable_cause"])
        # deterministic skeleton STILL renders
        self.assertIsNotNone(pm["deterministic"]["duration_s"])
        self.assertEqual(pm["deterministic"]["member_count"], 2)
        self.assertEqual(j["llm_status"], "unreachable")
        # nothing persisted
        self.assertIsNone(app.get_incident(iid).get("postmortem_json"))

    def test_garbage_json_degrades(self):
        def gen(prompt, timeout=None, capture=None, fmt=None):
            self.calls["n"] += 1
            return "not json at all", None
        app._ollama_generate = gen
        iid = _open_incident("gpu_util", "gpu_power")
        _resolve(iid)
        j = self.c.post("/api/incidents/" + iid + "/postmortem").get_json()
        self.assertFalse(j["postmortem"]["generated"])
        self.assertIsNone(app.get_incident(iid).get("postmortem_json"))


class TestNoLifecycleMutation(unittest.TestCase):
    def setUp(self):
        _clean()
        self._orig = app._ollama_generate
        app._ollama_generate = _pm_gen()

    def tearDown(self):
        app._ollama_generate = self._orig

    def test_generation_never_changes_lifecycle(self):
        iid = _open_incident("gpu_util", "gpu_power")
        inc0 = _resolve(iid)
        before = (inc0["state"], inc0["cleared_at"], inc0["opened_at"], inc0["severity"])
        app.get_incident_postmortem(iid, generate=True, force=True)
        inc1 = app.get_incident(iid)
        after = (inc1["state"], inc1["cleared_at"], inc1["opened_at"], inc1["severity"])
        self.assertEqual(before, after)
        self.assertEqual(inc1["state"], "cleared")


class TestPublicPrivacy(unittest.TestCase):
    """The postmortem prose lives ONLY on the authed dashboard; it must never
    appear on the unauthenticated public /status surface."""

    def setUp(self):
        _clean()
        self.c = app.app.test_client()
        self._sp = app.STATUS_PAGE
        app.STATUS_PAGE = True
        self._orig = app._ollama_generate
        self.marker = "SECRET-POSTMORTEM-MARKER-xyz"
        app._ollama_generate = _pm_gen(cause=self.marker, impact=self.marker,
                                       action=self.marker)
        self.iid = _open_incident("gpu_util", "gpu_power")
        _resolve(self.iid)
        app.get_incident_postmortem(self.iid, generate=True)

    def tearDown(self):
        app.STATUS_PAGE = self._sp
        app._ollama_generate = self._orig

    def test_marker_absent_from_public_status(self):
        r = self.c.get("/api/status")
        self.assertNotIn(self.marker, r.get_data(as_text=True))

    def test_marker_absent_from_default_incident_read(self):
        # The default drawer poll (GET /api/incidents/<id>) never ships the prose.
        r = self.c.get("/api/incidents/" + self.iid)
        self.assertNotIn(self.marker, r.get_data(as_text=True))
        self.assertNotIn("postmortem_json", r.get_data(as_text=True))
        # but it does carry the glanceable flag
        self.assertTrue(r.get_json()["incident"]["has_postmortem"])

    def test_marker_present_on_authed_postmortem_endpoint(self):
        r = self.c.get("/api/incidents/" + self.iid + "/postmortem")
        self.assertIn(self.marker, r.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
