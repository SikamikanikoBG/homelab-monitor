"""Unit tests for the Lab Copilot (E1) — the local-LLM insight layer.

Covers the non-LLM logic only (no ollama needed in CI):
  • context assembly from live LATEST + the forecast helpers
  • deterministic fact rendering (the LLM grounding + no-LLM fallback)
  • prompt assembly (digest + ask)
  • graceful-degrade: every endpoint returns 200 with a clear llm_status when
    the local LLM is disabled / unreachable, never a 500.
"""
import json
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


class TestExplainPromptAndFacts(unittest.TestCase):
    def _point(self, **kw):
        p = {"key": "gpu_power", "value": 280, "baseline": 90, "z": 4.2,
             "unit": "W", "direction": "spike", "magnitude": 190}
        p.update(kw)
        return p

    def test_explain_context_clamps_future_ts(self):
        now = int(time.time())
        ctx = app._explain_context(self._point(ts=now + 99999), now)
        self.assertLessEqual(ctx["ts"], now)

    def test_explain_context_handles_bad_ts(self):
        # garbage ts must not raise; falls back to now
        ctx = app._explain_context(self._point(ts="not-a-number"))
        self.assertIsInstance(ctx["ts"], int)

    def test_explain_facts_describe_the_spike(self):
        facts = app._explain_facts(app._explain_context(self._point()))
        joined = "\n".join(facts)
        self.assertIn("GPU power draw", joined)
        self.assertIn("spike", joined)
        self.assertIn("280", joined)   # value
        self.assertIn("90", joined)    # baseline

    def test_explain_facts_never_empty(self):
        facts = app._explain_facts(app._explain_context({"key": "", "value": None}))
        self.assertTrue(facts)
        self.assertIsInstance(facts[0], str)

    def test_explain_prompt_contains_facts_and_anchor(self):
        facts = ["At 2026-01-01 12:00, GPU power draw showed a spike."]
        p = app._explain_prompt(facts)
        self.assertIn(facts[0], p)
        self.assertIn("LIKELY CAUSE:", p)
        self.assertIn("ONLY", p.upper())


class TestExplainEndpointGraceful(unittest.TestCase):
    def setUp(self):
        self._en = app.COPILOT_ENABLED
        app.COPILOT_ENABLED = False   # force no-LLM path deterministically
        self.c = app.app.test_client()

    def tearDown(self):
        app.COPILOT_ENABLED = self._en

    def test_explain_degrades_to_facts(self):
        r = self.c.post("/api/copilot/explain", json={
            "key": "gpu_temp", "value": 95, "baseline": 60, "z": 5.0,
            "unit": "°C", "direction": "spike"})
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertEqual(j["source"], "facts")
        self.assertEqual(j["llm_status"], "disabled")
        self.assertTrue(j["explanation"])
        self.assertIsInstance(j["facts"], list)
        self.assertIn("model", j)

    def test_explain_handles_bad_payload(self):
        r = self.c.post("/api/copilot/explain", data="not json",
                        content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["source"], "facts")

    def test_explain_empty_payload_still_200(self):
        r = self.c.post("/api/copilot/explain", json={})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["facts"])


