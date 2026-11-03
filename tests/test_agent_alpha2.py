"""Regression tests for the alpha pass-2 findings (design/ai-agents/07-alpha/FINDINGS.md, "Pass 2").

N1: a model may claim an action waits on the owner without ever calling the
action tool. Its text is left alone; next to it every surface shows the run's
own record of what it proposed, ran or was refused.
"""
import unittest
from unittest.mock import patch

from agent_fakes import app
from backend.agent import approvals, outcome, prompts, store
from test_agent_loop import LoopCase, call

CLAIM = "Action waiting on you: restart of worker on lab1. Approve it in the card above."


class FactsCase(LoopCase):
    def test_n1_claimed_but_never_called_shows_no_actions(self):
        self.activate("lab1", container__restart="ask")
        self.script({"tool_calls": [call("get_containers", host="lab1")]}, {"text": CLAIM})
        rid = self.ask("is worker ok on lab1?")
        d = self.run_detail(rid)
        self.assertEqual(d["run"]["status"], "done")
        self.assertEqual(d["approvals"], [])
        self.assertEqual(d["run"]["actions"], [])
        self.assertEqual(d["run"]["actions_text"], "No actions were proposed in this run.")
        conv = self.on.get("/api/agent/conversations/" + self.cid).get_json()
        self.assertEqual(conv["runs"][-1]["actions"], [])
        # the model's words are kept as they were: nothing is filtered or rewritten
        self.assertEqual([m["content"] for m in conv["messages"] if m["role"] == "assistant"][-1], CLAIM)

    def test_n1_delivery_message_carries_the_facts(self):
        app.save_settings({"ntfy_topic": "lab"})
        self.addCleanup(app.save_settings, {"ntfy_topic": ""})
        self.script({"text": CLAIM})
        t = self.on.post("/api/agent/tasks", {"name": "t", "prompt": "check", "hosts": ["lab1"], "cron": "0 3 * * *",
                                              "mode": "report", "provider_id": self.pid, "model": "m1",
                                              "delivery": {"channels": ["ntfy"]}}).get_json()["task"]
        with patch("backend.agent.ports.dispatch_to_channels") as d:
            self.on.post("/api/agent/tasks/%s/run" % t["id"])
        level, title, detail, chans = d.call_args[0]
        self.assertIn(CLAIM, detail)
        self.assertIn("No actions were proposed in this run.", detail)
        self.assertEqual(level, "info")                  # a claim alone doesn't make it a warning

    def test_n1_a_real_proposal_is_listed_pending_then_ran(self):
        self.activate("lab1", container__restart="ask")
        self.script({"tool_calls": [call("container__restart", host="lab1", name="worker")]}, {"text": "Restarted."})
        rid = self.ask("restart worker on lab1")
        facts = self.run_detail(rid)["run"]["actions"]
        self.assertEqual([(f["outcome"], f["action"], f["host"], f["target"]) for f in facts],
                         [("pending", "container.restart", "lab1", "worker")])
        self.assertIn("#agent/approvals", outcome.footer(facts))
        aid = facts[0]["approval_id"]
        self.on.post("/api/agent/approvals/%s/decision" % aid, {"decision": "approve"})
        facts = self.run_detail(rid)["run"]["actions"]
        self.assertEqual([(f["outcome"], f["approval_id"]) for f in facts], [("ran", aid)])   # one line, not two

    def test_n1_refused_and_unattended_proposals(self):
        self.activate("lab1", container__restart="deny")
        self.script({"tool_calls": [call("container__restart", host="lab1", name="web")]}, {"text": "ok"})
        rid = self.ask("restart web on lab1")
        facts = self.run_detail(rid)["run"]["actions"]
        self.assertEqual([f["outcome"] for f in facts], ["refused"])
        self.assertTrue(facts[0]["detail"])
        self.assertIn("Refused: Restart a container on lab1 (web)", outcome.footer(facts))

    def test_n1_probes_and_reads_are_not_actions(self):
        self.activate("lab1")
        self.script({"tool_calls": [call("list_hosts"), call("host__disk_usage", host="lab1")]}, {"text": "fine"})
        rid = self.ask()
        self.assertEqual(self.run_detail(rid)["run"]["actions"], [])

    def test_n1_prompt_says_when_an_action_is_pending(self):
        p = prompts.build(hosts=["lab1"], modes={}, rules={}, run_mode="act", attended=True)
        self.assertIn("only proposed, waiting for approval or done once you have called its action tool", p)
        self.assertIn("approval id", p)


class ExpiredCase(LoopCase):
    def test_17_card_already_swept_to_expired_answers_expired(self):
        rid = store.run_create("c", "schedule", False, "act")
        aid, _ = approvals.propose(store.run_get(rid), "c1", "container.restart", "lab1", {"name": "web"}, None)
        with app.LOCK:
            app.DB.execute("UPDATE agent_approvals SET expires_at=1 WHERE id=?", (aid,))
            app.DB.commit()
        approvals.expire()
        self.assertEqual(store.approval_get(aid)["status"], "expired")
        r = self.on.post("/api/agent/approvals/%s/decision" % aid, {"decision": "approve"})
        self.assertEqual((r.status_code, r.get_json()["error"], r.get_json()["status"]), (409, "expired", "expired"))

    def test_17_a_decided_card_still_says_already_decided(self):
        rid = store.run_create("c", "schedule", False, "act")
        aid, _ = approvals.propose(store.run_get(rid), "c1", "container.restart", "lab1", {"name": "web"}, None)
        self.on.post("/api/agent/approvals/%s/decision" % aid, {"decision": "reject"})
        r = self.on.post("/api/agent/approvals/%s/decision" % aid, {"decision": "approve"})
        self.assertEqual(r.get_json()["error"], "already_decided")


if __name__ == "__main__":
    unittest.main()
