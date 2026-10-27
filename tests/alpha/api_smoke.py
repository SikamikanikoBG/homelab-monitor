#!/usr/bin/env python3
"""End-to-end check of a running alpha instance through its HTTP API, stdlib only.

    python tests/alpha/api_smoke.py http://ardi:9831 <password> [model_base_url model]

Default model: the stub server inside the alpha network. Checks: login, enable,
provider + tool probe, a read-only chat, a confirm card approved and executed on
a sandbox box, a Passive refusal that names the setting, an automation that
parks its proposal, the audit export and that no key leaks. Prints one line per
check and exits non-zero on the first failure.
"""
import http.cookiejar
import json
import sys
import time
import urllib.error
import urllib.request

BASE = sys.argv[1].rstrip("/")
PW = sys.argv[2]
MODEL_URL = sys.argv[3] if len(sys.argv) > 3 else "http://hlm-alpha-stub:18099/v1"
MODEL = sys.argv[4] if len(sys.argv) > 4 else "stub-agent"
STUB = MODEL == "stub-agent"
KEY = "sk-alpha-NEVER-SHOWN-9876"

jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
CSRF = {"v": ""}


def call(method, path, body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json", "X-CSRF-Token": CSRF["v"]})
    try:
        with opener.open(req, timeout=timeout) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw.startswith(("{", "[")) else raw)
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw


def ok(cond, what, detail=""):
    print(("ok   " if cond else "FAIL ") + what + ((" — " + str(detail)[:300]) if detail and not cond else ""))
    if not cond:
        sys.exit(1)


def wait_run(rid, until=("done", "failed", "refused", "cancelled", "waiting_approval", "budget_exhausted"), t=240):
    end = time.time() + t
    while time.time() < end:
        s, d = call("GET", "/api/agent/runs/" + rid)
        if d["run"]["status"] in until:
            return d
        time.sleep(1)
    return d


def ask(cid, text):
    s, d = call("POST", "/api/agent/conversations/%s/messages" % cid, {"text": text})
    ok(s == 202, "message accepted: " + text[:40], d)
    return wait_run(d["run_id"])


s, d = call("POST", "/api/auth/login", {"password": PW})
ok(s == 200, "login", d)
CSRF["v"] = d["csrf"]
ok(call("GET", "/api/agent/status")[1]["password_set"], "status says a password is set")
ok(call("POST", "/api/agent/enable", {"enabled": True})[1]["enabled"], "agent enabled")

provs = call("GET", "/api/agent/providers")[1]["providers"]
prov = next((p for p in provs if p["base_url"] == MODEL_URL), None)
if not prov:
    s, d = call("POST", "/api/agent/providers", {"preset": "openai_compatible" if STUB else "vllm",
                                                 "base_url": MODEL_URL, "label": "stub" if STUB else "vader vLLM"})
    ok(s == 200, "provider added", d)
    prov = d["provider"]
s, d = call("POST", "/api/agent/providers/%s/test" % prov["id"])
ok(d.get("ok"), "provider test lists models", d)
s, d = call("POST", "/api/agent/models/probe", {"provider_id": prov["id"], "model": MODEL}, timeout=300)
ok(d.get("tools") == "yes", "tool probe: tools=%s multi_step=%s" % (d.get("tools"), d.get("multi_step")), d)
call("PUT", "/api/agent/settings", {"default_provider_id": prov["id"], "default_model": MODEL})

# a cloud provider with a key, to check it never comes back
s, d = call("POST", "/api/agent/providers", {"preset": "openai", "api_key": KEY, "label": "leak check"})
leak_id = d["provider"]["id"]

for _ in range(60):
    hosts = {h["host"]: h for h in call("GET", "/api/agent/policy")[1]["hosts"]}
    if all(hosts.get(h, {}).get("online") for h in ("lab1", "lab2")):
        break
    time.sleep(2)
ok(all(hosts.get(h, {}).get("online") for h in ("lab1", "lab2")), "sandbox boxes online", hosts.keys())
ok("docker" in hosts["lab1"]["capabilities"], "lab1 has docker", hosts["lab1"])

