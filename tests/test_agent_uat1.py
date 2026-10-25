"""Regression tests for the owner's first UAT of the agent (branch fix/agent-uat-1).

The session: "how are my fleet's disks?" ran 37 steps of SSH probes and deep
folder scans, hit the 120k token budget and printed "I stopped because ...
Here is what I have so far" with nothing after it; "summarise" then re-ran the
same calls and stopped the same way. No sentence of answer, twice.
"""
import json
import time
import unittest
from unittest.mock import patch

from agent_fakes import app
from backend.agent import guards, loop, policy, prompts, state, store, toolbox
from backend.agent.providers.anthropic import AnthropicAdapter
from backend.agent.providers.base import ChatRequest, ProviderError
from backend.tools.read import BY_NAME
from test_agent_loop import LoopCase, call

ANSWER = "Disks: lab1 / is at 41%, nas /data at 88% — nas is the one to watch."


class AlwaysAnswerCase(LoopCase):
    def test_budget_reached_ends_with_a_real_answer(self):
        state.put({"budget_chat": {"max_steps": 3, "max_seconds": 300, "max_tokens": 100000}})
        self.script(*([{"tool_calls": [call("get_host", name="lab1")]}] * 3 + [{"text": ANSWER}]))
        rid = self.ask("how are the disks across my fleet")
        d = self.run_detail(rid)
        self.assertEqual(d["run"]["status"], "budget_exhausted")
        said = [m["content"] for m in self.messages() if m["role"] == "assistant" and m["content"]]
        self.assertEqual(said[-1], ANSWER)
        self.assertFalse(any("Here is what I have so far" in s for s in said))
        last = self.adapter.requests[-1]
        self.assertTrue(last.no_tools)
        self.assertIn("no more tool calls", last.messages[-1]["content"])
        self.assertEqual(last.messages[-1]["role"], "user")
        # the wrap-up is in the audit as a model step
        self.assertTrue(any(s["kind"] == "model" and (s["args"] or {}).get("wrap_up") for s in d["steps"]))

    def test_wrap_up_tool_calls_are_dropped_and_a_failed_wrap_up_still_says_something(self):
        state.put({"budget_chat": {"max_steps": 2, "max_seconds": 300, "max_tokens": 100000}})
        self.script({"text": "Looking at lab1 first.", "tool_calls": [call("get_host", name="lab1")]},
                    {"tool_calls": [call("get_host", name="lab1")]},
                    ProviderError("unreachable", "down"))
        rid = self.ask()
        said = [m["content"] for m in self.messages() if m["role"] == "assistant" and m["content"]]
        self.assertIn("couldn't write an answer", said[-1])
        self.assertIn("Looking at lab1 first.", said[-1])           # the last progress, not an empty promise
        self.assertEqual(self.run_detail(rid)["run"]["status"], "budget_exhausted")

    def test_error_streak_answers_every_call_then_wraps_up(self):
        bad = call("get_host", name="ghost")
        self.script({"tool_calls": [bad]}, {"tool_calls": [bad]}, {"tool_calls": [bad, call("list_hosts")]},
                    {"text": "ghost isn't a host; lab1 and nas are fine."})
        rid = self.ask()
        msgs = self.messages()
        calls = [c["id"] for m in msgs if m["role"] == "assistant" for c in (m["tool_calls"] or [])]
        answered = {m["tool_call_id"] for m in msgs if m["role"] == "tool"}
        self.assertEqual(set(calls), answered)                       # no dangling call in the history
        self.assertEqual(self.run_detail(rid)["run"]["status"], "failed")
        self.assertEqual([m["content"] for m in msgs if m["role"] == "assistant"][-1],
                         "ghost isn't a host; lab1 and nas are fine.")


class BudgetCase(unittest.TestCase):
    def test_tokens_count_what_each_call_adds(self):
        w = guards.Watch({"max_tokens": 120000})
        prompt = 9000
        for _ in range(10):                    # ten calls over a growing conversation
            w.add_usage(prompt, 150)
            prompt += 150 + 1200               # its reply and one new tool result come back next call
        self.assertLess(w.tokens, 25000)
        self.assertIsNone(guards.budget_exceeded(w))
        # a runaway that keeps adding still trips it
        for _ in range(40):
            w.add_usage(prompt, 150)
            prompt += 4000
        self.assertIn("token budget", guards.budget_exceeded(w))

    def test_defaults_fit_a_fleet_question(self):
        b = json.loads(state.DEFAULTS["budget_chat"]) if isinstance(state.DEFAULTS["budget_chat"], str) \
            else state.DEFAULTS["budget_chat"]
        self.assertGreaterEqual(b["max_steps"], 10)
        self.assertGreaterEqual(b["max_tokens"], 60000)


class TokenRunCase(LoopCase):
    def test_a_normal_ten_call_run_is_not_stopped_by_resent_prompts(self):
        state.put({"budget_chat": {"max_steps": 12, "max_seconds": 300, "max_tokens": 30000}})
        turns = [{"tool_calls": [call("get_host", name="lab1")], "usage": {"prompt": 8000 + 300 * i, "completion": 60}}
                 for i in range(10)]
        self.script(*(turns + [{"text": ANSWER, "usage": {"prompt": 11000, "completion": 80}}]))
        rid = self.ask()
        self.assertEqual(self.run_detail(rid)["run"]["status"], "done")


