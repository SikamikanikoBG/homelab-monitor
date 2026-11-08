from ui import *
import sys
tag = sys.argv[1]
with sync_playwright() as p:
    b = browser(p)
    pg = page(b, 1440, 900, url="/#overview")
    pg.wait_for_timeout(2500)
    print("fab:", pg.evaluate("() => { const f=document.querySelector('.ag-fab'); return f ? [!!f.offsetParent, f.hidden] : 'absent'; }"),
          "agent nav:", pg.evaluate("() => [...document.querySelectorAll('[data-t=agent],[data-tab=agent]')].map(e=>!!e.offsetParent)"))
    shot(pg, tag + "-overview")
    pg.goto(BASE + "/#agent"); pg.wait_for_timeout(2500)
    shot(pg, tag + "-agent-tab")
    print("agent tab text:", pg.inner_text("body")[:0] or pg.evaluate("() => (document.querySelector('#ag-root')||document.body).innerText.slice(0,500)").replace("\n", " | "))
    dump_errors()
