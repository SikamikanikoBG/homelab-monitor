"""Unit tests for the Lab Copilot (E1) — the local-LLM insight layer.

Covers the non-LLM logic only (no ollama needed in CI):
  • context assembly from live LATEST + the forecast helpers
  • deterministic fact rendering (the LLM grounding + no-LLM fallback)
  • prompt assembly (digest + ask)
  • graceful-degrade: every endpoint returns 200 with a clear llm_status when
    the local LLM is disabled / unreachable, never a 500.
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


class TestContextAndFacts(unittest.TestCase):
    def setUp(self):
        # Snapshot LATEST so we can mutate it freely, restore in tearDown.
        self._latest = dict(app.LATEST)

    def tearDown(self):
        app.LATEST.clear()
        app.LATEST.update(self._latest)

    def test_facts_no_gpu(self):
        app.LATEST.update({"gpu_avail": False, "util": 0, "mem_used": 0,
                           "mem_total": 0, "power": 0, "temp": 0, "models": []})
        ctx = app._copilot_context()
        facts = app._copilot_facts(ctx)
        self.assertTrue(any("No GPU" in f for f in facts))

    def test_facts_gpu_and_model(self):
        app.LATEST.update({"gpu_avail": True, "util": 73, "mem_used": 8000,
                           "mem_total": 24000, "power": 210, "temp": 64,
                           "models": [{"service": "ollama", "model": "gemma3:1b", "vram": 1500},
                                      {"service": "x", "model": "small", "vram": 100}]})
        ctx = app._copilot_context()
        facts = app._copilot_facts(ctx)
        joined = "\n".join(facts)
        self.assertIn("73%", joined)
        self.assertIn("210 W", joined)
        # biggest model is surfaced as the driver, and it's the larger one
        self.assertIn("gemma3:1b", joined)
        self.assertTrue(any("Biggest model" in f for f in facts))

    def test_facts_never_empty(self):
        # Even with a totally empty LATEST, facts list is non-empty.
        app.LATEST.clear()
        app.LATEST.update({"mem_total": 0})
        facts = app._copilot_facts(app._copilot_context())
        self.assertTrue(facts)
        self.assertIsInstance(facts[0], str)

    def test_context_picks_top_three_models(self):
        app.LATEST.update({"gpu_avail": True, "mem_total": 24000,
                           "models": [{"service": "s%d" % i, "model": "m%d" % i, "vram": i * 100}
                                      for i in range(6)]})
        ctx = app._copilot_context()
        self.assertLessEqual(len(ctx["models"]), 3)
        # sorted biggest-first
        self.assertEqual(ctx["models"][0]["vram_mb"], 500)


class TestPromptAssembly(unittest.TestCase):
    def test_digest_prompt_contains_facts(self):
        facts = ["GPU: 50% utilisation.", "Disk /backup fills in ~9 days."]
        p = app._copilot_digest_prompt(facts)
        self.assertIn("GPU: 50% utilisation.", p)
        self.assertIn("Disk /backup fills in ~9 days.", p)
        self.assertIn("DIGEST:", p)
        # grounding instruction present so the model doesn't invent numbers
        self.assertIn("ONLY", p.upper())

    def test_ask_prompt_contains_question_and_facts(self):
        facts = ["GPU: 64C."]
        p = app._copilot_ask_prompt(facts, "is it overheating?")
        self.assertIn("is it overheating?", p)
        self.assertIn("GPU: 64C.", p)
        self.assertIn("ANSWER:", p)


class TestOllamaGraceful(unittest.TestCase):
    def setUp(self):
        self._url, self._en = app.COPILOT_OLLAMA_URL, app.COPILOT_ENABLED

    def tearDown(self):
        app.COPILOT_OLLAMA_URL, app.COPILOT_ENABLED = self._url, self._en

    def test_disabled_returns_disabled_code(self):
        app.COPILOT_ENABLED = False
        txt, err = app._ollama_generate("hi")
        self.assertIsNone(txt)
        self.assertEqual(err, "disabled")

    def test_unreachable_returns_unreachable_code(self):
        # Point at a dead port; must degrade, not raise.
        app.COPILOT_ENABLED = True
        app.COPILOT_OLLAMA_URL = "http://127.0.0.1:1"
        txt, err = app._ollama_generate("hi", timeout=2)
        self.assertIsNone(txt)
        self.assertEqual(err, "unreachable")


class TestEndpointsGraceful(unittest.TestCase):
    """Endpoints must always be 200 with a usable shape even when the LLM is off."""
    def setUp(self):
        self._en = app.COPILOT_ENABLED
        app.COPILOT_ENABLED = False   # force the no-LLM path deterministically
        self.c = app.app.test_client()

    def tearDown(self):
        app.COPILOT_ENABLED = self._en

    def test_digest_degrades_to_facts(self):
        r = self.c.get("/api/copilot/digest")
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertEqual(j["source"], "facts")
        self.assertEqual(j["llm_status"], "disabled")
        self.assertTrue(j["digest"])           # falls back to the joined facts
        self.assertIsInstance(j["facts"], list)
        self.assertIn("model", j)

    def test_ask_empty_question(self):
        r = self.c.post("/api/copilot/ask", json={"question": "  "})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["llm_status"], "no_question")

    def test_ask_degrades_gracefully(self):
        r = self.c.post("/api/copilot/ask", json={"question": "is anything hot?"})
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertEqual(j["source"], "facts")
        self.assertEqual(j["llm_status"], "disabled")
        self.assertTrue(j["facts"])

    def test_ask_handles_bad_payload(self):
        r = self.c.post("/api/copilot/ask", data="not json",
                         content_type="application/json")
        self.assertEqual(r.status_code, 200)


if __name__ == "__main__":
    unittest.main()
