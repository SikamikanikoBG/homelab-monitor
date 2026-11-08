"""US-2 providers: key write-only, error kinds, env:/file: refs, SSRF-ish URLs, EC-12/13."""
import hlm, json, time
c = hlm.Client(); c.login()
KEY = "sk-alpha-PROV-SECRET-4242"
made = []
def add(**body):
    s, d = c.call("POST", "/api/agent/providers", body)
    if s == 200: made.append(d["provider"]["id"])
    return s, d
s, d = add(preset="openai_compatible", base_url="http://hlm-alpha-stub:18099/err/401/v1", api_key=KEY, label="err401")
print("POST response contains key:", KEY in json.dumps(d), "last4:", d.get("provider", {}).get("key_last4"))
print("test 401:", c.call("POST", "/api/agent/providers/%s/test" % d["provider"]["id"])[1])
for code in ("429", "500"):
    s, d = add(preset="openai_compatible", base_url="http://hlm-alpha-stub:18099/err/%s/v1" % code, label="err" + code)
    print("test", code, c.call("POST", "/api/agent/providers/%s/test" % d["provider"]["id"])[1])
s, d = add(preset="openai_compatible", base_url="http://10.255.255.1:9/v1", label="unreach")
t0 = time.time(); print("test unreachable:", c.call("POST", "/api/agent/providers/%s/test" % d["provider"]["id"])[1], "%.0fs" % (time.time() - t0))
s, d = add(preset="openai_compatible", base_url="http://nonexistent.invalid/v1", label="dns")
print("test dns:", c.call("POST", "/api/agent/providers/%s/test" % d["provider"]["id"])[1])
# real OpenAI with a fake key (one request, no side effects)
s, d = add(preset="openai", api_key="sk-fake-alpha-000000000000", label="openai fake")
print("test real openai fake key:", c.call("POST", "/api/agent/providers/%s/test" % d["provider"]["id"])[1])
# env / file refs
for ref in ("env:HOME", "env:DOES_NOT_EXIST", "file:/etc/hostname", "file:/data/gpu.db", "file:/proc/self/environ", "file:../../etc/passwd"):
    s, d = add(preset="openai_compatible", base_url="http://hlm-alpha-stub2:18099/v1", api_key=ref, label="ref " + ref)
    print("ref", ref, s, json.dumps(d.get("provider", d))[:250])
# weird URLs
for u in ("file:///etc/passwd", "javascript:alert(1)", "http://127.0.0.1:9800/api/backup", "http://localhost:9800/v1", "gopher://x", "", "http://[::1]:9800/v1"):
    s, d = add(preset="openai_compatible", base_url=u, label="url")
    tr = c.call("POST", "/api/agent/providers/%s/test" % d["provider"]["id"])[1] if s == 200 else None
    print("url", repr(u), s, (d.get("error"), d.get("message")) if s != 200 else "", json.dumps(tr)[:200] if tr else "")
# key never comes back
s, all_ = c.call("GET", "/api/agent/providers")
print("GET providers contains secret:", KEY in json.dumps(all_), "| PATCH label:", KEY in json.dumps(c.call("PATCH", "/api/agent/providers/" + made[0], {"label": "renamed"})[1]))
print("PATCH api_key empty string keeps key?:", c.call("PATCH", "/api/agent/providers/" + made[0], {"api_key": ""})[1].get("provider", {}).get("key_set"))
for pid in made: c.call("DELETE", "/api/agent/providers/" + pid)
