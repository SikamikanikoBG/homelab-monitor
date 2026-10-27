"""US-9: a custom automation from the UI - which actions can it propose?"""
from ui import *
import hlm, json
api = hlm.Client(); api.login()
PID = hlm.stub2_provider(api)
with sync_playwright() as p:
    b = browser(p)
    pg = page(b, 1440, 900)
    agent(pg, "auto")
    reqs = []
    pg.on("request", lambda r: reqs.append(r.post_data) if r.method == "POST" and r.url.endswith("/api/agent/tasks") else None)
    pg.locator("button", has_text="New from a prompt").click(); pg.wait_for_timeout(800)
    pg.fill("#agd-name", "UI custom restart proposer")
    pg.fill("#agd-prompt", "Every night check lab1 and propose restarting worker. " + hlm.stub([{"tool_calls": [{"name": "container__restart", "arguments": {"host": "lab1", "name": "worker"}}]}, {"text": "proposed"}]))
    pg.locator("[role=dialog] label, dialog label", has_text="lab1").locator("input").check()
    pg.select_option("#agd-mode", "propose")
    pg.select_option("#agd-model", PID + "|stub-agent")
    shot(pg, "a-desk-new-prompt-filled")
    pg.locator("[role=dialog] button, dialog button", has_text="Activate").last.click(); pg.wait_for_timeout(1500)
    print("UI POST body:", reqs)
t = next(t for t in api.call("GET", "/api/agent/tasks")[1]["items"] if t["name"] == "UI custom restart proposer")
print("task actions:", t["actions"], "mode", t["mode"])
s, d = api.call("POST", "/api/agent/tasks/%s/run" % t["id"])
d = api.wait_run(d["run_id"])
print("run:", d["run"]["status"], [(s["kind"], s["decision_reason"]) for s in d["steps"] if s["kind"] != "model"])
cid = d["run"]["conversation_id"]
print("tool said:", [m["content"] for m in api.call("GET", "/api/agent/conversations/" + cid)[1]["messages"] if m["role"] == "tool"])
api.call("DELETE", "/api/agent/tasks/" + t["id"])
