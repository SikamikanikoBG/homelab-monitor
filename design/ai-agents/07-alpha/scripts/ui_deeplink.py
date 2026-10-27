from ui import *
import hlm
with sync_playwright() as p:
    b = browser(p)
    pg = page(b, 1440, 900, url="/#containers")
    pg.locator("[data-h='lab2']").first.click(); pg.wait_for_timeout(1500)
    pg.wait_for_selector(".ag-fab:not([hidden])"); pg.click(".ag-fab"); pg.wait_for_timeout(600)
    print("context chip:", pg.inner_text(".ag-panel")[:80].replace("\n", " | "))
    pg.fill("#agw-ta", hlm.stub([{"tool_calls": [{"name": "get_containers", "arguments": {"host": "lab1"}}]}, {"text": "See [the GPU page](#gpu) and [lab1 containers](#containers?host=lab1)."}]))
    pg.click("#agw-send")
    pg.wait_for_function("() => document.querySelector('#agw-msgs').innerText.includes('lab1 containers')", timeout=30000)
    pg.wait_for_timeout(800)
    chips = pg.locator("#agw-msgs .ag-chip")
    print("chips:", [chips.nth(i).inner_text() for i in range(chips.count())])
    chips.first.click(); pg.wait_for_timeout(500)
    shot(pg, "d-chip-open")
    print("chip body:", pg.inner_text("#agw-msgs")[-400:].replace("\n", " | "))
    # click GPU first, then lab1 containers
    pg.locator("#agw-msgs a", has_text="lab1 containers").click(); pg.wait_for_timeout(2000)
    print("after deeplink url:", pg.url, "| selected host:", pg.evaluate("() => [...document.querySelectorAll('[data-h].active, [data-h].on, [data-h][aria-pressed=true], [data-h].sel')].map(e=>e.dataset.h)"))
    shot(pg, "d-after-deeplink")
    # what does the context chip say when the server's selected host is lab2 & widget reopened?
    dump_errors()
