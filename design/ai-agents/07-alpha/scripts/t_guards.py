"""EC-3 loops/malformed, EC-4 huge results, EC-21 dedupe, budgets incl. a partial budget object."""
import hlm, json, time
from t_policy import setp, c
def run(turns, label):
    cid = c.new_conv()
    t0 = time.time()
    s, d = c.ask(cid, hlm.stub(turns))
    if not isinstance(d, dict) or "run" not in d:
        print(label, s, d); return
    msgs = c.call("GET", "/api/agent/conversations/" + cid)[1]["messages"]
    last = [m.get("content") for m in msgs if m["role"] == "assistant" and m.get("content")]
    print(f"{label:38s} {d['run']['status']}/{d['run']['status_reason']} steps={d['run']['steps']} {time.time()-t0:.0f}s last={str(last[-1:])[:200]}")
    return d, msgs
same = {"tool_calls": [{"name": "list_hosts", "arguments": {}}]}
run([same] * 30, "same tool x30")
run([{"tool_calls": [{"name": "get_host", "arguments": {"name": "nope%d" % i}}]} for i in range(30)], "failing tool x30")
run([{"tool_calls": [{"name": "no_such_tool", "arguments": {}}]}] * 30, "unknown tool x30")
run([{"tool_calls": [{"name": "get_host", "arguments": "not-json{"}]}] * 30, "malformed args x30")
run([{"text": ""}] * 5, "empty replies")
setp("lab1", "active", "container.restart", "allow")
d, m = run([{"tool_calls": [{"name": "container__restart", "arguments": {"host": "lab1", "name": "web"}}]},
            {"tool_calls": [{"name": "container__restart", "arguments": {"host": "lab1", "name": "web", "reason": "really"}}]}, {"text": "done"}], "EC-21 restart twice")
print("   results:", [str(x.get("content"))[:100] for x in m if x["role"] == "tool"])
setp("lab1", "active", "container.restart", "ask")
d, m = run([{"tool_calls": [{"name": "container__restart", "arguments": {"host": "lab1", "name": "web"}}, {"name": "container__restart", "arguments": {"host": "lab1", "name": "web"}}]}, {"text": "done"}], "two identical calls in one turn (ask)")
print("   approvals:", [(a["status"]) for a in d["approvals"]])
for a in d["approvals"]:
    if a["status"] == "pending": c.call("POST", "/api/agent/approvals/%s/decision" % a["id"], {"decision": "reject"})
# partial budget
orig = c.call("GET", "/api/agent/settings")[1]["budget_chat"]
c.call("PUT", "/api/agent/settings", {"budget_chat": {"max_steps": 40}})
print("budget after partial PUT:", c.call("GET", "/api/agent/settings")[1]["budget_chat"])
run([same] * 30, "same tool x30, budget {max_steps:40}")
c.call("PUT", "/api/agent/settings", {"budget_chat": orig})
