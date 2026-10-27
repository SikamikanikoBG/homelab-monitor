import hlm, json
c = hlm.Client(); c.login()
for pid, label in (("80b86aca5d1e4bb1ba2362bd5226fee0", "tested no"), ("534c1b034b2a4b2c92bdde0434f4ebdd", "untested")):
    cid = c.new_conv()
    s, d = c.call("POST", "/api/agent/conversations/%s/messages" % cid, {"text": "how is my lab?", "provider_id": pid, "model": "stub-notools"})
    if s == 202:
        d = c.wait_run(d["run_id"])
        msgs = c.call("GET", "/api/agent/conversations/" + cid)[1]["messages"]
        print(label, d["run"]["status"], d["run"]["status_reason"], [m["content"] for m in msgs if m["role"] == "assistant"])
    else:
        print(label, s, d)
print("set notools as default:", c.call("PUT", "/api/agent/settings", {"default_provider_id": "80b86aca5d1e4bb1ba2362bd5226fee0", "default_model": "stub-notools"})[0])
print(c.call("GET", "/api/agent/status")[1]["default"])
c.call("PUT", "/api/agent/settings", {"default_provider_id": "534c1b034b2a4b2c92bdde0434f4ebdd", "default_model": "stub-agent"})