class TestAskRouting(unittest.TestCase):
    """The ask-box's deterministic retrieval/routing: entity detection, topic
    keyword routing, targeted retrieval, sources, bounds, fallback + degrade,
    and the no-secret-leak guarantee. Uses synthetic HEALTH + DB rows; ollama is
    forced off so the LLM path is deterministic (we assert on the routed facts)."""

    def setUp(self):
        self._latest = dict(app.LATEST)
        self._health = dict(app.HEALTH)
        self._en = app.COPILOT_ENABLED
        app.COPILOT_ENABLED = False
        # A small, realistic live fleet.
        app.HEALTH["docker"] = {"available": True, "containers": [
            {"name": "chroma", "state": "running", "status": "ok", "label": "Up 3 hours",
             "mem_bytes": 1500 * 1048576, "vram_bytes": None, "uptime_s": 10800},
            {"name": "ollama", "state": "running", "status": "ok", "label": "Up 1 day",
             "mem_bytes": 800 * 1048576, "vram_bytes": 4000 * 1048576, "uptime_s": 86400},
            {"name": "grafana", "state": "running", "status": "warn", "label": "unhealthy",
             "mem_bytes": 120 * 1048576, "vram_bytes": None, "uptime_s": 200},
        ], "summary": {"total": 3, "running": 3, "problems": 1}}
        app.HEALTH["systemd"] = {"available": True, "services": [
            {"name": "sshd.service", "active": "active", "status": "ok", "label": "running"},
        ], "summary": {"loaded": 1, "running": 1, "failed": 0, "admin": 0}}
        app.LATEST.update({"gpu_avail": True, "util": 60, "mem_used": 8000,
                           "mem_total": 24000, "power": 200, "temp": 65,
                           "models": [{"service": "ollama", "model": "gemma3:1b", "vram": 1500}]})
        self.now = int(time.time())
        with app.LOCK:
            app.DB.execute("DELETE FROM power_proc")
            app.DB.execute("DELETE FROM proc")
            app.DB.execute("DELETE FROM events")
            app.DB.execute("DELETE FROM disk_samples")
            # cost rows: chroma is the priciest, then ollama
            for k in range(50):
                ts = self.now - k * 600
                app.DB.execute("INSERT INTO power_proc VALUES(?,?,?,?)", (ts, "container", "chroma", 120))
                app.DB.execute("INSERT INTO power_proc VALUES(?,?,?,?)", (ts, "container", "ollama", 40))
                app.DB.execute("INSERT INTO power_proc VALUES(?,?,?,?)", (ts, "service", "sshd.service", 2))
            # memory rows (last 10 min)
            app.DB.execute("INSERT INTO proc VALUES(?,?,?)", (self.now - 60, "chroma", 1500))
            app.DB.execute("INSERT INTO proc VALUES(?,?,?)", (self.now - 60, "ollama", 800))
            # an OOM touching chroma
            app.DB.execute("INSERT INTO events VALUES(?,?,?,?)",
                           (self.now - 3600, "chroma", "oom", "killed chroma"))
            # a filling disk
            for k in range(8):
                ts = self.now - (7 - k) * 86400
                app.DB.execute("INSERT INTO disk_samples VALUES(?,?,?,?)",
                               (ts, "/backup", 800 + k * 20, 1000))
            # tariff so cost numbers render
            app.DB.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('kwh_price','0.30')")
            app.DB.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('currency','€')")
            app.DB.commit()

    def tearDown(self):
        app.COPILOT_ENABLED = self._en
        app.LATEST.clear(); app.LATEST.update(self._latest)
        app.HEALTH.clear(); app.HEALTH.update(self._health)
        # Clean up the synthetic rows/settings so we don't leak into other tests'
        # shared DB (e.g. a planted webhook_url would make digest tests see a
        # configured channel).
        with app.LOCK:
            app.DB.execute("DELETE FROM power_proc")
            app.DB.execute("DELETE FROM proc")
            app.DB.execute("DELETE FROM events")
            app.DB.execute("DELETE FROM disk_samples")
            for k in ("kwh_price", "currency", "webhook_url", "telegram_token"):
                app.DB.execute("DELETE FROM settings WHERE key=?", (k,))
            app.DB.commit()

    # ── entity detection ──────────────────────────────────────────────
    def test_entity_match_real_container(self):
        ents = app._ask_live_entities()
        hits = app._ask_match_entities("why is chroma using so much ram?", ents)
        self.assertIn(("chroma", "container"), [(h["name"], h["kind"]) for h in hits])

    def test_entity_match_ignores_noise(self):
        # 'grafanas' / substring must not falsely match 'grafana' (word boundary)
        hits = app._ask_match_entities("how are things going generally today?")
        self.assertEqual(hits, [])
        # substring guard: 'ollamatron' should not match 'ollama'
        hits2 = app._ask_match_entities("what is ollamatron?")
        self.assertNotIn("ollama", [h["name"] for h in hits2])

    def test_entity_match_model_name(self):
        hits = app._ask_match_entities("how big is gemma3:1b right now?")
        self.assertIn(("gemma3:1b", "model"), [(h["name"], h["kind"]) for h in hits])

    # ── topic keyword routing ─────────────────────────────────────────
    def test_topic_gpu(self):
        self.assertIn("gpu", app._ask_detect_topics("is the gpu healthy?"))

    def test_topic_disk(self):
        self.assertIn("disk", app._ask_detect_topics("which disk is filling fastest?"))

    def test_topic_cost(self):
        self.assertIn("cost", app._ask_detect_topics("what's my most expensive container?"))

    def test_topic_memory(self):
        self.assertIn("memory", app._ask_detect_topics("which container uses the most ram?"))

    def test_topic_uptime(self):
        self.assertIn("uptime", app._ask_detect_topics("is anything down right now?"))

    def test_topic_multiple(self):
        t = app._ask_detect_topics("is the gpu hot and which disk is full?")
        self.assertIn("gpu", t); self.assertIn("disk", t)

    # ── targeted retrieval ────────────────────────────────────────────
    def test_named_container_pulls_health_cost_oom(self):
        facts, used, _ = app._ask_route("why is chroma using so much memory?", self.now)
        joined = "\n".join(facts).lower()
        self.assertIn("chroma", joined)
        self.assertIn("container:chroma", used)
        self.assertTrue(any("ram" in f.lower() or "1500" in f for f in facts))
        self.assertIn("cost", used)      # per-entity MTD cost pulled
        self.assertIn("events", used)    # OOM pulled

    def test_most_expensive_pulls_top_entities(self):
        facts, used, _ = app._ask_route("what's my most expensive container this month?", self.now)
        joined = "\n".join(facts).lower()
        self.assertIn("cost", used)
        # chroma (120W) should rank above ollama (40W)
        self.assertIn("chroma", joined)
        ci, oi = joined.find("chroma"), joined.find("ollama")
        self.assertTrue(ci != -1 and (oi == -1 or ci < oi))

    def test_most_expensive_falls_back_to_energy_without_tariff(self):
        # Drop the tariff → "most expensive" must still rank, by energy (kWh).
        with app.LOCK:
            app.DB.execute("DELETE FROM settings WHERE key='kwh_price'")
            app.DB.commit()
        facts, used, _ = app._ask_route("most expensive container this month?", self.now)
        joined = "\n".join(facts).lower()
        self.assertIn("cost", used)
        self.assertIn("kwh", joined)        # ranked by energy use
        self.assertIn("chroma", joined)     # still the heaviest

    def test_cost_topic_without_fact_does_not_claim_cost_source(self):
        # A named entity with no cost data + a "cost" keyword must NOT surface a
        # phantom "based on: cost" chip — sources reflect only injected facts.
        ctx = {"cost_month": {"enabled": False}, "gpu": {}, "anomalies": []}
        lines, srcs = app._ask_topic_facts({"cost"}, "what does it cost", ctx, self.now)
        self.assertEqual(lines, [])
        self.assertNotIn("cost", srcs)

    def test_gpu_health_pulls_gpu_and_headroom(self):
        facts, used, _ = app._ask_route("is the gpu healthy?", self.now)
        self.assertIn("gpu", used)
        self.assertTrue(any("GPU now" in f for f in facts))

    def test_disk_pulls_fill_eta(self):
        facts, used, _ = app._ask_route("which disk is filling fastest?", self.now)
        self.assertIn("disk", used)
        self.assertTrue(any("/backup" in f for f in facts))

    # ── fallback + bounds + degrade ───────────────────────────────────
    def test_no_match_returns_empty_for_generic_fallback(self):
        facts, used, _ = app._ask_route("tell me a joke about the weather", self.now)
        self.assertEqual(facts, [])
        self.assertEqual(used, [])

    def test_context_size_bounded(self):
        big = ["fact number %d that is reasonably long " % i * 4 for i in range(100)]
        bounded = app._ask_bound_facts(big)
        self.assertLessEqual(len(bounded), app._ASK_MAX_FACTS)
        self.assertLessEqual(sum(len(f) for f in bounded), app._ASK_MAX_CHARS)
        self.assertTrue(all(len(f) <= app._ASK_MAX_LINE for f in bounded))

    def test_endpoint_routes_and_returns_sources(self):
        c = app.app.test_client()
        r = c.post("/api/copilot/ask", json={"question": "is the gpu healthy?"})
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertEqual(j["routing"], "live")
        self.assertIn("gpu", j["sources"])

    def test_endpoint_generic_fallback_when_no_match(self):
        c = app.app.test_client()
        r = c.post("/api/copilot/ask", json={"question": "hello there friend"})
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertEqual(j["routing"], "generic")
        self.assertTrue(j["facts"])  # generic digest facts, never empty

    def test_llm_unreachable_returns_facts_summary_not_500(self):
        # COPILOT_ENABLED is False (setUp) → _ollama_generate returns 'disabled'
        c = app.app.test_client()
        r = c.post("/api/copilot/ask", json={"question": "why is chroma using ram?"})
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertEqual(j["source"], "facts")
        self.assertEqual(j["llm_status"], "disabled")
        self.assertTrue(j["facts_summary"])      # routed facts handed back, useful
        self.assertIn("container:chroma", j["sources"])

    def test_always_200_on_garbage(self):
        c = app.app.test_client()
        r = c.post("/api/copilot/ask", data="not json", content_type="application/json")
        self.assertEqual(r.status_code, 200)

    def test_no_secret_leak_in_facts(self):
        # Plant a secret-looking setting; the routed facts must never carry it.
        with app.LOCK:
            app.DB.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('webhook_url','https://hooks.example/SECRETTOKEN')")
            app.DB.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('telegram_token','BOTSECRET123')")
            app.DB.commit()
        for q in ("is the gpu healthy?", "most expensive container?",
                  "why is chroma using ram?", "which disk is filling?"):
            facts, _, _ = app._ask_route(q, self.now)
            blob = "\n".join(facts)
            self.assertNotIn("SECRETTOKEN", blob)
            self.assertNotIn("BOTSECRET123", blob)
            self.assertNotIn("hooks.example", blob)


