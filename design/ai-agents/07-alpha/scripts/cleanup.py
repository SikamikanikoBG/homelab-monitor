"""Put the alpha back to its hand-over state (keeps the DST tasks and the stub2 provider for re-tests)."""
import hlm, json
c = hlm.Client(); c.login()
for t in c.call("GET", "/api/agent/tasks")[1]["items"]:
    if not t["name"].startswith("DST check"):
        print("delete task", t["name"], c.call("DELETE", "/api/agent/tasks/" + t["id"])[0])
for p in c.call("GET", "/api/agent/providers")[1]["providers"]:
    if p["label"] in ("slow stub", "Stub Ollama @ local"):
        print("delete provider", p["label"], c.call("DELETE", "/api/agent/providers/" + p["id"])[0])
c.call("POST", "/api/settings", {"custom_ai_servers": ""})
for a in ("container.stop", "service.restart", "ollama.remove_model", "journal.vacuum", "docker.prune_stopped_containers", "container.set_restart_policy"):
    c.call("PUT", "/api/agent/policy/hosts/lab1/actions/" + a, {"rule": "deny"})
c.call("PUT", "/api/agent/policy/hosts/lab1/actions/container.restart", {"rule": "ask"})
c.call("PUT", "/api/agent/policy/hosts/lab1/actions/docker.prune_dangling_images", {"rule": "allow"})
c.call("PUT", "/api/agent/policy/hosts/lab1", {"mode": "active"})
c.call("PUT", "/api/agent/policy/hosts/lab2", {"mode": "passive"})
c.call("PUT", "/api/agent/policy/hosts/local", {"mode": "passive"})
print("paths", c.call("PUT", "/api/agent/policy/paths", {"host": "lab1", "paths": []}))
c.call("PUT", "/api/agent/settings", {"default_provider_id": "534c1b034b2a4b2c92bdde0434f4ebdd", "default_model": "stub-agent", "budget_chat": {"max_seconds": 300, "max_steps": 12, "max_tokens": 120000}})
for a in c.call("GET", "/api/agent/approvals")[1]["items"]:
    if a["status"] == "pending": c.call("POST", "/api/agent/approvals/%s/decision" % a["id"], {"decision": "reject"})
for r in c.call("GET", "/api/agent/runs?status=waiting_approval")[1]["items"]:
    c.call("POST", "/api/agent/runs/%s/cancel" % r["id"])
print(json.dumps(c.call("GET", "/api/agent/policy")[1]["rules"]))
print(c.call("GET", "/api/agent/status")[1])
