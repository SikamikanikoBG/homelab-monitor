from ui import *
with sync_playwright() as p:
    b = browser(p)
    pg = page(b, 1440, 900)
    agent(pg, "activity"); pg.wait_for_timeout(4000)
    d = pg.locator("#ag-cockpit input[type=date]")
    print("date input attrs:", d.evaluate("e=>[e.title, e.getAttribute('aria-label'), e.placeholder, e.previousElementSibling && e.previousElementSibling.innerText]"))
    for v in ("2026-10-25", "2026-10-24", "2026-10-23"):
        d.fill(v); d.dispatch_event("change"); pg.wait_for_timeout(2000)
        print(v, pg.locator("tr[data-run]").count(), pg.evaluate("() => (document.querySelector('#ag-cockpit .ag-empty')||{}).innerText"))
