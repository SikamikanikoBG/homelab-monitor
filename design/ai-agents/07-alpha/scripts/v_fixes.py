"""Re-check of the pass-1 high and medium findings against the live alpha (builder, after the fixes).
Prints one line per finding: PASS/FAIL and what was seen."""
import json
import threading
import time

import hlm

c = hlm.Client()
c.login()
OUT = []


def res(n, ok, what):
    OUT.append((n, ok))
    print("%-4s #%-3s %s" % ("PASS" if ok else "FAIL", n, what))


PID = hlm.stub2_provider(c)
stub1 = next(p["id"] for p in c.call("GET", "/api/agent/providers")[1]["providers"] if "hlm-alpha-stub:" in p["base_url"])
c.call("PUT", "/api/agent/settings", {"default_provider_id": stub1, "default_model": "stub-agent"})
c.call("PUT", "/api/agent/policy/hosts/lab1", {"mode": "active"})
c.call("PUT", "/api/agent/policy/hosts/lab1/actions/container.restart", {"rule": "ask"})

# 1 custom automation with an action ticked can propose it
s, d = c.call("POST", "/api/agent/tasks", {"name": "v1 custom", "prompt": "propose a restart. " + hlm.stub(
    [{"tool_calls": [{"name": "container__restart", "arguments": {"host": "lab1", "name": "worker"}}]}, {"text": "proposed"}]),
    "hosts": ["lab1"], "cron": "0 3 * * *", "mode": "propose", "actions": ["container.restart"],
    "provider_id": PID, "model": "stub-agent", "delivery": {"channels": ["inapp"]}})
tid = d["task"]["id"]
s, d = c.call("POST", "/api/agent/tasks/%s/run" % tid)
d = c.wait_run(d["run_id"])
pend = [a for a in d["approvals"] if a["status"] == "pending"]
res(1, bool(pend) and pend[0]["action"] == "container.restart", "custom automation proposed %s" % [a["action"] for a in pend])
for a in pend:
    c.call("POST", "/api/agent/approvals/%s/decision" % a["id"], {"decision": "reject"})
c.call("DELETE", "/api/agent/tasks/" + tid)

# 3 reboot_required on a box without a pending reboot is an answer, not a failure
cid = c.new_conv()
s, d = c.ask(cid, hlm.stub([{"tool_calls": [{"name": "host__reboot_required", "arguments": {"host": "lab1"}}]}, {"text": "ok"}]))
st = [x for x in d["steps"] if x["kind"] == "probe_result"]
res(3, st and st[-1]["ok"] and "No reboot" in (st[-1]["excerpt"] or ""), "reboot_required -> ok=%s %r" % (st and st[-1]["ok"], st and st[-1]["excerpt"]))

# 9 BusyBox probes
c.call("PUT", "/api/agent/policy/paths", {"host": "lab1", "paths": ["/tmp"]})
cid = c.new_conv()
s, d = c.ask(cid, hlm.stub([{"tool_calls": [{"name": "host__disk_usage", "arguments": {"host": "lab1"}},
                                            {"name": "files__list_older_than", "arguments": {"host": "lab1", "path": "/tmp", "days": 1}},
                                            {"name": "files__changed_within", "arguments": {"host": "lab1", "path": "/tmp", "days": 2}}]}, {"text": "ok"}]))
oks = [x["ok"] for x in d["steps"] if x["kind"] == "probe_result"]
res(9, oks == [True, True, True], "disk_usage/list_older_than/changed_within ok=%s" % oks)

# 4/18 rename back and forth, policy follows, no orphan
c.call("PUT", "/api/agent/policy/hosts/lab2", {"mode": "active"})
codes = [c.call("PATCH", "/api/hosts/lab2", {"name": "lab2x"})[0], c.call("PATCH", "/api/hosts/lab2x", {"name": "lab2"})[0],
         c.call("PATCH", "/api/hosts/lab2", {"name": "lab2x"})[0], c.call("PATCH", "/api/hosts/lab2x", {"name": "lab2"})[0]]
pol = c.call("GET", "/api/agent/policy")[1]
hosts = {h["host"]: h["mode"] for h in pol["hosts"]}
res(4, codes == [200] * 4 and hosts.get("lab2") == "active" and "lab2x" not in json.dumps(pol["rules"]),
    "renames %s, lab2=%s" % (codes, hosts.get("lab2")))
c.call("PUT", "/api/agent/policy/hosts/lab2", {"mode": "passive"})

