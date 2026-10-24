from ui import *
with sync_playwright() as p:
    b = browser(p)
    for view in ("policy", "activity", "approvals", "auto", "models"):
        pg = page(b, 390, 844, dark=True)
        agent(pg, view)
        r = pg.evaluate("""() => {
          const out = {doc: [document.documentElement.scrollWidth, document.documentElement.clientWidth]};
          const sc = [...document.querySelectorAll('#ag-cockpit *, .ag-cockpit *')].filter(e => e.scrollWidth > e.clientWidth + 2 && e.offsetParent)
              .map(e => [e.tagName + '.' + (e.className||'').toString().slice(0,30), e.scrollWidth, e.clientWidth, getComputedStyle(e).overflowX]);
          out.over = sc.slice(0, 8); return out; }""")
        print(view, r)
        pg.context.close()
