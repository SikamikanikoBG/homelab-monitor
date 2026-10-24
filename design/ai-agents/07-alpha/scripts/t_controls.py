"""EC-10 restart mid-run, EC-15 ENABLE_CONTROLS=0 (pending approval made before, approved after)."""
import hlm, json, time, threading
from t_policy import setp, c
# slow provider
url = "http://hlm-alpha-stub2:18099/slow/40/v1"
pid = next((p["id"] for p in c.call("GET", "/api/agent/providers")[1]["providers"] if p["base_url"] == url), None)
if not pid:
    pid = c.call("POST", "/api/agent/providers", {"preset": "openai_compatible", "base_url": url, "label": "slow stub"})[1]["provider"]["id"]
# pending approval before the restart
setp("lab1", "active", "container.restart", "ask")
cid = c.new_conv()
s, d = c.ask(cid, hlm.stub([{"tool_calls": [{"name": "container__restart", "arguments": {"host": "lab1", "name": "worker"}}]}, {"text": "after"}]))
pend = [a for a in d["approvals"] if a["status"] == "pending"][0]
print("pending approval", pend["id"][:8])
cid2 = c.new_conv()
s, r = c.call("POST", "/api/agent/conversations/%s/messages" % cid2, {"text": "slow one", "provider_id": pid, "model": "stub-agent"})
print("slow run", s, r)
time.sleep(4)
print("status before restart:", c.call("GET", "/api/agent/runs/" + r["run_id"])[1]["run"]["status"])
print(hlm.ardi("bash hlm-alpha-hub_env.sh ENABLE_CONTROLS=0", timeout=120))
c = hlm.Client(); c.login()
time.sleep(3)
d = c.call("GET", "/api/agent/runs/" + r["run_id"])[1]
print("slow run after restart:", d["run"]["status"], d["run"]["status_reason"])
print("status:", c.call("GET", "/api/agent/status")[1])
print("pending approval after restart:", [ (a["id"][:8], a["status"]) for a in c.call("GET", "/api/agent/approvals")[1]["items"] if a["id"] == pend["id"]])
before = hlm.started("hlm-target1", "worker")
print("approve under controls=0:", c.call("POST", "/api/agent/approvals/%s/decision" % pend["id"], {"decision": "approve"})[0])
time.sleep(3)
print("restarted:", before != hlm.started("hlm-target1", "worker"))
print("approval now:", [(a["status"], a.get("result")) for a in c.call("GET", "/api/agent/approvals")[1]["items"] if a["id"] == pend["id"]])
setp("lab1", "active", "container.restart", "allow")
cid = c.new_conv()
s, d = c.ask(cid, hlm.stub([{"tool_calls": [{"name": "container__restart", "arguments": {"host": "lab1", "name": "worker"}}]}, {"text": "after"}]))
msgs = c.call("GET", "/api/agent/conversations/" + cid)[1]["messages"]
print("allow under controls=0:", [(s["kind"], s["decision_reason"]) for s in d["steps"] if s["kind"] != "model"], [m["content"] for m in msgs if m["role"] == "tool"])
print("restarted:", before != hlm.started("hlm-target1", "worker"))
cid = c.new_conv()
s, d = c.ask(cid, hlm.stub([{"tool_calls": [{"name": "docker__system_df", "arguments": {"host": "lab1"}}]}, {"text": "after"}]))
print("probe under controls=0:", [(s["kind"], s["decision_reason"], s["ok"]) for s in d["steps"] if s["kind"] != "model"])
print("dashboard container action under controls=0:", c.call("POST", "/api/containers/web/action", {"action": "restart", "host": "lab1"}))
print("policy PUT under controls=0:", c.call("PUT", "/api/agent/policy/hosts/lab1/actions/container.stop", {"rule": "allow"}))
setp("lab1", "active", "container.restart", "ask")
