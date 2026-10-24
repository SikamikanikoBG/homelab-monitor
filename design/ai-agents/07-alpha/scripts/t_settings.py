import hlm, json, time
from t_policy import setp, c
orig = c.call("GET", "/api/agent/settings")[1]
for k, v in [("chat_card_ttl_min", 0), ("chat_card_ttl_min", -5), ("chat_card_ttl_min", "abc"), ("approval_ttl_h", 0),
             ("approval_ttl_h", 100000), ("tz", "Mars/Olympus"), ("tz", "UTC"), ("rate_limit_per_day", -1),
             ("budget_chat", {"max_steps": 0}), ("budget_chat", {"max_steps": 100000}), ("cost_cap_day", "-1"),
             ("default_provider_id", "nope"), ("bogus_key", 1), ("max_output_tokens", 10**9)]:
    s, d = c.call("PUT", "/api/agent/settings", {k: v})
    print(f"{k:20s} {v!r:28} -> {s} {json.dumps(d)[:160]}")
    c.call("PUT", "/api/agent/settings", {k: orig.get(k)} if k in orig and k != "tz_effective" else {})
c.call("PUT", "/api/agent/settings", {"tz": ""})
now = c.call("GET", "/api/agent/settings")[1]
print("restored diff:", {k: (orig[k], now.get(k)) for k in orig if orig[k] != now.get(k)})
# expiry
setp("lab1", "active", "container.restart", "ask")
print(c.call("PUT", "/api/agent/settings", {"chat_card_ttl_min": 1}))
cid = c.new_conv()
s, d = c.ask(cid, hlm.stub([{"tool_calls": [{"name": "container__restart", "arguments": {"host": "lab1", "name": "worker"}}]}, {"text": "after"}]))
ap = [a for a in d["approvals"] if a["status"] == "pending"][0]
print("expires in", ap["expires_at"] - ap["created_at"], "s")
time.sleep(75)
before = hlm.started("hlm-target1", "worker")
print("approve after expiry:", c.call("POST", "/api/agent/approvals/%s/decision" % ap["id"], {"decision": "approve"}))
time.sleep(2)
print("restarted:", before != hlm.started("hlm-target1", "worker"))
d = c.call("GET", "/api/agent/runs/" + d["run"]["id"])[1]
print("run:", d["run"]["status"], d["run"]["status_reason"], [a["status"] for a in d["approvals"]])
c.call("PUT", "/api/agent/settings", {"chat_card_ttl_min": orig["chat_card_ttl_min"]})