# ── SSE helpers for the streaming tests ───────────────────────────────────────
def _parse_sse(body):
    """Parse an SSE byte/str body into a list of (event, data_obj) frames."""
    if isinstance(body, bytes):
        body = body.decode("utf-8", "replace")
    out = []
    for frame in body.split("\n\n"):
        frame = frame.strip()
        if not frame:
            continue
        ev, data = "message", ""
        for line in frame.split("\n"):
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                data += line[5:].strip()
        try:
            out.append((ev, json.loads(data)))
        except Exception:
            out.append((ev, data))
    return out


class _FakeOllamaStreamResp:
    """Iterates like urllib's response object: yields raw JSONL byte lines, the
    last carrying done:true + timing fields (mirrors ollama /api/generate)."""
    def __init__(self, chunks, fail_after=None):
        lines = []
        for i, c in enumerate(chunks):
            lines.append(json.dumps({"model": "m", "response": c, "done": False}))
        lines.append(json.dumps({"model": "m", "response": "", "done": True,
                                 "eval_count": 10, "eval_duration": 1000000000,
                                 "load_duration": 50000000, "prompt_eval_count": 5,
                                 "prompt_eval_duration": 20000000}))
        self._lines = [(l + "\n").encode("utf-8") for l in lines]
        self._fail_after = fail_after

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        for i, l in enumerate(self._lines):
            if self._fail_after is not None and i >= self._fail_after:
                raise OSError("connection reset mid-stream")
            yield l


