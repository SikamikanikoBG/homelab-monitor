"""Model adapters against a local stub server (#329, US-1/US-2, EC-1/2/12/13).

No cloud keys exist in CI, so every adapter is exercised over its real wire
format against tests/agent_stub_server.py, plus golden checks of what each one
sends (auth header style, schema down-conversion, Azure URLs). An opt-in live
check runs against a real OpenAI-compatible server when HLM_LIVE_LLM is set.
"""
import json
import os
import threading
import time
import unittest
from unittest.mock import patch

import agent_stub_server as stub
from backend.agent import providers
from backend.agent.providers import http, probe
from backend.agent.providers.anthropic import AnthropicAdapter, to_anthropic_messages
from backend.agent.providers.base import ChatRequest, ProviderError, parse_args
from backend.agent.providers.ollama import OllamaAdapter
from backend.agent.providers.openai_compat import OpenAICompatAdapter, smaller_max_tokens, strip_schema

TOOL = {"name": "get_host", "description": "d",
        "parameters": {"type": "object", "additionalProperties": False,
                       "properties": {"name": {"type": "string", "pattern": "^[a-z]+$"}}}}


def req(**kw):
    base = dict(model="stub-agent", system="sys", messages=[{"role": "user", "content": "hi"}], tools=[TOOL],
                max_tokens=256)
    base.update(kw)
    return ChatRequest(**base)


class StubCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv, cls.url = stub.start()
        cls._backoff = http.BACKOFF
        http.BACKOFF = (0, 0, 0)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        http.BACKOFF = cls._backoff

    def setUp(self):
        stub.set_script([])


