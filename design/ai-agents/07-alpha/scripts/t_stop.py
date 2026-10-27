"""US-12 Stop via API: how fast does a run end while the model is slow; busy-send answer; pause all."""
import hlm, time, json
c = hlm.Client(); c.login()
# wait for queue to drain
for _ in range(60):
    if c.call("GET", "/api/agent/status")[1]["running"] == 0: break
    time.sleep(2)
slow = [p for p in c.call("GET", "/api/agent/providers")[1]["providers"] if "/slow/" in p["base_url"]][0]
cid = c.new_conv()
s, r = c.call("POST", "/api/agent/conversations/%s/messages" % cid, {"text": "slow", "provider_id": slow["id"], "model": "stub-agent"})
time.sleep(3)
print("busy send same conv:", c.call("POST", "/api/agent/conversations/%s/messages" % cid, {"text": "again"}))
cid2 = c.new_conv()
print("send other conv while busy (MAX_RUNS=1):", c.call("POST", "/api/agent/conversations/%s/messages" % cid2, {"text": "queued?"}))
t0 = time.time()
print("cancel:", c.call("POST", "/api/agent/runs/%s/cancel" % r["run_id"]))
while time.time() - t0 < 60:
    st = c.call("GET", "/api/agent/runs/" + r["run_id"])[1]["run"]
    if st["status"] not in ("running", "queued"): break
    time.sleep(0.5)
print("stopped after %.1fs: %s %s" % (time.time() - t0, st["status"], st["status_reason"]))
print("cancel again:", c.call("POST", "/api/agent/runs/%s/cancel" % r["run_id"]))
print("cancel bogus:", c.call("POST", "/api/agent/runs/nope/cancel"))
# pause all
print("pause:", c.call("POST", "/api/agent/pause", {"paused": True}))
tasks = c.call("GET", "/api/agent/tasks")[1]["items"]
t = tasks[0]
print("Run now while paused:", c.call("POST", "/api/agent/tasks/%s/run" % t["id"]))
print("status:", c.call("GET", "/api/agent/status")[1])
print("chat while paused:", c.call("POST", "/api/agent/conversations/%s/messages" % c.new_conv(), {"text": "hi"})[0])
print("unpause:", c.call("POST", "/api/agent/pause", {"paused": False}))
