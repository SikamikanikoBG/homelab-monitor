"""EC-9/EC-29: approve while the host is unreachable (paused container)."""
import hlm, json, time
from t_policy import setp, c
for _ in range(60):
    if c.call("GET", "/api/agent/status")[1]["running"] == 0: break
    time.sleep(2)
setp("lab1", "active", "container.restart", "ask")
cid = c.new_conv()
s, d = c.ask(cid, hlm.stub([{"tool_calls": [{"name": "container__restart", "arguments": {"host": "lab1", "name": "worker"}}]}, {"text": "after offline"}]))
ap = [a for a in d["approvals"] if a["status"] == "pending"][0]
print(hlm.ardi("docker pause hlm-target1"))
t0 = time.time()
print("approve:", c.call("POST", "/api/agent/approvals/%s/decision" % ap["id"], {"decision": "approve"}, timeout=120)[0])
d = c.wait_run(d["run"]["id"], until=("done", "failed", "refused", "cancelled"), t=180)
print("run: %s/%s after %.0fs" % (d["run"]["status"], d["run"]["status_reason"], time.time() - t0))
print("steps:", [(s["kind"], s["ok"], str(s["excerpt"])[:150]) for s in d["steps"] if s["kind"] not in ("model",)])
print("approval:", [(a["status"], a.get("result")) for a in d["approvals"]])
print(hlm.ardi("docker unpause hlm-target1"))
time.sleep(5)
print("worker:", hlm.started("hlm-target1", "worker"))
