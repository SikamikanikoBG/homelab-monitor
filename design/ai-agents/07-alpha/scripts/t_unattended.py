"""Unattended (scheduled) policy: Run now and a real cron fire, verified on lab1."""
import hlm, json, time, sys
from t_policy import setp, c
PID = hlm.stub2_provider(c)

def task(turns, mode="act", actions=None, hosts=("lab1",), cron="0 4 1 1 *", name="alpha probe"):
    body = {"name": name, "prompt": hlm.stub(turns), "hosts": list(hosts), "mode": mode, "cron": cron,
            "delivery": {"channels": ["inapp"]}}
    if actions is not None: body["actions"] = actions
    body["provider_id"] = PID; body["model"] = "stub-agent"
    s, d = c.call("POST", "/api/agent/tasks", body)
    assert s == 200, (s, d)
    return d["task"]

def run_task(t, real_fire=False):
    if real_fire:
        # wait for the next minute's fire
        for _ in range(130):
            f = c.call("GET", "/api/agent/tasks/%s/fires" % t["id"])[1]
            items = f.get("items") or f.get("fires") or []
            rid = next((x.get("run_id") for x in items if x.get("run_id")), None)
            if rid: break
            time.sleep(1)
        else:
            return {"fires": f}
    else:
        s, d = c.call("POST", "/api/agent/tasks/%s/run" % t["id"]); assert s == 202, (s, d); rid = d["run_id"]
    d = c.wait_run(rid, t=120)
    return d

def summary(d, before, after):
    return {"status": d["run"]["status"], "attended": d["run"]["attended"], "mode": d["run"]["mode"],
            "steps": [(s["kind"], s["tool"], s["decision"], s["decision_reason"]) for s in d["steps"] if s["kind"] != "model"],
            "approvals": [(a["status"], a["action"], a["attended"]) for a in d["approvals"]],
            "restarted": before != after}

def case(label, action, toolname, args, rule, unlock=False, mode="act", real=False, actions=None, decide=None):
    setp("lab1", "active", action, rule, unlock)
    t = task([{"tool_calls": [{"name": toolname, "arguments": dict(host=args.pop("_host","lab1"), **args)}]}, {"text": "done"}],
             mode=mode, actions=[action] if actions is None else actions, cron="* * * * *" if real else "0 4 1 1 *", name="alpha " + label)
    before = hlm.started("hlm-target1", args.get("name", "worker"))
    d = run_task(t, real_fire=real)
    if real: c.call("PATCH", "/api/agent/tasks/" + t["id"], {"enabled": False})
    after = hlm.started("hlm-target1", args.get("name", "worker"))
    if "run" not in d:
        print(label, "NO RUN", d); return
    r = summary(d, before, after)
    print(f"{label:45s} {json.dumps(r)[:700]}")
    for a in d["approvals"]:
        if a["status"] == "pending": c.call("POST", "/api/agent/approvals/%s/decision" % a["id"], {"decision": "reject"})
    c.call("DELETE", "/api/agent/tasks/" + t["id"])
    return r

if __name__ == "__main__":
    which = sys.argv[1:] or ["runnow"]
    if "runnow" in which:
        case("RunNow restart allow", "container.restart", "container__restart", {"name": "worker"}, "allow")
        case("RunNow restart allow, task actions=[]", "container.restart", "container__restart", {"name": "worker"}, "allow", actions=[])
        case("RunNow restart allow, mode=propose", "container.restart", "container__restart", {"name": "worker"}, "allow", mode="propose")
        case("RunNow restart allow, mode=report", "container.restart", "container__restart", {"name": "worker"}, "allow", mode="report")
        case("RunNow stop allow no unlock", "container.stop", "container__stop", {"name": "worker"}, "allow")
    if "fire" in which:
        pass  # case("FIRE restart allow", "container.restart", "container__restart", {"name": "worker"}, "allow", real=True)
        pass  # case("FIRE stop allow no unlock", "container.stop", "container__stop", {"name": "worker"}, "allow", real=True)
        pass  # case("FIRE stop allow + unlock", "container.stop", "container__stop", {"name": "worker"}, "allow", unlock=True, real=True)
        hlm.ardi("docker exec hlm-target1 docker start worker")
        case("FIRE service.restart allow + unlock", "service.restart", "service__restart", {"name": "sshd"}, "allow", real=True)
        case("FIRE ollama.remove allow + unlock", "ollama.remove_model", "ollama__remove_model", {"name": "llama3:8b"}, "allow", real=True)
        case("FIRE restart ask", "container.restart", "container__restart", {"name": "worker"}, "ask", real=True)
    setp("lab1", "active", "container.restart", "ask")
    setp("lab1", "active", "container.stop", "deny", False)
