"""Auth, scopes, CSRF, origin, MCP. Prints a line per check."""
import hlm, json, urllib.request, urllib.error
c = hlm.Client(); c.login()
out = {}
def show(name, r): print(f"{name:55s} {r[0]} {(json.dumps(r[1])[:200] if not isinstance(r[1], str) else r[1][:200]).encode("ascii","replace").decode()}")

# no session
anon = hlm.Client()
for p in ["/api/agent/status", "/api/agent/conversations", "/api/agent/audit/export", "/api/agent/providers", "/api/backup", "/api/data"]:
    show("anon GET " + p, anon.call("GET", p))
show("anon POST approvals decision", anon.call("POST", "/api/agent/approvals/x/decision", {"decision": "approve"}))
# keys
import os
keys = json.load(open("keys.json")) if os.path.exists("keys.json") else {}
for scope in ([] if keys else ("read", "push", "admin")):
    r = c.call("POST", "/api/integration/keys", {"name": "alpha-" + scope, "scope": scope})
    show("mint " + scope, r)
    keys[scope] = r[1].get("key") or r[1].get("token") or r[1].get("api_key")
json.dump(keys, open("keys.json", "w"))
show("list keys (session)", c.call("GET", "/api/integration/keys"))
for scope, k in keys.items():
    kc = hlm.Client(); kc.headers = {"Authorization": "Bearer %s" % k}
    show(scope + " GET status", kc.call("GET", "/api/agent/status"))
    show(scope + " GET conversations", kc.call("GET", "/api/agent/conversations"))
    show(scope + " GET audit export", kc.call("GET", "/api/agent/audit/export", raw=True))
    show(scope + " GET providers", kc.call("GET", "/api/agent/providers"))
    show(scope + " GET backup", kc.call("GET", "/api/backup", raw=True))
    show(scope + " GET integration keys", kc.call("GET", "/api/integration/keys"))
    show(scope + " POST conversation", kc.call("POST", "/api/agent/conversations"))
    show(scope + " POST pause(false)", kc.call("POST", "/api/agent/pause", {"paused": False}))
    show(scope + " PUT policy lab2 passive", kc.call("PUT", "/api/agent/policy/hosts/lab2", {"mode": "passive"}))
    show(scope + " POST approve bogus", kc.call("POST", "/api/agent/approvals/bogus/decision", {"decision": "approve"}))
    xk = hlm.Client(); xk.headers = {"X-API-Key": k}
    show(scope + " X-API-Key GET status", xk.call("GET", "/api/agent/status"))
# CSRF
nc = hlm.Client(); nc.login(); tok = nc.csrf; nc.csrf = ""
show("session POST no csrf", nc.call("POST", "/api/agent/conversations"))
nc.csrf = "wrong"
show("session POST wrong csrf", nc.call("POST", "/api/agent/conversations"))
nc.csrf = tok
show("session POST csrf + evil Origin", nc.call("POST", "/api/agent/conversations", headers={"Origin": "http://evil.example"}))
show("session POST csrf + evil Referer only", nc.call("POST", "/api/agent/conversations", headers={"Referer": "http://evil.example/x"}))
show("session POST csrf + Origin null", nc.call("POST", "/api/agent/conversations", headers={"Origin": "null"}))
show("session POST csrf + good Origin", nc.call("POST", "/api/agent/conversations", headers={"Origin": hlm.BASE}))
show("session PUT policy no csrf (form ctype)", nc.call("PUT", "/api/agent/policy/hosts/lab2", {"mode": "passive"}, headers={"Content-Type": "text/plain"}, csrf=False))
# cookie flags
r = urllib.request.urlopen(urllib.request.Request(hlm.BASE + "/api/auth/login", data=json.dumps({"password": hlm.PW}).encode(), headers={"Content-Type": "application/json"}, method="POST"))
print("Set-Cookie:", r.headers.get_all("Set-Cookie"))
print("headers:", {k: v for k, v in r.headers.items() if k.lower().startswith(("x-", "content-security", "referrer", "strict"))})
# MCP
for h in ({}, {"Authorization": "Bearer " + keys["read"]}, {"Authorization": "Bearer " + keys["push"]}):
    req = urllib.request.Request(hlm.BASE.replace("9831", "9841") + "/mcp", data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}).encode(), headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream", **h}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as rr: print("MCP", list(h) and h["Authorization"][:12], rr.status, rr.read()[:120])
    except urllib.error.HTTPError as e: print("MCP", list(h) and h["Authorization"][:12], e.code, e.read()[:120])
    except Exception as e: print("MCP err", e)
