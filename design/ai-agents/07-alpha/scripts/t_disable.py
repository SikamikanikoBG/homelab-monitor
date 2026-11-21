"""EC-36 runtime disable; password reset CLI; US-3 enable without password."""
import hlm, json, time
from t_policy import setp, c
setp("lab1", "active", "container.restart", "ask")
cid = c.new_conv()
s, d = c.ask(cid, hlm.stub([{"tool_calls": [{"name": "container__restart", "arguments": {"host": "lab1", "name": "worker"}}]}, {"text": "x"}]))
ap = [a for a in d["approvals"] if a["status"] == "pending"][0]
slow = [p for p in c.call("GET", "/api/agent/providers")[1]["providers"] if "/slow/" in p["base_url"]][0]
s, r = c.call("POST", "/api/agent/conversations/%s/messages" % c.new_conv(), {"text": "slow", "provider_id": slow["id"], "model": "stub-agent"})
time.sleep(2)
print("disable:", c.call("POST", "/api/agent/enable", {"enabled": False}))
time.sleep(2)
print("status:", c.call("GET", "/api/agent/status")[1])
print("approval after disable:", [(a["status"], a.get("result")) for a in c.call("GET", "/api/agent/approvals")[1]["items"] if a["id"] == ap["id"]])
print("approve after disable:", c.call("POST", "/api/agent/approvals/%s/decision" % ap["id"], {"decision": "approve"}))
print("slow run after disable:", c.call("GET", "/api/agent/runs/" + r["run_id"])[1]["run"]["status"])
print("chat while disabled:", c.call("POST", "/api/agent/conversations/%s/messages" % c.new_conv(), {"text": "hi"}))
print("providers kept:", len(c.call("GET", "/api/agent/providers")[1]["providers"]), "runs kept:", len(c.call("GET", "/api/agent/runs")[1]["items"]))