class TestOpenAICompat(StubCase):
    def ad(self, path="/v1", **kw):
        return OpenAICompatAdapter(self.url + path, api_key=kw.pop("key", "sk-test-1234"),
                                   preset=kw.pop("preset", {"auth": "bearer"}), **kw)

    def test_text_streams_and_usage(self):
        stub.set_script([{"text": "Hello from the stub, this is long", "usage": {"prompt": 11, "completion": 7}}])
        deltas = []
        r = self.ad().chat(req(), on_delta=deltas.append)
        self.assertEqual(r.text, "Hello from the stub, this is long")
        self.assertGreater(len(deltas), 1)
        self.assertEqual((r.usage.prompt, r.usage.completion), (11, 7))
        self.assertFalse(r.usage.estimated)
        self.assertEqual(stub.REQUESTS[-1]["headers"]["Authorization"], "Bearer sk-test-1234")

    def test_tool_call_deltas_are_assembled(self):
        stub.set_script([{"tool_calls": [{"name": "get_host", "arguments": {"name": "lab"}},
                                         {"name": "list_hosts", "arguments": {}}]}])
        r = self.ad().chat(req())
        self.assertEqual([c["name"] for c in r.tool_calls], ["get_host", "list_hosts"])
        self.assertEqual(r.tool_calls[0]["arguments"], {"name": "lab"})
        self.assertTrue(all(c["id"] for c in r.tool_calls))

    def test_malformed_arguments_kept_raw(self):
        stub.set_script([{"tool_calls": [{"name": "get_host", "arguments": '{"name": "lab"'}]}])
        r = self.ad().chat(req())
        self.assertIn("__raw__", r.tool_calls[0]["arguments"])
        self.assertEqual(parse_args("[1]"), {"__raw__": "[1]"})

    def test_payload_shape(self):
        stub.set_script([{"text": "x"}])
        self.ad(preset={"auth": "bearer", "stream_usage": False, "extra_body": {"chat_template_kwargs": {"x": 1}}}).chat(
            req(messages=[{"role": "user", "content": "q"},
                          {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "name": "get_host",
                                                                                 "arguments": {"name": "a"}}]},
                          {"role": "tool", "tool_call_id": "c1", "name": "get_host", "content": "{}"}]))
        b = stub.REQUESTS[-1]["body"]
        self.assertNotIn("stream_options", b)
        self.assertEqual(b["chat_template_kwargs"], {"x": 1})
        self.assertEqual(b["messages"][0], {"role": "system", "content": "sys"})
        self.assertEqual(b["messages"][2]["tool_calls"][0]["function"]["arguments"], '{"name": "a"}')
        self.assertEqual(b["messages"][3]["tool_call_id"], "c1")
        self.assertEqual(b["tools"][0]["function"]["parameters"]["properties"]["name"]["pattern"], "^[a-z]+$")

    def test_errors_are_classified(self):
        cases = {401: "auth", 403: "auth", 400: "bad_request", 404: "bad_request"}
        for code, kind in cases.items():
            with self.assertRaises(ProviderError) as cm:
                self.ad("/err/%d/v1" % code).chat(req())
            self.assertEqual(cm.exception.kind, kind, code)
        for code, kind in ((429, "rate_limited"), (503, "server")):
            before = len(stub.REQUESTS)
            with self.assertRaises(ProviderError) as cm:
                self.ad("/err/%d/v1" % code).chat(req())
            self.assertEqual(cm.exception.kind, kind)

    def test_retries_before_first_byte_only_for_retryable(self):
        calls = []
        orig = http._open

        def flaky(r, timeout, secret):
            calls.append(1)
            if len(calls) < 3:
                raise ProviderError("server", "boom", status=502)
            return orig(r, timeout, secret)
        stub.set_script([{"text": "ok"}])
        with patch.object(http, "_open", flaky):
            self.assertEqual(self.ad().chat(req()).text, "ok")
        self.assertEqual(len(calls), 3)
        calls.clear()

        def auth(r, timeout, secret):
            calls.append(1)
            raise ProviderError("auth", "no")
        with patch.object(http, "_open", auth), self.assertRaises(ProviderError):
            self.ad().chat(req())
        self.assertEqual(len(calls), 1)

    def test_unreachable_and_timeout(self):
        with self.assertRaises(ProviderError) as cm:
            OpenAICompatAdapter("http://127.0.0.1:9", preset={}).chat(req())
        self.assertEqual(cm.exception.kind, "unreachable")
        with self.assertRaises(ProviderError) as cm:
            OpenAICompatAdapter(self.url + "/slow/2/v1", preset={}, timeout=0.5).chat(req())
        self.assertEqual(cm.exception.kind, "timeout")

    def test_key_never_in_error_text(self):
        try:
            self.ad("/err/401/v1", key="sk-SECRETKEY").chat(req())
        except ProviderError as e:
            self.assertNotIn("sk-SECRETKEY", str(e))
            self.assertNotIn("sk-SECRETKEY", e.human())

    def test_cancel_before_start(self):
        ev = threading.Event()
        ev.set()
        with self.assertRaises(ProviderError) as cm:
            self.ad().chat(req(), cancel=ev)
        self.assertEqual(cm.exception.kind, "cancelled")

    def test_abort_mid_stream(self):
        stub.set_script([{"text": "x" * 4000}])
        ad = self.ad()
        got = []

        def on(d):
            got.append(d)
            if len(got) == 3:
                ad.abort()
        with self.assertRaises(ProviderError) as cm:
            ad.chat(req(), on_delta=on)
        self.assertEqual(cm.exception.kind, "cancelled")

    def test_no_redirects(self):
        import http.server as hs

        class R(hs.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                self.send_response(302)
                self.send_header("Location", "http://evil.example/steal")
                self.send_header("Content-Length", "0")
                self.end_headers()
        srv = hs.ThreadingHTTPServer(("127.0.0.1", 0), R)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with self.assertRaises(ProviderError) as cm:
                OpenAICompatAdapter("http://127.0.0.1:%d/v1" % srv.server_address[1], api_key="k", preset={}).chat(req())
            self.assertEqual(cm.exception.kind, "protocol")
        finally:
            srv.shutdown()

    def test_list_models_reads_context_window(self):
        ms = self.ad().list_models()
        self.assertEqual(ms[0].id, "stub-agent")
        self.assertEqual(ms[0].context_window, 32768)

    def test_context_400_shrink(self):
        msg = ("This model's maximum context length is 32768 tokens. However, you requested 16384 output tokens "
               "and your prompt contains at least 20000 input tokens")
        self.assertEqual(smaller_max_tokens(msg, 16384), 8192)
        self.assertIsNone(smaller_max_tokens(msg.replace("20000", "32600"), 16384))
        self.assertIsNone(smaller_max_tokens("other", 1000))

    def test_azure_url_and_header(self):
        p = providers.preset("azure_openai")
        ad = OpenAICompatAdapter("https://x.openai.azure.com", api_key="az", preset=p,
                                 extra={"deployment": "gpt4o", "api_version": "2024-10-21"})
        self.assertEqual(ad._chat_url("ignored"),
                         "https://x.openai.azure.com/openai/deployments/gpt4o/chat/completions?api-version=2024-10-21")
        self.assertEqual(ad._headers(), {"api-key": "az"})

    def test_gemini_schema_downconvert(self):
        p = providers.preset("gemini")
        self.assertTrue(p["experimental"])
        out = OpenAICompatAdapter("x", preset=p).payload(req())
        params = out["tools"][0]["function"]["parameters"]
        self.assertNotIn("additionalProperties", params)
        self.assertNotIn("pattern", params["properties"]["name"])
        self.assertEqual(strip_schema({"a": [{"pattern": 1, "b": 2}]}, ["pattern"]), {"a": [{"b": 2}]})

    def test_provider_meta_echoed_back(self):
        from backend.agent.providers.openai_compat import to_openai_messages
        out = to_openai_messages("", [{"role": "assistant", "content": None,
                                       "tool_calls": [{"id": "c1", "name": "x", "arguments": {}}],
                                       "provider_meta": {"tool_calls": {"c1": {"extra_content": {"sig": "abc"}}}}}])
        self.assertEqual(out[0]["tool_calls"][0]["extra_content"], {"sig": "abc"})


class TestOllama(StubCase):
    def test_chat_with_num_ctx_and_tools(self):
        stub.set_script([{"text": "hello", "tool_calls": [{"name": "get_host", "arguments": {"name": "a"}}],
                          "usage": {"prompt": 5, "completion": 3}}])
        ad = OllamaAdapter(self.url)
        r = ad.chat(req(num_ctx=16384))
        self.assertEqual(r.text, "hello")
        self.assertEqual(r.tool_calls[0]["arguments"], {"name": "a"})
        self.assertEqual((r.usage.prompt, r.usage.completion), (5, 3))
        self.assertEqual(stub.REQUESTS[-1]["body"]["options"]["num_ctx"], 16384)

    def test_show_and_list(self):
        ad = OllamaAdapter(self.url + "/v1")         # a pasted /v1 is tolerated
        self.assertEqual(ad.show("stub-agent"), (32768, "yes"))
        self.assertEqual(ad.show("stub-notools"), (32768, "no"))
        self.assertIn("stub-agent", [m.id for m in ad.list_models()])


class TestAnthropic(StubCase):
    def test_chat(self):
        stub.set_script([{"text": "hi there", "tool_calls": [{"name": "get_host", "arguments": {"name": "lab"}}],
                          "usage": {"prompt": 9, "completion": 4}}])
        ad = AnthropicAdapter(self.url + "/v1", api_key="ak-1")
        r = ad.chat(req())
        self.assertEqual(r.text, "hi there")
        self.assertEqual(r.tool_calls[0]["arguments"], {"name": "lab"})
        self.assertEqual((r.usage.prompt, r.usage.completion), (9, 4))
        h = {k.lower(): v for k, v in stub.REQUESTS[-1]["headers"].items()}
        self.assertEqual(h["x-api-key"], "ak-1")
        self.assertEqual(h["anthropic-version"], "2023-06-01")
        b = stub.REQUESTS[-1]["body"]
        self.assertEqual(b["system"], "sys")
        self.assertEqual(b["tools"][0]["input_schema"], TOOL["parameters"])
        self.assertTrue(b["max_tokens"] > 0)

    def test_tool_results_merge_into_one_user_turn(self):
        out = to_anthropic_messages([
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "a", "name": "x", "arguments": {}},
                                                               {"id": "b", "name": "y", "arguments": {}}]},
            {"role": "tool", "tool_call_id": "a", "content": "1"},
            {"role": "tool", "tool_call_id": "b", "content": "2"}])
        self.assertEqual(len(out), 3)
        self.assertEqual([b["tool_use_id"] for b in out[2]["content"]], ["a", "b"])


