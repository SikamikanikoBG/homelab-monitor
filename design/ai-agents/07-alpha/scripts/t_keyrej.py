"""EC-12/EC-13: a task on a provider that answers 401 / 429."""
import hlm, json
c = hlm.Client(); c.login()
for code in ("401", "429"):
    s, d = c.call("POST", "/api/agent/providers", {"preset": "openai_compatible", "base_url": "http://hlm-alpha-stub:18099/err/%s/v1" % code, "label": "err" + code})
    pid = d["provider"]["id"]
    s, t = c.call("POST", "/api/agent/tasks", {"name": "err task " + code, "prompt": "check", "hosts": ["lab1"], "mode": "report", "cron": "0 4 1 1 *",
                                               "delivery": {"channels": ["inapp"]}, "provider_id": pid, "model": "stub-agent"})
    print(code, "create task", s, t.get("fields"))
    if s != 200:
        c.call("DELETE", "/api/agent/providers/" + pid); continue
    tid = t["task"]["id"]
    s, r = c.call("POST", "/api/agent/tasks/%s/run" % tid); d = c.wait_run(r["run_id"], t=120)
    msgs = c.call("GET", "/api/agent/conversations/" + d["run"]["conversation_id"])[1]["messages"]
    print(code, "run", d["run"]["status"], d["run"]["status_reason"], [m["content"] for m in msgs if m["role"] == "assistant"][-1:])
    print(code, "task warning:", [x.get("warning") for x in c.call("GET", "/api/agent/tasks")[1]["items"] if x["id"] == tid])
    c.call("DELETE", "/api/agent/tasks/" + tid); c.call("DELETE", "/api/agent/providers/" + pid)
