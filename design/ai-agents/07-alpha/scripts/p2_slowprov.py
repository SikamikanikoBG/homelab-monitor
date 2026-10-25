import hlm
c = hlm.Client(); c.login()
url = "http://hlm-alpha-stub2:18099/slow/40/v1"
p = next((p for p in c.call("GET", "/api/agent/providers")[1]["providers"] if p["base_url"] == url), None)
if not p:
    p = c.call("POST", "/api/agent/providers", {"preset": "openai_compatible", "base_url": url, "label": "slow stub"})[1]["provider"]
print(c.call("POST", "/api/agent/models/probe", {"provider_id": p["id"], "model": "stub-agent"}, timeout=200))
