from ui import *
with sync_playwright() as p:
    b = browser(p)
    pg = page(b, 390, 844)
    agent(pg, "chat")
    for sel in ["#ag-new", ".ag-convs", "#ag-convs", "[data-conv]", ".ag-rail", "#ag-ta", ".ag-fab"]:
        loc = pg.locator(sel)
        n = loc.count()
        vis = [loc.nth(i).is_visible() for i in range(min(n, 3))]
        print(sel, n, vis, loc.first.bounding_box() if n else None)
    print(pg.evaluate("() => [...document.querySelectorAll('#ag-cockpit button, .ag-cockpit button')].filter(b=>b.offsetParent).map(b=>b.innerText.trim()||b.title||b.getAttribute('aria-label')).slice(0,40)"))
    dump_errors()
