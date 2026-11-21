"""Schedules: validation, cron preview incl. DST, templates, pause stops fires, PATCH/DELETE."""
import hlm, json, time, sys
c = hlm.Client(); c.login()
P = lambda *a: print(*[json.dumps(x, ensure_ascii=False)[:400] if not isinstance(x, str) else x for x in a])
for cron in ("0 3 * * *", "30 3 * * *", "0 * * * *", "*/0 * * * *", "61 * * * *", "bogus", "0 0 31 2 *", "@daily", "0 4 * * 1-5", ""):
    P("preview", repr(cron), c.call("POST", "/api/agent/cron/preview", {"cron": cron, "tz": "Europe/Sofia", "count": 5, "from": "2026-10-25T00:00"}))
base = {"name": "v", "prompt": "Report hosts.", "hosts": ["lab1"], "mode": "report", "cron": "0 4 1 1 *", "delivery": {"channels": ["inapp"]}}
for k, v in [("cron", "bogus"), ("hosts", []), ("hosts", ["nope"]), ("mode", "yolo"), ("name", "x" * 5000), ("prompt", "y" * 200000),
             ("prompt", ""), ("delivery", {"channels": ["discord"]}), ("actions", ["rm.rf"]), ("actions", ["files.list_older_than"]), ("provider_id", "nope"),
             ("cron", "* * * * *"), ("catch_up", "all")]:
    s, d = c.call("POST", "/api/agent/tasks", dict(base, **{k: v}))
    P(f"task {k}={str(v)[:30]!r}", s, d if s != 200 else {"ok": True, k: d["task"].get(k) if k not in ("prompt", "name") else len(d["task"][k])})
    if s == 200: c.call("DELETE", "/api/agent/tasks/" + d["task"]["id"])
# Off host in task
c.call("PUT", "/api/agent/policy/hosts/lab2", {"mode": "off"})
s, d = c.call("POST", "/api/agent/tasks", dict(base, hosts=["lab2"])); P("task on Off host", s, d.get("error") or d.get("task", {}).get("hosts"))
if s == 200: c.call("DELETE", "/api/agent/tasks/" + d["task"]["id"])
c.call("PUT", "/api/agent/policy/hosts/lab2", {"mode": "passive"})
# templates
tpls = {t["id"]: t for t in c.call("GET", "/api/agent/templates")[1]["templates"]}
P("templates", list(tpls))
for tid, t in tpls.items():
    P(tid, "eligible", t["eligible_hosts"], "mode", t["default_mode"], "actions", [a["id"] for a in t["actions"]], "est_tokens", t["est_tokens"])
s, d = c.call("POST", "/api/agent/tasks", {"template_id": "model-housekeeping", "hosts": ["lab1"], "mode": "propose", "delivery": {"channels": ["inapp"]}})
P("activate model-housekeeping on ineligible lab1", s, d.get("error") or "ok")
if s == 200: c.call("DELETE", "/api/agent/tasks/" + d["task"]["id"])
s, d = c.call("POST", "/api/agent/tasks", {"template_id": "docker-cleanup", "hosts": ["lab1"], "mode": "act", "delivery": {"channels": ["inapp"]}})
P("activate docker-cleanup", s, {k: d["task"][k] for k in ("cron", "actions", "mode", "tools", "next_fire_local")} if s == 200 else d)
tid = d["task"]["id"]
P("PATCH cron+mode", c.call("PATCH", "/api/agent/tasks/" + tid, {"cron": "15 2 * * *", "mode": "propose"}))
P("PATCH bad cron", c.call("PATCH", "/api/agent/tasks/" + tid, {"cron": "nope"}))
P("PATCH actions widen", c.call("PATCH", "/api/agent/tasks/" + tid, {"actions": ["container.stop"]}))
P("DELETE", c.call("DELETE", "/api/agent/tasks/" + tid))
P("DELETE again", c.call("DELETE", "/api/agent/tasks/" + tid))
P("fires of deleted", c.call("GET", "/api/agent/tasks/%s/fires" % tid))
if "pause" in sys.argv:
    PID = hlm.stub2_provider(c)
    c.call("POST", "/api/agent/pause", {"paused": True})
    s, d = c.call("POST", "/api/agent/tasks", dict(base, cron="* * * * *", name="paused probe", provider_id=PID, model="stub-agent"))
    t = d["task"]; time.sleep(130)
    P("fires while paused", c.call("GET", "/api/agent/tasks/%s/fires" % t["id"]))
    c.call("POST", "/api/agent/pause", {"paused": False}); time.sleep(75)
    P("fires after unpause", c.call("GET", "/api/agent/tasks/%s/fires" % t["id"]))
    c.call("DELETE", "/api/agent/tasks/" + t["id"])