cid = call("POST", "/api/agent/conversations")[1]["id"]
d = ask(cid, "Which hosts do I have and are they healthy?" if not STUB else "How is my lab?")
ok(d["run"]["status"] == "done", "read-only chat answered", d["run"])
tools = [s["tool"] for s in d["steps"] if s["kind"] == "tool"]
ok(tools, "it looked with tools: %s" % tools)

call("PUT", "/api/agent/policy/hosts/lab1", {"mode": "active"})
call("PUT", "/api/agent/policy/hosts/lab1/actions/container.restart", {"rule": "ask"})
cid2 = call("POST", "/api/agent/conversations")[1]["id"]
text = ('#stub [{"tool_calls":[{"name":"container__restart","arguments":{"host":"lab1","name":"web",'
        '"reason":"alpha check"}}]},{"text":"Restarted web on lab1."}]') if STUB else "Restart the web container on lab1."
d = ask(cid2, text)
ok(d["run"]["status"] == "waiting_approval", "restart parks on a confirm card", d["run"])
appr = [a for a in d["approvals"] if a["status"] == "pending"][0]
ok(appr["host"] == "lab1" and appr["action"] == "container.restart", "card shows host and action", appr)
s, r = call("POST", "/api/agent/approvals/%s/decision" % appr["id"], {"decision": "approve"})
ok(s == 200, "approved", r)
s, r2 = call("POST", "/api/agent/approvals/%s/decision" % appr["id"], {"decision": "approve"})
ok(s == 409, "second approval refused (exactly once)", r2)
d = wait_run(d["run"]["id"], until=("done", "failed", "refused", "cancelled"))
res = [s for s in d["steps"] if s["kind"] == "action_result"]
ok(res and res[-1]["ok"], "restart executed on lab1", res)

cid3 = call("POST", "/api/agent/conversations")[1]["id"]
text = ('#stub [{"tool_calls":[{"name":"container__restart","arguments":{"host":"lab2","name":"web"}}]},'
        '{"text":"lab2 is Passive."}]') if STUB else "Restart the web container on lab2."
d = ask(cid3, text)
msgs = call("GET", "/api/agent/conversations/" + cid3)[1]["messages"]
said = " ".join((m.get("content") or "") for m in msgs if m["role"] in ("tool", "assistant"))
ok("Passive" in said or "passive" in said, "lab2 refusal names the setting", said[-300:])
ok(not [s for s in d["steps"] if s["kind"] == "action_result"], "nothing ran on lab2")

s, d = call("POST", "/api/agent/tasks", {"template_id": "docker-cleanup", "hosts": ["lab1"], "mode": "propose",
                                         "delivery": {"channels": ["inapp"]}})
ok(s == 200, "Docker cleanup activated (propose)", d)
tid = d["task"]["id"]
call("PUT", "/api/agent/policy/hosts/lab1/actions/docker.prune_dangling_images", {"rule": "allow"})
if STUB:
    # The stub follows its default script; a scheduled run must at least finish and report.
    pass
s, d = call("POST", "/api/agent/tasks/%s/run" % tid)
ok(s == 202, "Run now", d)
d = wait_run(d["run_id"], until=("done", "failed", "refused", "cancelled", "budget_exhausted"), t=600)
ok(d["run"]["status"] in ("done", "budget_exhausted"), "automation finished: %s" % d["run"]["status"], d["run"])
ok(not [s for s in d["steps"] if s["kind"] == "action_result"], "propose mode ran no action")

for path in ("/api/agent/providers", "/api/agent/audit/export", "/api/agent/runs", "/api/agent/admin-log"):
    s, body = call("GET", path)
    ok(KEY not in json.dumps(body), "no key in " + path)
call("DELETE", "/api/agent/providers/" + leak_id)
s, body = call("GET", "/api/agent/audit/export?format=csv")
ok(s == 200 and "container.restart" in body, "audit export has the restart")
print("ALPHA API SMOKE PASSED")
