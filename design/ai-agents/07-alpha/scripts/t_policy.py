"""Policy matrix, attended chat: host mode x rule for container.restart on lab1/worker, verified on the box."""
import hlm, json, time
c = hlm.Client(); c.login()

def setp(host, mode=None, action=None, rule=None, unlock=None):
    if mode:
        s, d = c.call("PUT", "/api/agent/policy/hosts/" + host, {"mode": mode}); assert s == 200, d
    if action:
        body = {"rule": rule}
        if unlock is not None: body["unattended_unlocked"] = unlock; body["acknowledge"] = True; body["confirm"] = True
        s, d = c.call("PUT", "/api/agent/policy/hosts/%s/actions/%s" % (host, action), body); assert s == 200, (s, d)

def act(host, tool, args, then="ok", decide=None):
    box = {"lab1": "hlm-target1", "lab2": "hlm-target2"}.get(host)
    before = hlm.started(box, args.get("name", "worker")) if box and "name" in args else ""
    cid = c.new_conv()
    s, d = c.ask(cid, hlm.stub([{"tool_calls": [{"name": tool, "arguments": dict(host=host, **args)}]}, {"text": then}]))
    if s != 202 and not isinstance(d, dict) or "run" not in d:
        return {"http": s, "resp": d}
    apprs = [a for a in d["approvals"] if a["status"] == "pending"]
    if apprs and decide:
        r = c.call("POST", "/api/agent/approvals/%s/decision" % apprs[0]["id"], {"decision": decide})
        d = c.wait_run(d["run"]["id"], until=("done", "failed", "refused", "cancelled"))
    time.sleep(1)
    after = hlm.started(box, args.get("name", "worker")) if box and "name" in args else ""
    tool_steps = [s for s in d["steps"] if s["kind"] not in ("model",)]
    msgs = c.call("GET", "/api/agent/conversations/" + cid)[1]["messages"]
    tool_msg = [m.get("content") for m in msgs if m["role"] == "tool"]
    return {"status": d["run"]["status"], "reason": d["run"]["status_reason"],
            "steps": [(s["kind"], s["tool"], s["decision"], s["decision_reason"], s["ok"]) for s in tool_steps],
            "approval": [(a["status"], a["rule"], a["risk"]) for a in d["approvals"]],
            "tool_said": [str(t)[:220] for t in tool_msg],
            "restarted": before != after, "before": before, "after": after}

if __name__ == "__main__":
    R = {}
    T = "container__restart"
    for mode in ("off", "passive", "active"):
        for rule in ("deny", "ask", "allow"):
            setp("lab1", mode, "container.restart", rule)
            r = act("lab1", T, {"name": "worker", "reason": "matrix"}, decide="approve")
            R[mode + "/" + rule] = r
            print(mode, rule, json.dumps(r)[:900]); print()
    setp("lab1", "active", "container.restart", "ask")
    json.dump(R, open("out_policy.json", "w"), indent=1)
