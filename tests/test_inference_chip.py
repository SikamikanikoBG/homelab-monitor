"""Unit tests for the per-inference cost/throughput chip.

Covers `_inference_cost` (the server-side helper that turns ONE local generation's
timing × the live GPU watts × the electricity tariff into a tiny
{tokens,tps,ttft_ms,energy_wh,cost,currency} object) and that the copilot
endpoints carry `inference` when timings exist and omit it cleanly when not — all
without an ollama in CI (the LLM call is monkeypatched).

The tariff/GPU-power live numbers stay server-side; the math reuses the SAME
_cost_ctx/_price_at the Costs tab uses, so it can never contradict it.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


# A realistic non-streaming ollama /api/generate response (durations in ns).
# eval_count 120 over eval_duration 2.0 s → 60 tok/s; eval_duration_ns is the
# field _inference_cost needs for the energy math.
SAMPLE_GEN = {
    "model": "gemma3:1b",
    "response": "ok",
    "done": True,
    "load_duration": 1_200_000_000,
    "prompt_eval_count": 40,
    "prompt_eval_duration": 200_000_000,
    "eval_count": 120,
    "eval_duration": 2_000_000_000,        # 2.0 s
}


def _metrics():
    return app._llm_metrics_from_response(SAMPLE_GEN, "gemma3:1b")


class TestMetricsCarriesEvalDuration(unittest.TestCase):
    def test_eval_duration_ns_present(self):
        m = _metrics()
        self.assertEqual(m["eval_duration_ns"], 2_000_000_000)


class TestInferenceMath(unittest.TestCase):
    def setUp(self):
        self._power = app.LATEST.get("power")
        self._saved = dict(app.get_settings())

    def tearDown(self):
        app.LATEST["power"] = self._power
        app.save_settings(self._saved)

    def test_energy_and_cost_when_tariff_present(self):
        # 2.0 s of generation at 300 W → energy = 2.0 * 300 / 3600 = 0.1667 Wh.
        # At €0.30/kWh → cost = 0.1667/1000 * 0.30 = 0.00005 €.
        app.LATEST["power"] = 300.0
        app.save_settings({"kwh_price": "0.30", "currency": "€",
                           "tariff_mode": "single", "kwh_price_night": ""})
        inf = app._inference_cost(_metrics(), now=1_000_000)
        self.assertEqual(inf["tokens"], 120)
        self.assertEqual(inf["tps"], 60.0)
        self.assertAlmostEqual(inf["energy_wh"], 0.167, delta=0.002)
        self.assertEqual(inf["currency"], "€")
        # cost ≈ 0.00005 → at/under the floor, so it renders as the floor value.
        self.assertIsNotNone(inf["cost"])
        self.assertGreater(inf["cost"], 0.0)

    def test_cost_above_floor_not_floored(self):
        # Bigger power so the cost clears the 4-dp floor and shows a real number.
        app.LATEST["power"] = 3000.0
        app.save_settings({"kwh_price": "0.40", "currency": "$",
                           "tariff_mode": "single", "kwh_price_night": ""})
        inf = app._inference_cost(_metrics(), now=1_000_000)
        # energy = 2.0*3000/3600 = 1.667 Wh; cost = 1.667/1000*0.40 = 0.000667
        self.assertAlmostEqual(inf["cost"], 0.0007, delta=0.0001)
        self.assertFalse(inf.get("cost_floored"))
        self.assertEqual(inf["currency"], "$")

    def test_cost_floor_formatting(self):
        # A tiny but real cost must NOT collapse to 0.0000 — it floors to 0.0001
        # and flags cost_floored so the UI can prefix "<".
        app.LATEST["power"] = 100.0
        app.save_settings({"kwh_price": "0.10", "currency": "€",
                           "tariff_mode": "single", "kwh_price_night": ""})
        inf = app._inference_cost(_metrics(), now=1_000_000)
        # energy = 2.0*100/3600 = 0.0556 Wh; cost = 0.0556/1000*0.10 = 5.6e-6
        self.assertEqual(inf["cost"], 0.0001)
        self.assertTrue(inf["cost_floored"])

    def test_cost_omitted_when_tariff_disabled(self):
        # No tariff configured → energy still shown, cost is None (no money).
        app.LATEST["power"] = 300.0
        app.save_settings({"kwh_price": "", "currency": "$"})
        inf = app._inference_cost(_metrics(), now=1_000_000)
        self.assertIsNotNone(inf["energy_wh"])
        self.assertIsNone(inf["cost"])
        self.assertIsNone(inf["currency"])

    def test_energy_omitted_when_no_gpu_power(self):
        # GPU power unavailable → energy + cost omitted, tokens + tps still there.
        app.LATEST["power"] = 0
        app.save_settings({"kwh_price": "0.30", "currency": "€",
                           "tariff_mode": "single", "kwh_price_night": ""})
        inf = app._inference_cost(_metrics(), now=1_000_000)
        self.assertEqual(inf["tokens"], 120)
        self.assertEqual(inf["tps"], 60.0)
        self.assertIsNone(inf["energy_wh"])
        self.assertIsNone(inf["cost"])

    def test_none_when_no_timings(self):
        # LLM-down / facts-only path: no usable timing → no chip object at all.
        self.assertIsNone(app._inference_cost(None))
        self.assertIsNone(app._inference_cost({"eval_count": 0}))
        self.assertIsNone(app._inference_cost({"eval_count": 120}))  # no duration
        self.assertIsNone(app._inference_cost("not a dict"))


class TestEndpointsCarryInference(unittest.TestCase):
    def setUp(self):
        self.c = app.app.test_client()
        self._gen = app._ollama_generate
        self._power = app.LATEST.get("power")
        self._saved = dict(app.get_settings())
        self._en = app.COPILOT_ENABLED
        app.COPILOT_ENABLED = True
        app.LATEST["power"] = 300.0
        app.save_settings({"kwh_price": "0.30", "currency": "€",
                           "tariff_mode": "single", "kwh_price_night": ""})

    def tearDown(self):
        app._ollama_generate = self._gen
        app.LATEST["power"] = self._power
        app.save_settings(self._saved)
        app.COPILOT_ENABLED = self._en

    def _fake_ok(self, prompt, timeout=None, capture=None, fmt=None):
        if isinstance(capture, list):
            capture.append(_metrics())
        return "an answer", None

    def _fake_down(self, prompt, timeout=None, capture=None, fmt=None):
        if isinstance(capture, list):
            capture.append(None)
        return None, "unreachable"

    def test_ask_nonstream_carries_inference(self):
        app._ollama_generate = self._fake_ok
        j = self.c.post("/api/copilot/ask",
                        json={"question": "how is the gpu?"}).get_json()
        self.assertEqual(j["source"], "llm")
        self.assertIn("inference", j)
        self.assertEqual(j["inference"]["tokens"], 120)
        self.assertIsNotNone(j["inference"]["energy_wh"])

    def test_ask_nonstream_omits_inference_when_llm_down(self):
        app._ollama_generate = self._fake_down
        j = self.c.post("/api/copilot/ask",
                        json={"question": "how is the gpu?"}).get_json()
        self.assertEqual(j["source"], "facts")
        self.assertNotIn("inference", j)

    def test_explain_nonstream_carries_inference(self):
        app._ollama_generate = self._fake_ok
        j = self.c.post("/api/copilot/explain", json={"key": "power"}).get_json()
        self.assertEqual(j["source"], "llm")
        self.assertIn("inference", j)

    def test_digest_nonstream_carries_inference(self):
        app._ollama_generate = self._fake_ok
        j = self.c.get("/api/copilot/digest").get_json()
        self.assertEqual(j["source"], "llm")
        self.assertIn("inference", j)

    def test_inference_omitted_when_no_gpu_power(self):
        # Even with the LLM up, no GPU power → no usable energy → chip absent
        # (tokens/tps still exist but the endpoint only attaches a non-empty obj).
        app._ollama_generate = self._fake_ok
        app.LATEST["power"] = 0
        j = self.c.post("/api/copilot/ask",
                        json={"question": "x"}).get_json()
        # inference still attached (tokens+tps), but energy/cost are null.
        self.assertIn("inference", j)
        self.assertIsNone(j["inference"]["energy_wh"])


class TestStreamDoneFrameCarriesInference(unittest.TestCase):
    def setUp(self):
        self.c = app.app.test_client()
        self._stream = app._ollama_generate_stream
        self._power = app.LATEST.get("power")
        self._saved = dict(app.get_settings())
        self._en = app.COPILOT_ENABLED
        app.COPILOT_ENABLED = True
        app.LATEST["power"] = 300.0
        app.save_settings({"kwh_price": "0.30", "currency": "€",
                           "tariff_mode": "single", "kwh_price_night": ""})

    def tearDown(self):
        app._ollama_generate_stream = self._stream
        app.LATEST["power"] = self._power
        app.save_settings(self._saved)
        app.COPILOT_ENABLED = self._en

    def _fake_stream_ok(self, prompt, timeout=None):
        yield ("token", "an ")
        yield ("token", "answer")
        yield ("done", {"text": "an answer", "metrics": _metrics()})

    def _fake_stream_down(self, prompt, timeout=None):
        yield ("error", "unreachable")

    def _done_frame(self, body):
        """Parse the LAST done frame's JSON out of an SSE byte body."""
        import json
        frames = body.split("\n\n")
        for fr in reversed(frames):
            if "event: done" in fr:
                data = "".join(l[5:].strip() for l in fr.split("\n")
                               if l.startswith("data:"))
                return json.loads(data)
        return None

    def test_ask_stream_done_carries_inference(self):
        app._ollama_generate_stream = self._fake_stream_ok
        body = self.c.post("/api/copilot/ask/stream",
                           json={"question": "x"}).get_data(as_text=True)
        done = self._done_frame(body)
        self.assertIsNotNone(done)
        self.assertIn("inference", done)
        self.assertEqual(done["inference"]["tokens"], 120)

    def test_ask_stream_error_has_no_inference(self):
        app._ollama_generate_stream = self._fake_stream_down
        body = self.c.post("/api/copilot/ask/stream",
                           json={"question": "x"}).get_data(as_text=True)
        self.assertIn("event: error", body)
        self.assertNotIn('"inference"', body)


if __name__ == "__main__":
    unittest.main()
