"""Approvals: double approve (parallel), policy change before approval, expiry, reject, rename."""
import hlm, json, time, threading
from t_policy import setp, c

def card(host="lab1", name="worker", tool="container__restart"):
    cid = c.new_conv()
    s, d = c.ask(cid, hlm.stub([{"tool_calls": [{"name": tool, "arguments": {"host": host, "name": name, "reason": "appr test"}}]}, {"text": "after card"}]))
    ap = [a for a in d["approvals"] if a["status"] == "pending"]
    return d["run"]["id"], (ap[0] if ap else None), d

def results(rid):
    d = c.wait_run(rid, until=("done", "failed", "refused", "cancelled"), t=60)
    return d["run"]["status"], [(s["kind"], s["decision"], s["decision_reason"], s["ok"]) for s in d["steps"] if s["kind"] not in ("model", "tool")]

setp("lab1", "active", "container.restart", "ask")
c.call("PUT", "/api/agent/policy/protected", {"host": "lab1", "kind": "container", "name": "worker", "protected": False})
# 1 parallel approve (two clients = two tabs)
c2 = hlm.Client(); c2.login()
rid, ap, _ = card()
before = hlm.started("hlm-target1", "worker")
codes = []
def go(cl): codes.append(cl.call("POST", "/api/agent/approvals/%s/decision" % ap["id"], {"decision": "approve"})[0])
ts = [threading.Thread(target=go, args=(c if i % 2 else c2,)) for i in range(12)]
[t.start() for t in ts]; [t.join() for t in ts]
st = results(rid)
print("1 parallel approve codes:", sorted(codes), "run:", st, "action_results:", sum(1 for x in st[1] if x[0] == "action_result"))
# 1b approve then reject
print("1b reject after approve:", c.call("POST", "/api/agent/approvals/%s/decision" % ap["id"], {"decision": "reject"}))
# 2 policy -> passive before approve
rid, ap, _ = card()
setp("lab1", "passive")
before = hlm.started("hlm-target1", "worker")
r = c.call("POST", "/api/agent/approvals/%s/decision" % ap["id"], {"decision": "approve"})
time.sleep(2)
print("2 passive-then-approve:", r, results(rid), "restarted:", before != hlm.started("hlm-target1", "worker"))
setp("lab1", "active")
# 3 rule -> deny before approve
rid, ap, _ = card()
setp("lab1", None, "container.restart", "deny")
before = hlm.started("hlm-target1", "worker")
r = c.call("POST", "/api/agent/approvals/%s/decision" % ap["id"], {"decision": "approve"})
time.sleep(2)
print("3 deny-then-approve:", r, results(rid), "restarted:", before != hlm.started("hlm-target1", "worker"))
setp("lab1", None, "container.restart", "ask")
# 4 protected target before approve
rid, ap, _ = card()
print("protect:", c.call("PUT", "/api/agent/policy/protected", {"host": "lab1", "kind": "container", "name": "worker", "protected": True}))
print("policy protected:", c.call("GET", "/api/agent/policy")[1]["protected"])
r = c.call("POST", "/api/agent/approvals/%s/decision" % ap["id"], {"decision": "approve"})
time.sleep(2)
print("4 protected-then-approve:", r, results(rid))
rid2, ap2, d2 = card()
print("4b propose on protected:", d2["run"]["status"], [(s["kind"], s["decision"], s["decision_reason"]) for s in d2["steps"] if s["kind"] not in ("model", "tool")])
print("unprotect:", c.call("PUT", "/api/agent/policy/protected", {"host": "lab1", "kind": "container", "name": "worker", "protected": False}))
for a in c.call("GET", "/api/agent/approvals")[1]["items"][:6]:
    print("   approval list:", a["id"][:8], a["status"], a.get("result"))
# 5 reject
rid, ap, _ = card()
r = c.call("POST", "/api/agent/approvals/%s/decision" % ap["id"], {"decision": "reject"})
d = c.wait_run(rid, until=("done", "failed", "refused", "cancelled"), t=60)
cid = d["run"]["conversation_id"]
msgs = c.call("GET", "/api/agent/conversations/" + cid)[1]["messages"]
print("5 reject:", r, d["run"]["status"], [str(m.get("content"))[:120] for m in msgs if m["role"] == "tool"])
# 6 bad decision values
rid, ap, _ = card()
for dec in ("APPROVE", "yes", None, ""):
    print("6 decision", repr(dec), c.call("POST", "/api/agent/approvals/%s/decision" % ap["id"], {"decision": dec}))
c.call("POST", "/api/agent/approvals/%s/decision" % ap["id"], {"decision": "reject"})
json.dump({"protected_note": "remove worker protection after"}, open("out_appr.json", "w"))