class TestAskStream(unittest.TestCase):
    """Streaming ask-box endpoint (/api/copilot/ask/stream): reuses the SAME live
    fixtures + routing as TestAskRouting (via its setUp/tearDown), mocks ollama
    streaming. Does NOT subclass it so its tests don't re-run here."""

    setUp = TestAskRouting.setUp
    tearDown = TestAskRouting.tearDown

    def _post_stream(self, q):
        c = app.app.test_client()
        return c.post("/api/copilot/ask/stream", json={"question": q})

    def test_stream_emits_tokens_then_done_with_sources(self):
        # Mock the streaming generator → token events + a done terminal.
        def fake_stream(prompt, timeout=None):
            self.assertIn("chroma", prompt.lower())  # routed facts in the prompt
            yield ("token", "Chro")
            yield ("token", "ma is ")
            yield ("token", "fine.")
            yield ("done", {"text": "Chroma is fine.", "metrics": None})
        orig = app._ollama_generate_stream
        app._ollama_generate_stream = fake_stream
        try:
            r = self._post_stream("why is chroma using so much memory?")
        finally:
            app._ollama_generate_stream = orig
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "text/event-stream")
        frames = _parse_sse(r.get_data())
        toks = [d["t"] for (ev, d) in frames if ev == "token"]
        self.assertEqual("".join(toks), "Chroma is fine.")
        done = [d for (ev, d) in frames if ev == "done"]
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0]["source"], "llm")
        self.assertEqual(done[0]["llm_status"], "ok")
        self.assertEqual(done[0]["answer"], "Chroma is fine.")
        self.assertEqual(done[0]["routing"], "live")
        self.assertIn("container:chroma", done[0]["sources"])

    def test_stream_routes_generic_when_no_match(self):
        def fake_stream(prompt, timeout=None):
            yield ("token", "ok")
            yield ("done", {"text": "ok", "metrics": None})
        orig = app._ollama_generate_stream
        app._ollama_generate_stream = fake_stream
        try:
            r = self._post_stream("hello there friend")
        finally:
            app._ollama_generate_stream = orig
        done = [d for (ev, d) in _parse_sse(r.get_data()) if ev == "done"]
        self.assertEqual(done[0]["routing"], "generic")
        self.assertTrue(done[0]["facts"])  # generic digest facts

    def test_stream_llm_error_at_start_emits_terminal_facts(self):
        # ollama unreachable before any token → single error frame with facts.
        def fake_stream(prompt, timeout=None):
            yield ("error", "unreachable")
        orig = app._ollama_generate_stream
        app._ollama_generate_stream = fake_stream
        try:
            r = self._post_stream("is the gpu healthy?")
        finally:
            app._ollama_generate_stream = orig
        self.assertEqual(r.status_code, 200)
        frames = _parse_sse(r.get_data())
        self.assertFalse([f for f in frames if f[0] == "token"])
        err = [d for (ev, d) in frames if ev == "error"]
        self.assertEqual(len(err), 1)
        self.assertEqual(err[0]["source"], "facts")
        self.assertEqual(err[0]["llm_status"], "unreachable")
        self.assertTrue(err[0]["facts_summary"])
        self.assertIn("gpu", err[0]["sources"])

    def test_stream_llm_error_mid_stream_terminates_gracefully(self):
        # Some tokens arrive, then ollama dies → tokens + a terminal error frame,
        # never a 500, never a hang.
        def fake_stream(prompt, timeout=None):
            yield ("token", "partial ")
            yield ("error", "unreachable")
        orig = app._ollama_generate_stream
        app._ollama_generate_stream = fake_stream
        try:
            r = self._post_stream("is the gpu healthy?")
        finally:
            app._ollama_generate_stream = orig
        frames = _parse_sse(r.get_data())
        self.assertEqual([d["t"] for (ev, d) in frames if ev == "token"], ["partial "])
        err = [d for (ev, d) in frames if ev == "error"]
        self.assertEqual(len(err), 1)
        self.assertEqual(err[0]["source"], "facts")

    def test_stream_empty_question_emits_terminal_error(self):
        r = self._post_stream("   ")
        self.assertEqual(r.status_code, 200)
        frames = _parse_sse(r.get_data())
        err = [d for (ev, d) in frames if ev == "error"]
        self.assertEqual(len(err), 1)
        self.assertEqual(err[0]["llm_status"], "no_question")

    def test_stream_disabled_llm_emits_facts_terminal(self):
        # COPILOT_ENABLED is False (inherited setUp) → real _ollama_generate_stream
        # yields ('error','disabled'); endpoint must hand back routed facts.
        r = self._post_stream("why is chroma using ram?")
        self.assertEqual(r.status_code, 200)
        frames = _parse_sse(r.get_data())
        err = [d for (ev, d) in frames if ev == "error"]
        self.assertEqual(len(err), 1)
        self.assertEqual(err[0]["llm_status"], "disabled")
        self.assertEqual(err[0]["source"], "facts")
        self.assertIn("container:chroma", err[0]["sources"])

    def test_stream_no_secret_in_streamed_bytes(self):
        # Plant secrets; the entire streamed body (tokens + terminal) must be clean.
        with app.LOCK:
            app.DB.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('webhook_url','https://hooks.example/SECRETTOKEN')")
            app.DB.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('telegram_token','BOTSECRET123')")
            app.DB.commit()

        def fake_stream(prompt, timeout=None):
            yield ("token", "fine")
            yield ("done", {"text": "fine", "metrics": None})
        orig = app._ollama_generate_stream
        app._ollama_generate_stream = fake_stream
        try:
            for q in ("is the gpu healthy?", "most expensive container?",
                      "why is chroma using ram?", "which disk is filling?"):
                body = self._post_stream(q).get_data(as_text=True)
                self.assertNotIn("SECRETTOKEN", body)
                self.assertNotIn("BOTSECRET123", body)
                self.assertNotIn("COPILOT_OLLAMA_URL", body)
                self.assertNotIn("11434", body)  # ollama URL/port never streamed
        finally:
            app._ollama_generate_stream = orig

    def test_stream_generator_real_ollama_jsonl_tokens(self):
        # Drive the REAL _ollama_generate_stream against a faked urllib response
        # that yields ollama's JSONL — proves token parsing + done detection.
        self._en2 = app.COPILOT_ENABLED
        app.COPILOT_ENABLED = True

        def fake_urlopen(req, timeout=None):
            return _FakeOllamaStreamResp(["Hel", "lo ", "lab"])
        orig = app.urllib.request.urlopen
        app.urllib.request.urlopen = fake_urlopen
        try:
            evs = list(app._ollama_generate_stream("p"))
        finally:
            app.urllib.request.urlopen = orig
            app.COPILOT_ENABLED = self._en2
        toks = [v for (k, v) in evs if k == "token"]
        self.assertEqual("".join(toks), "Hello lab")
        done = [v for (k, v) in evs if k == "done"]
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0]["text"], "Hello lab")

    def test_stream_generator_unreachable_yields_error(self):
        self._en2 = app.COPILOT_ENABLED
        app.COPILOT_ENABLED = True

        def boom(req, timeout=None):
            raise OSError("no route to host")
        orig = app.urllib.request.urlopen
        app.urllib.request.urlopen = boom
        try:
            evs = list(app._ollama_generate_stream("p"))
        finally:
            app.urllib.request.urlopen = orig
            app.COPILOT_ENABLED = self._en2
        self.assertEqual(evs, [("error", "unreachable")])

    def test_nonstream_endpoint_still_intact(self):
        # The additive stream must not have changed the original endpoint.
        c = app.app.test_client()
        r = c.post("/api/copilot/ask", json={"question": "is the gpu healthy?"})
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertEqual(j["routing"], "live")
        self.assertIn("gpu", j["sources"])
        self.assertEqual(j["source"], "facts")     # LLM off → facts fallback
        self.assertEqual(j["llm_status"], "disabled")


