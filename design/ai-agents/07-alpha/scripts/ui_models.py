from ui import *
import hlm, json
api = hlm.Client(); api.login()
with sync_playwright() as p:
    b = browser(p)
    pg = page(b, 1440, 900)
    agent(pg, "models")
    shot(pg, "m-found")
    sec = pg.locator("text=Found on your lab").first.locator("xpath=ancestor::*[contains(@class,'card') or self::section][1]")
    print("found section:", pg.inner_text("#ag-cockpit")[200:900].replace("\n", " | "))
    pg.locator("#ag-cockpit button", has_text="Add").first.click(); pg.wait_for_timeout(2500)
    shot(pg, "m-after-add", full=True)
    # the new provider block
    blk = pg.locator("text=http://hlm-alpha-stub:18099").last.locator("xpath=ancestor::*[.//button[contains(.,'Delete')]][1]")
    print("new block:", blk.inner_text()[:400].replace("\n", " | "))
    row = blk.locator("tr, .ag-mrow, div", has_text="stub-notools").last
    tb = blk.locator("button", has_text="Test")
    print("test buttons:", tb.count())
    # click Test on the notools row
    btns = pg.locator("button[data-probe$='|stub-notools']")
    print("probe buttons:", [btns.nth(i).get_attribute("data-probe") for i in range(btns.count())])
    newid = [x for x in [btns.nth(i).get_attribute("data-probe") for i in range(btns.count())] if not x.startswith(("534c", "1be7"))]
    nt = pg.locator("button[data-probe='%s']" % newid[0]).locator("xpath=ancestor::tr[1]")
    pg.locator("button[data-probe='%s']" % newid[0]).click()
    pg.wait_for_timeout(6000)
    shot(pg, "m-notools-tested", full=True)
    print("after notools test:", nt.inner_text()[:300].replace("\n", " | "))
    dump_errors()
m = api.call("GET", "/api/agent/models")[1]["models"]
print([(x["provider_id"][:6], x["model"], x["tools"], x["multi_step"]) for x in m])
