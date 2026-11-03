from ui import *
import hlm, json
info = json.load(open("out_collapse.json"))
with sync_playwright() as p:
    b = browser(p)
    for w, h, dark in ((1440, 900, False), (390, 844, True)):
        pg = page(b, w, h, dark)
        agent(pg, "activity")
        print("filters:", pg.evaluate("() => [...document.querySelectorAll('#ag-cockpit select')].filter(s=>s.offsetParent).map(s=>[...s.options].map(o=>o.text).slice(0,8))"))
        pg.locator("tr[data-run='%s']" % info["runs"][1]).first.click() if pg.locator("tr[data-run='%s']" % info["runs"][1]).count() else pg.locator("tr[data-run]").first.click()
        pg.wait_for_selector("#ag-rundetail .ag-step", timeout=10000)
        pg.wait_for_timeout(500)
        shot(pg, f"act-{w}-detail", full=True)
        print(w, "detail:", pg.inner_text("#ag-rundetail")[:1200].replace("\n", " | "))
        pg.context.close()
    dump_errors()
