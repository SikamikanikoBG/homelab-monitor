from ui import *
import hlm, sys, time
api = hlm.Client(); api.login()
w = int(sys.argv[1]) if len(sys.argv) > 1 else 1440
tag = "a-desk" if w > 500 else "a-phone"
with sync_playwright() as p:
    b = browser(p)
    pg = page(b, w, 900 if w > 500 else 844)
    agent(pg, "auto")
    card = pg.locator("text=Docker cleanup").first.locator("xpath=ancestor::*[.//button[contains(., 'Activate')]][1]")
    card.locator("button", has_text="Activate").click()
    pg.wait_for_timeout(800)
    pg.fill("#agd-name", "UI cleanup <b>x</b> 🧹 тест")
    pg.locator("label", has_text="local").locator("input").uncheck()
    pg.locator("label", has_text="lab1").locator("input").check()
    pg.select_option("#agd-sched", label="Custom cron…")
    pg.wait_for_timeout(500)
    shot(pg, tag + "-activate-custom")
    cr = pg.locator("input[id*=cron], #agd-cron")
    if cr.count():
        cr.first.fill("61 * * * *"); pg.wait_for_timeout(800); shot(pg, tag + "-activate-badcron")
        print("bad cron feedback:", pg.inner_text("[role=dialog], dialog, .ag-dlg")[-600:].replace("\n", " | ") if pg.locator("[role=dialog], dialog[open], .ag-dlg").count() else "?")
        cr.first.fill("15 2 * * *"); pg.wait_for_timeout(800)
    pg.locator("button", has_text="Activate").last.click()
    pg.wait_for_timeout(1500)
    shot(pg, tag + "-after-activate")
    pg.locator("button", has_text="My automations").first.click()
    pg.wait_for_timeout(1000)
    shot(pg, tag + "-my-automations", full=True)
    txt = pg.inner_text("#ag-cockpit, .ag-cockpit")
    print("my automations text:", txt[:1500].replace("\n", " | "))
    print("buttons:", pg.evaluate("() => [...document.querySelectorAll('.ag-cockpit button')].filter(b=>b.offsetParent).map(b=>b.innerText.trim()).filter(Boolean)"))
    dump_errors()
