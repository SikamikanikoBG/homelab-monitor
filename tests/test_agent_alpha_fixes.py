"""Regression tests for the alpha pass-1 findings (design/ai-agents/07-alpha/FINDINGS.md).
Each test names the finding it pins down."""
import json
import time
import unittest
from unittest.mock import patch

from agent_fakes import AgentOn, FakeFleet, ScriptedAdapter, app
from backend.agent import approvals, hosts as agent_hosts, providers, store


class RenameCase(unittest.TestCase):
    def setUp(self):
        self.on = AgentOn().start()
        self.fleet = FakeFleet({"lab2": {"family": "linux", "containers": ["web"]}}).start()

    def tearDown(self):
        with app.LOCK:
            for h in ("lab2", "lab2x"):
                app.DB.execute("DELETE FROM host_samples_1h WHERE host=?", (h,))
                app.DB.execute("DELETE FROM host_samples WHERE host=?", (h,))
                app.DB.execute("DELETE FROM hosts WHERE name=?", (h,))
            app.DB.commit()
        self.fleet.stop()
        self.on.stop()

    def _sample(self, host, ts):
        with app.LOCK:
            app.DB.execute("INSERT INTO host_samples_1h(ts, host, cpu, cnt) VALUES(?,?,1,1)", (ts, host))
            app.DB.commit()

    def test_18_rename_back_to_a_former_name(self):
        self._sample("lab2", 3600)
        self.assertEqual(self.on.patch("/api/hosts/lab2", {"name": "lab2x"}).status_code, 200)
        self._sample("lab2", 3600)               # the poller wrote under the old name mid-rename
        self._sample("lab2x", 7200)
        r = self.on.patch("/api/hosts/lab2x", {"name": "lab2"})
        self.assertEqual(r.status_code, 200, r.get_json())
        with app.LOCK:
            hosts = [r[0] for r in app.DB.execute("SELECT DISTINCT host FROM host_samples_1h WHERE host IN ('lab2','lab2x')")]
        self.assertEqual(hosts, ["lab2"])

    def test_4_policy_moves_with_the_rename_and_leaves_no_orphan(self):
        self.on.put("/api/agent/policy/hosts/lab2", {"mode": "active"})
        self.on.put("/api/agent/policy/hosts/lab2/actions/container.restart", {"rule": "allow"})
        self.assertEqual(self.on.patch("/api/hosts/lab2", {"name": "lab2x"}).status_code, 200)
        self.assertEqual(store.host_modes(), {"lab2x": "active"})
        self.assertEqual(list(store.action_rules()), ["lab2x"])

    def test_4_failed_rename_changes_nothing(self):
        self.on.put("/api/agent/policy/hosts/lab2", {"mode": "active"})
        quiet = app.app.test_client()
        quiet.set_cookie("hlm_session", self.on.client.get_cookie("hlm_session").value)
        app.app.config["PROPAGATE_EXCEPTIONS"] = False
        try:
            with patch("backend.db.repos.gpu_samples.rename_host", side_effect=RuntimeError("boom")):
                r = quiet.patch("/api/hosts/lab2", json={"name": "lab2x"}, headers=self.on.h())
        finally:
            app.app.config["PROPAGATE_EXCEPTIONS"] = None
        self.assertEqual(r.status_code, 500)
        with app.LOCK:
            self.assertIsNotNone(app.DB.execute("SELECT 1 FROM hosts WHERE name='lab2'").fetchone())
        self.assertEqual(store.host_modes(), {"lab2": "active"})

    def test_4_orphans_cleaned_at_boot(self):
        store.set_host_mode("ghost", "active")
        store.set_action_rule("ghost", "container.restart", "allow", False)
        self.assertEqual(agent_hosts.prune_orphans(), 2)
        self.assertNotIn("ghost", store.host_modes())

    def test_8_rename_cancels_a_chat_run_parked_on_the_host(self):
        rid = store.run_create("c", "chat", True, "act", status="waiting_approval")
        aid, _ = approvals.propose(store.run_get(rid), "c1", "container.restart", "lab2", {"name": "web"}, None)
        self.on.patch("/api/hosts/lab2", {"name": "lab2x"})
        self.assertEqual(store.approval_get(aid)["status"], "refused")
        self.assertEqual(store.run_get(rid)["status"], "cancelled")

    def test_delete_drops_policy_in_the_same_transaction(self):
        self.on.put("/api/agent/policy/hosts/lab2", {"mode": "active"})
        self.assertEqual(self.on.delete("/api/hosts/lab2").status_code, 200)
        self.assertEqual(store.host_modes(), {})


if __name__ == "__main__":
    unittest.main()
