"""Pass 2 #1: actions picker in New from a prompt; Propose -> pending approval; Act+Allow -> real restart."""
from ui import *
import hlm, json, time
api = hlm.Client(); api.login()
PID = hlm.stub2_provider(api)
api.call("PUT", "/api/agent/policy/hosts/lab1", {"mode": "active"})
api.call("PUT", "/api/agent/policy/hosts/lab1/actions/container.restart", {"rule": "allow"})
STUB = hlm.stub([{"tool_calls": [{"name": "container__restart", "arguments": {"host": "lab1", "name": "worker", "reason": "p2"}}]}, {"text": "done"}])
def create(pg, name, mode):
    agent(pg, "auto")
    pg.locator("#ag-cockpit button", has_text="New from a prompt").click(); pg.wait_for_timeout(800)
    pg.fill("#agd-name", name)
    pg.fill("#agd-prompt", "Check lab1 and restart worker. " + STUB)
    dlg = pg.locator("[role=dialog], dialog[open]").last
    for lbl in dlg.locator("label").all():
        t = lbl.inner_text().strip()
        if t == "local": lbl.locator("input").uncheck()
        if t == "lab1": lbl.locator("input").check()
    txt = dlg.inner_text()
    print("dialog text (actions part):", txt[txt.find("Actions") if "Actions" in txt else 0:][:600].replace("\n", " | "))
    cb = dlg.locator("label", has_text="Restart a container").locator("input")
    print("restart checkbox count:", cb.count())
    if cb.count(): cb.first.check()
    pg.select_option("#agd-mode", mode)
    pg.select_option("#agd-model", PID + "|stub-agent")
    shot(pg, "p2-new-prompt-" + mode, full=True)
    dlg.locator("button", has_text="Activate").last.click(); pg.wait_for_timeout(1500)
    t = next(t for t in api.call("GET", "/api/agent/tasks")[1]["items"] if t["name"] == name)
    print(name, "saved actions:", t["actions"], "mode", t["mode"], "hosts", t["hosts"])
    return t
with sync_playwright() as p:
    b = browser(p)
    pg = page(b, 1440, 900)
    t = create(pg, "p2 propose", "propose")
    before = hlm.started("hlm-target1", "worker")
    s, d = api.call("POST", "/api/agent/tasks/%s/run" % t["id"]); d = api.wait_run(d["run_id"])
    print("propose run:", d["run"]["status"], [(s["kind"], s["decision_reason"]) for s in d["steps"] if s["kind"] != "model"], [(a["status"]) for a in d["approvals"]], "restarted:", before != hlm.started("hlm-target1", "worker"))
    for a in d["approvals"]:
        if a["status"] == "pending": api.call("POST", "/api/agent/approvals/%s/decision" % a["id"], {"decision": "reject"})
    t2 = create(pg, "p2 act", "act")
    # Edit dialog shows picker with restart ticked?
    agent(pg, "auto"); pg.locator("#ag-cockpit button", has_text="My automations").first.click(); pg.wait_for_timeout(800)
    pg.locator("tr", has_text="p2 act").locator("button", has_text="Edit").click(); pg.wait_for_timeout(800)
    shot(pg, "p2-edit-dialog", full=True)
    print("edit: restart ticked:", pg.locator("[role=dialog] label, dialog label", has_text="Restart a container").locator("input").first.is_checked())
    pg.keyboard.press("Escape")
    before = hlm.started("hlm-target1", "worker")
    s, d = api.call("POST", "/api/agent/tasks/%s/run" % t2["id"]); d = api.wait_run(d["run_id"], until=("done", "failed", "refused", "cancelled"))
    time.sleep(2)
    print("act run:", d["run"]["status"], [(s["kind"], s["decision_reason"], s["ok"]) for s in d["steps"] if s["kind"] != "model"], "restarted:", before != hlm.started("hlm-target1", "worker"), hlm.started("hlm-target1", "worker"))
    for t_ in (t, t2): api.call("DELETE", "/api/agent/tasks/" + t_["id"])
    dump_errors()
api.call("PUT", "/api/agent/policy/hosts/lab1/actions/container.restart", {"rule": "ask"})