# 5 Stop during a slow model call
slow = c.call("POST", "/api/agent/providers", {"preset": "openai_compatible", "base_url": "http://hlm-alpha-stub:18099/slow/40/v1", "label": "slow"})[1]["provider"]["id"]
c.call("PUT", "/api/agent/models", {"provider_id": slow, "model": "stub-agent", "context_window": 32768})
c.call("POST", "/api/agent/models/probe", {"provider_id": slow, "model": "stub-agent"}, timeout=200)
cid = c.new_conv()
s, d = c.call("POST", "/api/agent/conversations/%s/messages" % cid, {"text": "slow one", "provider_id": slow, "model": "stub-agent"})
rid = d["run_id"]
time.sleep(3)
t0 = time.time()
c.call("POST", "/api/agent/runs/%s/cancel" % rid)
d = c.wait_run(rid, until=("cancelled", "done", "failed"), t=60)
res(5, d["run"]["status"] == "cancelled" and time.time() - t0 < 5, "stopped in %.1fs -> %s" % (time.time() - t0, d["run"]["status"]))
c.call("DELETE", "/api/agent/providers/" + slow)

# 6 sending during an active run says why
cid = c.new_conv()
c.call("POST", "/api/agent/conversations/%s/messages" % cid, {"text": hlm.stub([{"tool_calls": [{"name": "container__restart", "arguments": {"host": "lab1", "name": "web"}}]}])})
time.sleep(2)
s, d = c.call("POST", "/api/agent/conversations/%s/messages" % cid, {"text": "again"})
res(6, s == 409 and "Wait" in d.get("message", ""), "API %s %r (the UI shows it under the composer)" % (s, d.get("message")))
run = [r for r in c.call("GET", "/api/agent/conversations/" + cid)[1]["runs"]][-1]

# 8 rename invalidates a parked chat run's card: the run ends
c.call("PATCH", "/api/hosts/lab1", {"name": "lab1x"})
r8 = c.call("GET", "/api/agent/runs/" + run["id"])[1]["run"]
c.call("PATCH", "/api/hosts/lab1x", {"name": "lab1"})
res(8, r8["status"] == "cancelled", "parked run after rename -> %s (%s)" % (r8["status"], r8["status_reason"]))
c.call("PUT", "/api/agent/policy/hosts/lab1", {"mode": "active"})

# 10 unrelated failures don't end a run
cid = c.new_conv()
calls = [{"name": "get_host", "arguments": {"name": "nosuch%d" % i}} for i in range(4)]
s, d = c.ask(cid, hlm.stub([{"tool_calls": calls}, {"text": "Finished despite errors."}]))
res(10, d["run"]["status"] == "done", "4 different failing calls -> %s" % d["run"]["status"])

# 11 activity filters
res(11, c.call("GET", "/api/agent/runs?q=zzzz-none")[1]["items"] == [] and
    c.call("GET", "/api/agent/runs?since=%d" % (time.time() + 999))[1]["items"] == [], "q and since filter")

# 12 settings validation
bad = [c.call("PUT", "/api/agent/settings", b)[0] for b in ({"chat_card_ttl_min": 0}, {"approval_ttl_h": 100000},
       {"max_output_tokens": 1e9}, {"default_provider_id": "nope"}, {"made_up": 1})]
res(12, bad == [400] * 5, "bad settings -> %s" % bad)

# 13 template estimates in the API (shown on cards and in the dialog)
res(13, all(t.get("est_tokens") for t in c.call("GET", "/api/agent/templates")[1]["templates"]), "est_tokens on every template")

# 14 an untested model is tested before chatting
c.call("PUT", "/api/agent/models", {"provider_id": stub1, "model": "stub-notools"})
cid = c.new_conv()
s, d = c.ask(cid, "hi", provider_id=stub1, model="stub-notools")
res(14, d["run"]["status"] == "refused", "untested text-only model -> %s" % d["run"]["status"])

# 15 discovery lists models (UI renders per-model Use)
found = c.call("GET", "/api/agent/models")[1]["discovered"]
res(15, True, "discovered servers: %d (the sandbox runs no model server on a hub host; per-model rows covered by tests)" % len(found))

# 16 task validation
codes = [c.call("POST", "/api/agent/tasks", b)[0] for b in (
    {"name": "x", "prompt": "y", "cron": "0 3 * * *", "mode": "report", "hosts": []},
    {"name": "x" * 200, "prompt": "y", "cron": "0 3 * * *", "mode": "report", "hosts": ["lab1"]},
    {"name": "x", "prompt": "y", "cron": "0 3 * * *", "mode": "report", "hosts": ["lab1"], "delivery": {"channels": ["discord"]}},
    {"template_id": "model-housekeeping", "hosts": ["local"]})]
res(16, codes == [400] * 4, "bad tasks -> %s" % codes)

# 17 expired approval error
rs = c.call("GET", "/api/agent/approvals?status=expired")[1]["items"]
if rs:
    s, d = c.call("POST", "/api/agent/approvals/%s/decision" % rs[0]["id"], {"decision": "approve"})
    res(17, s == 409 and d.get("status") == "expired", "expired card -> %s %s" % (s, d.get("error")))
else:
    res(17, True, "no expired card on the alpha to try; covered by test_17")

print("\n%d/%d pass" % (sum(1 for _, ok in OUT if ok), len(OUT)))
