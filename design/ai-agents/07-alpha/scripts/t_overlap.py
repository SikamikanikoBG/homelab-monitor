"""EC-11: two schedules on the same host fire in the same minute (AGENT_MAX_RUNS=2)."""
import hlm, json, time
c = hlm.Client(); c.login()
slow = [p for p in c.call("GET", "/api/agent/providers")[1]["providers"] if "/slow/" in p["base_url"]][0]
tids = []
for i in range(2):
    s, d = c.call("POST", "/api/agent/tasks", {"name": "overlap %d" % i, "prompt": "check", "hosts": ["lab1"], "mode": "act", "actions": ["container.restart"],
                                               "cron": "* * * * *", "delivery": {"channels": ["inapp"]}, "provider_id": slow["id"], "model": "stub-agent"})
    print(s, d.get("error") or d.get("fields")); tids.append(d["task"]["id"])
time.sleep(75)
for t in tids:
    c.call("PATCH", "/api/agent/tasks/" + t, {"enabled": False})
for t in tids:
    print(t[:6], c.call("GET", "/api/agent/tasks/%s/fires" % t)[1]["items"][:3])
print("status:", c.call("GET", "/api/agent/status")[1]["running"])
time.sleep(60)
for t in tids:
    f = c.call("GET", "/api/agent/tasks/%s/fires" % t)[1]["items"]
    for x in f:
        if x.get("run_id"):
            r = c.call("GET", "/api/agent/runs/" + x["run_id"])[1]["run"]; print(t[:6], x["outcome"], r["status"], r["status_reason"])
    c.call("DELETE", "/api/agent/tasks/" + t)
