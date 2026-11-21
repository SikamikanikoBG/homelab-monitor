import hlm, time
from t_policy import setp, c
setp("lab1", "active", "container.restart", "ask")
cid = c.new_conv()
s, d = c.ask(cid, hlm.stub([{"tool_calls": [{"name": "container__restart", "arguments": {"host": "lab1", "name": "worker"}}]}, {"text": "x"}]))
ap = [a for a in d["approvals"] if a["status"] == "pending"][0]
setp("lab1", "passive")
s, r = c.call("POST", "/api/agent/approvals/%s/decision" % ap["id"], {"decision": "approve"})
print("#30 approve after passive:", s, r.get("status"), r.get("approval", {}).get("status"), r.get("approval", {}).get("result"), r.get("message"))
setp("lab1", "active")
