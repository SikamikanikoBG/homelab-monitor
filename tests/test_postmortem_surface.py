"""Tests for SURFACING the AI incident postmortem (E1) — the discoverability +
turn-on-able + digest-citation slice on top of the existing postmortem layer.

Covers the three parts:
  PART 1 — the /api/incidents LIST carries has_postmortem for the badge/filter,
           WITHOUT a new per-incident query and WITHOUT any LLM call.
  PART 2 — the incident_auto_postmortem setting persists exactly like
           incident_auto_explain (same POST path) and defaults OFF.
  PART 3 — the NL digest cites the most-recent PERSISTED postmortem when one
           exists, omits it cleanly when absent, and adds NO new LLM call
           (the deterministic sections builder is LLM-free).
  PRIVACY — the postmortem prose stays off the public /status surface.
"""
import json
import os
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


class TestListBadgeFlag(unittest.TestCase):
    """PART 1 — the incidents LIST reflects has_postmortem for the badge/filter,
    reusing the existing flag (no new query) and with zero LLM on the list read."""

    def setUp(self):
        _clean()
        self.c = app.app.test_client()
        self._orig = app._ollama_generate
        self.calls = {"n": 0}
        app._ollama_generate = _pm_gen(counter=self.calls)

    def tearDown(self):
        app._ollama_generate = self._orig

    def test_list_flag_false_before_generation(self):
        iid = _open_incident("gpu_util", "gpu_power")
        _resolve(iid)
        items = app.list_incidents()
        row = next(x for x in items if x["id"] == iid)
        self.assertIn("has_postmortem", row)
        self.assertFalse(row["has_postmortem"])

    def test_list_flag_true_after_generation_zero_llm_on_list(self):
        iid = _open_incident("gpu_util", "gpu_power")
        _resolve(iid)
        app.get_incident_postmortem(iid, generate=True)
        n = self.calls["n"]
        # The LIST read itself must be LLM-free and must reflect the flag.
        r = self.c.get("/api/incidents")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.calls["n"], n)   # no new LLM on the list path
        items = r.get_json()["incidents"]
        row = next(x for x in items if x["id"] == iid)
        self.assertTrue(row["has_postmortem"])

    def test_list_does_not_ship_prose(self):
        iid = _open_incident("gpu_util", "gpu_power")
        _resolve(iid)
        app.get_incident_postmortem(iid, generate=True)
        r = self.c.get("/api/incidents")
        txt = r.get_data(as_text=True)
        self.assertNotIn("postmortem_json", txt)
        self.assertNotIn("probable_cause", txt)


class TestSettingsToggle(unittest.TestCase):
    """PART 2 — incident_auto_postmortem persists exactly like incident_auto_explain
    (same /api/settings POST path) and defaults OFF."""

    def setUp(self):
        _clean()
        self.c = app.app.test_client()

    def test_default_off(self):
        self.assertEqual(app.SETTING_DEFAULTS["incident_auto_postmortem"], "0")
        s = self.c.get("/api/settings").get_json()["settings"]
        self.assertEqual(s["incident_auto_postmortem"], "0")

    def test_toggle_persists_like_auto_explain(self):
        # Flip both via the SAME POST mechanism auto-explain uses.
        r = self.c.post("/api/settings", json={"incident_auto_postmortem": "1",
                                               "incident_auto_explain": "1"})
        self.assertEqual(r.status_code, 200)
        s = r.get_json()["settings"]
        self.assertEqual(s["incident_auto_postmortem"], "1")
        self.assertEqual(s["incident_auto_explain"], "1")
        # Round-trips through a fresh read.
        s2 = self.c.get("/api/settings").get_json()["settings"]
        self.assertEqual(s2["incident_auto_postmortem"], "1")
        # And clears back to OFF the same way.
        self.c.post("/api/settings", json={"incident_auto_postmortem": "0"})
        s3 = self.c.get("/api/settings").get_json()["settings"]
        self.assertEqual(s3["incident_auto_postmortem"], "0")


class TestDigestCitation(unittest.TestCase):
    """PART 3 — the digest sections cite the most-recent PERSISTED postmortem when
    present, omit it cleanly when absent, and add NO new LLM call (tripwire)."""

    def setUp(self):
        _clean()
        self._orig = app._ollama_generate
        self.calls = {"n": 0}
        app._ollama_generate = _pm_gen(
            cause="GPU power and utilisation spiked together under a model load.",
            counter=self.calls)

    def tearDown(self):
        app._ollama_generate = self._orig

    def _pm_section(self, sections):
        for header, lines in sections:
            if header == "Latest postmortem":
                return lines
        return None

    def test_omitted_when_no_postmortem(self):
        iid = _open_incident("gpu_util", "gpu_power")
        _resolve(iid)   # resolved but NO postmortem generated
        sections = app._digest_sections(now=1_000_500)
        self.assertIsNone(self._pm_section(sections))

    def test_cited_when_persisted_present_no_new_llm(self):
        iid = _open_incident("gpu_util", "gpu_power")
        _resolve(iid)
        app.get_incident_postmortem(iid, generate=True)   # persists prose
        n = self.calls["n"]
        # Building the digest SECTIONS must NOT call the LLM (deterministic only).
        sections = app._digest_sections(now=1_000_500)
        self.assertEqual(self.calls["n"], n)   # tripwire: zero new LLM
        lines = self._pm_section(sections)
        self.assertIsNotNone(lines)
        blob = " ".join(lines)
        self.assertIn("GPU power", blob)   # the persisted cause is cited
        self.assertIn(iid, blob)           # deterministic id-based title

    def test_citation_helper_reads_persisted_only_no_llm(self):
        # _latest_postmortem_citation is a pure cache read — never generates.
        iid = _open_incident("gpu_util", "gpu_power")
        _resolve(iid)
        # No postmortem yet → None.
        self.assertIsNone(app._latest_postmortem_citation())
        app.get_incident_postmortem(iid, generate=True)
        n = self.calls["n"]
        cite = app._latest_postmortem_citation()
        self.assertEqual(self.calls["n"], n)
        self.assertIsNotNone(cite)
        self.assertEqual(cite["id"], iid)
        self.assertTrue(cite["cause"])

    def test_most_recent_wins(self):
        iid1 = _open_incident("gpu_util", "gpu_power", at=1_000_000)
        _resolve(iid1, start=1_000_100)
        app.get_incident_postmortem(iid1, generate=True)
        # A second, later resolved incident with its own postmortem.
        iid2 = _open_incident("cpu_temp", "gpu_temp", at=2_000_000)
        _resolve(iid2, start=2_000_100)
        app._ollama_generate = _pm_gen(
            cause="Thermal ceiling reached during a sustained render.",
            counter=self.calls)
        app.get_incident_postmortem(iid2, generate=True)
        cite = app._latest_postmortem_citation()
        self.assertEqual(cite["id"], iid2)
        self.assertIn("Thermal", cite["cause"])


class TestDigestCitationPrivacy(unittest.TestCase):
    """The digest citation is a PRIVATE artifact — the persisted prose must never
    reach the unauthenticated public /status surface."""

    def setUp(self):
        _clean()
        self.c = app.app.test_client()
        self._sp = app.STATUS_PAGE
        app.STATUS_PAGE = True
        self._orig = app._ollama_generate
        self.marker = "SECRET-DIGEST-PM-MARKER-xyz"
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

    def test_marker_present_in_private_digest_citation(self):
        cite = app._latest_postmortem_citation()
        self.assertIn(self.marker, cite["cause"])


if __name__ == "__main__":
    unittest.main()
