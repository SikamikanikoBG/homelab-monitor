"""US-1 discovery via a custom AI server (the stub speaks Ollama), EC-2 no-tools model, US-2 key handling."""
import hlm, json, time
c = hlm.Client(); c.login()
srv = [{"name": "Stub Ollama", "provider": "ollama", "host": "hlm-alpha-stub", "port": 18099}]
print("set custom server:", c.call("POST", "/api/settings", {"custom_ai_servers": json.dumps(srv)})[0])
d = c.call("GET", "/api/agent/models")[1]
print("discovered:", json.dumps(d["discovered"])[:800])
