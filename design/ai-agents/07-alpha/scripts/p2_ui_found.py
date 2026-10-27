from ui import *
import hlm
api = hlm.Client(); api.login()
with sync_playwright() as p:
    b = browser(p)
    pg = page(b, 1440, 900)
    agent(pg, "models"); pg.wait_for_timeout(1500)
    shot(pg, "p2-found")
    t = pg.inner_text("#ag-cockpit")
    i = t.upper().find("FOUND ON YOUR LAB")
    print("found section:", t[i:i+600].replace("\n", " | "))
    btns = pg.evaluate("() => [...document.querySelectorAll('#ag-cockpit button')].filter(b=>b.offsetParent).map(b=>[b.innerText.trim(), b.dataset ? JSON.stringify(b.dataset) : ''])")
    print([x for x in btns if x[0] in ("Use", "Add", "Test", "Use as default")][:12])
    before = api.call("GET", "/api/agent/status")[1]["default"]
    # click the first Use in the found section
    sec = pg.locator("#ag-cockpit :text-matches('Found on your lab', 'i')").first.locator("xpath=ancestor::*[contains(@class,'ag-card') or self::section][1]")
    use = sec.locator("button", has_text="Use")
    print("Use buttons in found section:", use.count())
    if use.count():
        use.first.click(); pg.wait_for_timeout(6000)
        shot(pg, "p2-found-after-use", full=True)
        print("default before/after:", before, api.call("GET", "/api/agent/status")[1]["default"])
        print(pg.evaluate("() => [...document.querySelectorAll('.toast,[role=status],[role=alert]')].map(e=>e.innerText).join(' / ')"))
    dump_errors()
