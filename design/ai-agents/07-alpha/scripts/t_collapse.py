"""US-10: unattended proposals park, collapse across runs, show in inbox; approve later executes against original run."""
import hlm, json, time
from t_unattended import task, c, PID
from t_policy import setp
setp("lab1", "active", "container.stop", "allow", False)
t = task([{"tool_calls": [{"name": "container__stop", "arguments": {"host": "lab1", "name": "worker", "reason": "nightly check"}}]}, {"text": "proposed a stop"}],
         mode="act", actions=["container.stop"], name="collapse probe")
ids = []
for i in range(2):
    s, d = c.call("POST", "/api/agent/tasks/%s/run" % t["id"]); d = c.wait_run(d["run_id"])
    ids.append(d["run"]["id"])
    print("run", i, d["run"]["status"], d["run"]["status_reason"], [(a["id"][:8], a["status"], a["seen_count"]) for a in d["approvals"]])
pend = [a for a in c.call("GET", "/api/agent/approvals")[1]["items"] if a["status"] == "pending" and a["action"] == "container.stop"]
print("pending stop approvals:", [(a["id"][:8], a["seen_count"], a["run_id"][:8], a["expires_at"] - a["created_at"]) for a in pend])
print("status badge count:", c.call("GET", "/api/agent/status")[1]["pending_approvals"])
json.dump({"task": t["id"], "approval": pend[0]["id"] if pend else None, "runs": ids}, open("out_collapse.json", "w"))
