"""EC-30 rename with a pending approval; policy follows rename; EC-6 unicode name."""
import hlm, json, time
from t_policy import setp, c
setp("lab2", "active", "container.restart", "ask")
cid = c.new_conv()
s, d = c.ask(cid, hlm.stub([{"tool_calls": [{"name": "container__restart", "arguments": {"host": "lab2", "name": "worker"}}]}, {"text": "x"}]))
ap = [a for a in d["approvals"] if a["status"] == "pending"][0]
print("rename lab2->lab2x:", c.call("PATCH", "/api/hosts/lab2", {"name": "lab2x"}))
time.sleep(2)
pol = c.call("GET", "/api/agent/policy")[1]
print("policy hosts:", [(h["host"], h["mode"]) for h in pol["hosts"]], "rules:", list(pol["rules"]))
print("approval:", [(a["host"], a["status"], a.get("result")) for a in c.call("GET", "/api/agent/approvals")[1]["items"] if a["id"] == ap["id"]])
before = hlm.started("hlm-target2", "worker")
print("approve:", c.call("POST", "/api/agent/approvals/%s/decision" % ap["id"], {"decision": "approve"}))
time.sleep(2); print("restarted:", before != hlm.started("hlm-target2", "worker"))
print("unicode rename:", c.call("PATCH", "/api/hosts/lab2x", {"name": "лаб-2 🚀"}))
print("rename back:", c.call("PATCH", "/api/hosts/lab2x", {"name": "lab2"}))
time.sleep(2)
pol = c.call("GET", "/api/agent/policy")[1]
print("policy hosts after:", [(h["host"], h["mode"], h["online"]) for h in pol["hosts"]])
setp("lab2", "passive")
