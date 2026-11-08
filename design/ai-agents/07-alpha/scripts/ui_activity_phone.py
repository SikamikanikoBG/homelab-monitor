from ui import *
with sync_playwright() as p:
    b = browser(p)
    pg = page(b, 390, 844, True)
    agent(pg, "activity")
    pg.locator("tr[data-run]").nth(3).click()
    pg.wait_for_selector("#ag-rundetail .ag-step", timeout=10000); pg.wait_for_timeout(1200)
    shot(pg, "act-390-after-tap-viewport")
    print("scrollY", pg.evaluate("scrollY"), "detail top", pg.evaluate("document.querySelector('#ag-rundetail').getBoundingClientRect().top"))
    print("load more?", pg.evaluate("() => [...document.querySelectorAll('#ag-cockpit button')].filter(b=>b.offsetParent).map(b=>b.innerText)"))