class FollowUpCase(LoopCase):
    def test_previous_run_results_stay_readable(self):
        big = "x" * 5000
        store.message_add(self.cid, "user", "how are the disks", run_id="r1")
        store.message_add(self.cid, "assistant", None, run_id="r1",
                          tool_calls=[{"id": "c1", "name": "list_hosts", "arguments": {}}])
        store.message_add(self.cid, "tool", big, run_id="r1", tool_call_id="c1", tool_name="list_hosts")
        store.message_add(self.cid, "assistant", "All fine.", run_id="r1")
        store.message_add(self.cid, "user", "and the GPU", run_id="r2")
        store.message_add(self.cid, "assistant", None, run_id="r2",
                          tool_calls=[{"id": "c2", "name": "get_gpu", "arguments": {}}])
        store.message_add(self.cid, "tool", big, run_id="r2", tool_call_id="c2", tool_name="get_gpu")
        store.message_add(self.cid, "assistant", "GPU idle.", run_id="r2")
        store.message_add(self.cid, "user", "summarise", run_id="r3")
        hist = loop._history({"id": "r3", "conversation_id": self.cid})
        tools = {m["tool_call_id"]: m["content"] for m in hist if m["role"] == "tool"}
        self.assertLess(len(tools["c1"]), 700)                                  # two runs back: a short head
        self.assertGreater(len(tools["c2"]), guards.PREV_RUN_VIEW_CHARS - 1)   # the run just before: readable
        self.assertLess(len(tools["c2"]), 5000)
        self.assertIn("All fine.", [m["content"] for m in hist if m["role"] == "assistant"])


class BroadFirstCase(LoopCase):
    def test_prompt_and_tools_say_start_broad(self):
        p = prompts.build(hosts=["lab1"], modes={}, rules={}, run_mode="act", attended=True)
        self.assertIn("Go from broad to deep", p)
        self.assertIn("Prefer one fleet-wide call over one call per host", p)
        self.assertIn("follow-up", p)
        self.assertIn("disks", BY_NAME["list_hosts"].description)
        self.assertIn("attention", BY_NAME["list_hosts"].description)
        self.assertIn("only to find what fills a disk", BY_NAME["scan_disk"].description)
        self.activate("lab1")
        specs, _ = toolbox.specs(toolbox.Scope())
        probe = next(s for s in specs if s["name"] == "host__disk_usage")
        self.assertIn("list_hosts", probe["description"])


class NoToolsPayloadCase(unittest.TestCase):
    def test_wrap_up_request_offers_no_callable_tools(self):
        from backend.agent.providers.openai_compat import OpenAICompatAdapter
        from backend.agent.providers.ollama import OllamaAdapter
        tools = [{"name": "list_hosts", "description": "d", "parameters": {"type": "object", "properties": {}}}]
        req = ChatRequest(model="m", system="s", messages=[{"role": "user", "content": "q"}], tools=tools, no_tools=True)
        self.assertNotIn("tools", OpenAICompatAdapter("http://x/v1").payload(req))
        self.assertNotIn("tools", OllamaAdapter("http://x").payload(req))
        a = AnthropicAdapter("https://api.anthropic.com").payload(req)
        self.assertEqual(a["tool_choice"], {"type": "none"})      # tools stay described: the history uses them


class OfflineCase(LoopCase):
    def test_offline_host_says_since_when(self):
        with app.HOST_DATA_LOCK:
            saved = app.HOST_DATA.pop("nas")
            app.HOST_DATA["nas"] = {"error": "ssh: connect timed out", "error_at": int(time.time())}
        with app.LOCK:
            app.DB.execute("INSERT INTO host_samples(ts, host, cpu) VALUES(?,?,1)", (1761300000, "nas"))
            app.DB.commit()
        try:
            self.activate("nas")
            # FakeFleet calls every host online; here the hub's real rule applies (stopped before tearDown)
            with patch("app._host_is_online", lambda e: bool(e.get("data"))):
                d = policy.decide("nas", "host.disk_usage", {}, attended=True)
                h = policy.hint(d.reason, "nas", "host.disk_usage")
            self.assertEqual(d.reason, "host_offline")
            self.assertIn("nas is offline since 2025-10-24", h)
            self.assertIn("connect timed out", h)
            self.assertNotIn("hasn't reported in", h)
        finally:
            with app.LOCK:
                app.DB.execute("DELETE FROM host_samples WHERE host='nas'")
                app.DB.commit()
            with app.HOST_DATA_LOCK:
                app.HOST_DATA["nas"] = saved

    def test_not_yet_polled_is_still_not_ready(self):
        with app.HOST_DATA_LOCK:
            saved = app.HOST_DATA.pop("nas")
        try:
            self.activate("nas")
            self.assertEqual(policy.decide("nas", "host.disk_usage", {}, attended=True).reason, "host_not_ready")
        finally:
            with app.HOST_DATA_LOCK:
                app.HOST_DATA["nas"] = saved


if __name__ == "__main__":
    unittest.main()