class TestProbe(StubCase):
    def test_tool_capable_model(self):
        r = probe.tool_probe(OpenAICompatAdapter(self.url + "/v1", preset={}), "stub-agent")
        self.assertEqual((r["tools"], r["multi_step"]), ("yes", "yes"))

    def test_text_only_model(self):
        r = probe.tool_probe(OllamaAdapter(self.url), "stub-notools")
        self.assertEqual((r["tools"], r["multi_step"]), ("no", "no"))
        self.assertIn("tool", r["detail"])

    def test_one_step_only(self):
        stub.set_script([{"tool_calls": [{"name": "report_number", "arguments": {"n": 7}}]},
                         {"text": "twelve"}])
        r = probe.tool_probe(OpenAICompatAdapter(self.url + "/v1", preset={}), "stub-agent")
        self.assertEqual((r["tools"], r["multi_step"]), ("yes", "no"))

    def test_unreachable_is_unknown(self):
        r = probe.tool_probe(OpenAICompatAdapter("http://127.0.0.1:9", preset={}), "m")
        self.assertEqual(r["tools"], "unknown")


class TestPresets(unittest.TestCase):
    def test_presets_are_well_formed(self):
        ids = set()
        for p in providers.presets():
            self.assertNotIn(p["id"], ids)
            ids.add(p["id"])
            self.assertIn(p["adapter"], ("openai_compat", "ollama", "anthropic"))
            self.assertNotIn("price", json.dumps(p))           # no shipped prices
            if not p.get("local"):
                self.assertTrue(p["base_url"].startswith("https://") or p["id"] == "azure_openai", p["id"])
        for want in ("openai", "anthropic", "gemini", "azure_openai", "groq", "mistral", "deepseek", "xai",
                     "openrouter", "together", "vllm", "llamacpp", "lmstudio", "ollama", "localai"):
            self.assertIn(want, ids)

    def test_check_base_url(self):
        self.assertIsNone(providers.check_base_url("http://vader:8000/v1"))
        self.assertIsNotNone(providers.check_base_url("file:///etc/passwd"))
        self.assertIsNotNone(providers.check_base_url("http://user:pw@host/v1"))

    def test_key_ref(self):
        with patch.dict(os.environ, {"MY_KEY": "sk-env"}):
            self.assertEqual(providers.resolve_key({"key_ref": "env:MY_KEY"}), "sk-env")
        self.assertIsNone(providers.check_key_ref("env:X"))
        self.assertIsNotNone(providers.check_key_ref("plain"))


@unittest.skipUnless(os.environ.get("HLM_LIVE_LLM"), "set HLM_LIVE_LLM=http://host:port/v1 to run")
class TestLive(unittest.TestCase):
    def test_live_probe(self):
        ad = OpenAICompatAdapter(os.environ["HLM_LIVE_LLM"], preset=providers.preset("vllm"))
        model = os.environ.get("HLM_LIVE_MODEL") or ad.list_models()[0].id
        r = probe.tool_probe(ad, model)
        self.assertEqual(r["tools"], "yes", r)


if __name__ == "__main__":
    unittest.main()