class TestExplainStream(unittest.TestCase):
    """Streaming 'explain this spike' endpoint (/api/copilot/explain/stream):
    builds the SAME deterministic explain context/facts/prompt as the non-stream
    endpoint, then streams the LLM's likely-cause. Mirrors TestAskStream."""

    def setUp(self):
        self._en = app.COPILOT_ENABLED
        app.COPILOT_ENABLED = False   # default: deterministic no-LLM path
        self.c = app.app.test_client()

    def tearDown(self):
        app.COPILOT_ENABLED = self._en
        # Remove any settings planted by the no-secret test so shared DB state
        # doesn't leak into later suites (e.g. digest channel detection).
        with app.LOCK:
            app.DB.execute("DELETE FROM settings WHERE key IN ('webhook_url','telegram_token')")
            app.DB.commit()

    def _point(self, **kw):
        p = {"key": "gpu_power", "value": 280, "baseline": 90, "z": 4.2,
             "unit": "W", "direction": "spike", "magnitude": 190}
        p.update(kw)
        return p

    def _post_stream(self, payload):
        return self.c.post("/api/copilot/explain/stream", json=payload)

    def test_stream_emits_tokens_then_done(self):
        def fake_stream(prompt, timeout=None):
            self.assertIn("LIKELY CAUSE:", prompt)   # explain prompt assembled
            self.assertIn("GPU power draw", prompt)   # explain facts grounded
            yield ("token", "Inference ")
            yield ("token", "load spiked.")
            yield ("done", {"text": "Inference load spiked.", "metrics": None})
        orig = app._ollama_generate_stream
        app._ollama_generate_stream = fake_stream
        try:
            r = self._post_stream(self._point())
        finally:
            app._ollama_generate_stream = orig
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "text/event-stream")
        frames = _parse_sse(r.get_data())
        toks = [d["t"] for (ev, d) in frames if ev == "token"]
        self.assertEqual("".join(toks), "Inference load spiked.")
        done = [d for (ev, d) in frames if ev == "done"]
        self.assertEqual(len(done), 1)
        # Terminal payload shape matches the non-stream /api/copilot/explain.
        self.assertEqual(done[0]["source"], "llm")
        self.assertEqual(done[0]["llm_status"], "ok")
        self.assertEqual(done[0]["explanation"], "Inference load spiked.")
        self.assertEqual(done[0]["key"], "gpu_power")
        self.assertIsInstance(done[0]["facts"], list)
        self.assertIn("context", done[0])
        self.assertIn("model", done[0])

    def test_stream_terminal_matches_nonstream_shape(self):
        # done frame must carry the same top-level keys the non-stream endpoint
        # returns (so the UI terminal render is identical).
        def fake_stream(prompt, timeout=None):
            yield ("token", "x")
            yield ("done", {"text": "x", "metrics": None})
        nonstream = self.c.post("/api/copilot/explain", json=self._point()).get_json()
        orig = app._ollama_generate_stream
        app._ollama_generate_stream = fake_stream
        try:
            frames = _parse_sse(self._post_stream(self._point()).get_data())
        finally:
            app._ollama_generate_stream = orig
        done = [d for (ev, d) in frames if ev == "done"][0]
        for k in ("now", "model", "key", "facts", "context", "enabled",
                  "explanation", "source", "llm_status"):
            self.assertIn(k, done, "done frame missing key %r" % k)
            self.assertIn(k, nonstream)

    def test_stream_llm_error_at_start_emits_terminal_facts(self):
        def fake_stream(prompt, timeout=None):
            yield ("error", "unreachable")
        orig = app._ollama_generate_stream
        app._ollama_generate_stream = fake_stream
        try:
            r = self._post_stream(self._point())
        finally:
            app._ollama_generate_stream = orig
        self.assertEqual(r.status_code, 200)
        frames = _parse_sse(r.get_data())
        self.assertFalse([f for f in frames if f[0] == "token"])
        err = [d for (ev, d) in frames if ev == "error"]
        self.assertEqual(len(err), 1)
        self.assertEqual(err[0]["source"], "facts")
        self.assertEqual(err[0]["llm_status"], "unreachable")
        self.assertTrue(err[0]["explanation"])   # deterministic facts summary
        self.assertTrue(err[0]["facts"])

    def test_stream_llm_error_mid_stream_terminates_gracefully(self):
        def fake_stream(prompt, timeout=None):
            yield ("token", "partial ")
            yield ("error", "unreachable")
        orig = app._ollama_generate_stream
        app._ollama_generate_stream = fake_stream
        try:
            frames = _parse_sse(self._post_stream(self._point()).get_data())
        finally:
            app._ollama_generate_stream = orig
        self.assertEqual([d["t"] for (ev, d) in frames if ev == "token"], ["partial "])
        err = [d for (ev, d) in frames if ev == "error"]
        self.assertEqual(len(err), 1)
        self.assertEqual(err[0]["source"], "facts")

    def test_stream_disabled_llm_emits_facts_terminal(self):
        # COPILOT_ENABLED False (setUp) → real generator yields ('error','disabled').
        r = self._post_stream(self._point())
        self.assertEqual(r.status_code, 200)
        frames = _parse_sse(r.get_data())
        err = [d for (ev, d) in frames if ev == "error"]
        self.assertEqual(len(err), 1)
        self.assertEqual(err[0]["llm_status"], "disabled")
        self.assertEqual(err[0]["source"], "facts")
        self.assertTrue(err[0]["explanation"])

    def test_stream_empty_payload_emits_terminal_not_500(self):
        # No usable point → still a terminal frame (facts), never a 500.
        r = self._post_stream({})
        self.assertEqual(r.status_code, 200)
        frames = _parse_sse(r.get_data())
        term = [(ev, d) for (ev, d) in frames if ev in ("done", "error")]
        self.assertEqual(len(term), 1)
        self.assertEqual(term[0][0], "error")     # LLM off → facts terminal
        self.assertTrue(term[0][1]["facts"])

    def test_stream_bad_payload_emits_terminal_not_500(self):
        r = self.c.post("/api/copilot/explain/stream", data="not json",
                        content_type="application/json")
        self.assertEqual(r.status_code, 200)
        frames = _parse_sse(r.get_data())
        self.assertTrue([f for f in frames if f[0] in ("done", "error")])

    def test_stream_no_secret_in_streamed_bytes(self):
        with app.LOCK:
            app.DB.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('webhook_url','https://hooks.example/SECRETTOKEN')")
            app.DB.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('telegram_token','BOTSECRET123')")
            app.DB.commit()

        def fake_stream(prompt, timeout=None):
            yield ("token", "fine")
            yield ("done", {"text": "fine", "metrics": None})
        orig = app._ollama_generate_stream
        app._ollama_generate_stream = fake_stream
        try:
            for pt in (self._point(), self._point(key="gpu_temp", unit="°C"),
                       self._point(key="power_draw"), {}):
                body = self._post_stream(pt).get_data(as_text=True)
                self.assertNotIn("SECRETTOKEN", body)
                self.assertNotIn("BOTSECRET123", body)
                self.assertNotIn("COPILOT_OLLAMA_URL", body)
                self.assertNotIn("11434", body)   # ollama URL/port never streamed
        finally:
            app._ollama_generate_stream = orig

    def test_nonstream_explain_still_intact(self):
        # The additive stream must not have changed the original endpoint shape.
        r = self.c.post("/api/copilot/explain", json=self._point())
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertEqual(j["source"], "facts")        # LLM off → facts fallback
        self.assertEqual(j["llm_status"], "disabled")
        self.assertTrue(j["explanation"])
        self.assertEqual(j["key"], "gpu_power")


if __name__ == "__main__":
    unittest.main()
