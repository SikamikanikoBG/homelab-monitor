import hlm, time
from t_unattended import task, c
c.call("PUT", "/api/agent/settings", {"rate_limit_per_day": 50})
c.call("PUT", "/api/agent/policy/hosts/lab1/actions/container.restart", {"rule": "allow"})
t = task([{"tool_calls": [{"name": "container__restart", "arguments": {"host": "lab1", "name": "worker", "reason": "p2"}}]}, {"text": "done"}], mode="act", actions=["container.restart"], name="p2 act api")
before = hlm.started("hlm-target1", "worker")
s, d = c.call("POST", "/api/agent/tasks/%s/run" % t["id"]); d = c.wait_run(d["run_id"], until=("done", "failed", "refused", "cancelled"))
time.sleep(2)
after = hlm.started("hlm-target1", "worker")
print("act+allow:", d["run"]["status"], [(s["kind"], s["decision_reason"], s["ok"]) for s in d["steps"] if s["kind"] != "model"], before, "->", after)
c.call("DELETE", "/api/agent/tasks/" + t["id"])
c.call("PUT", "/api/agent/settings", {"rate_limit_per_day": 3})
c.call("PUT", "/api/agent/policy/hosts/lab1/actions/container.restart", {"rule": "ask"})
