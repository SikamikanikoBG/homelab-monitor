"""XSS in answers, busy-send feedback, Stop button, cockpit approve with real verification."""
from ui import *
import hlm, json, time
api = hlm.Client(); api.login()
slow = next(p for p in api.call("GET", "/api/agent/providers")[1]["providers"] if "/slow/" in p["base_url"])
SKIPXSS = True
with sync_playwright() as p:
    b = browser(p)
    pg = page(b, 1440, 900, url="/#containers")
    pg.wait_for_selector(".ag-fab:not([hidden])", timeout=15000)
    pg.click(".ag-fab")
    xss = hlm.stub([{"text": "<img src=x onerror=\"window.__x=1\"> **bold** [click](javascript:window.__y=1) [ok](#containers?host=lab1) [ext](https://evil.example/x) <script>window.__z=1</script> `code` \" onmouseover=\"window.__w=1"}])
    pg.fill("#agw-ta", xss); pg.click("#agw-send")
    pg.wait_for_function("() => document.querySelector('#agw-msgs').innerText.includes('bold')", timeout=30000)
    pg.wait_for_timeout(1500)
    links = pg.evaluate("() => [...document.querySelectorAll('#agw-msgs a')].map(a=>[a.innerText, a.getAttribute('href'), a.target])")
    print("XSS flags", pg.evaluate("() => [window.__x, window.__y, window.__z, window.__w]"), "links", links, "imgs", pg.evaluate("() => document.querySelectorAll('#agw-msgs img').length"))
    for a in pg.locator("#agw-msgs a").all():
        try: a.click(timeout=1000)
        except Exception: pass
    pg.wait_for_timeout(800)
    print("after link clicks:", pg.evaluate("() => [window.__x, window.__y]"), pg.url)
    shot(pg, "f4-xss")
    # Busy send + Stop: pick the slow model
    pg.goto(BASE + "/#overview"); pg.wait_for_selector(".ag-fab:not([hidden])"); pg.click(".ag-fab")
    sel = pg.locator(".ag-panel select").first
    opts = sel.evaluate("s => [...s.options].map(o=>[o.value,o.text])")
    print("model options:", opts)
    v = next(o[0] for o in opts if "slow" in o[1])
    sel.select_option(v)
    pg.fill("#agw-ta", "slow question"); pg.click("#agw-send")
    pg.wait_for_timeout(2500)
    pg.fill("#agw-ta", "second while busy"); pg.click("#agw-send")
    pg.wait_for_timeout(1500)
    shot(pg, "f5-busy-send")
    print("panel text while busy:", pg.inner_text(".ag-panel")[-400:].replace("\n", " | "))
    stop = pg.locator(".ag-panel button", has_text="Stop")
    print("stop visible:", stop.count() and stop.first.is_visible())
    t0 = time.time()
    stop.first.click()
    pg.wait_for_timeout(3000)
    shot(pg, "f5-stopped")
    runs = api.call("GET", "/api/agent/runs")[1]
    items = runs.get("items") or runs.get("runs")
    print("latest runs:", [(r["title"][:20], r["status"], r.get("status_reason")) for r in items[:3]])
    print("panel after stop:", pg.inner_text(".ag-panel")[-300:].replace("\n", " | "))
    pg.context.close()
    dump_errors()
