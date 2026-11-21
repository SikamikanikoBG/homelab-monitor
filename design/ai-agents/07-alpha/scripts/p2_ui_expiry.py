"""#17 in the UI: approve an expired card."""
from ui import *
import hlm, time
api = hlm.Client(); api.login()
api.call("PUT", "/api/agent/settings", {"chat_card_ttl_min": 1})
api.call("PUT", "/api/agent/policy/hosts/lab1/actions/container.restart", {"rule": "ask"})
with sync_playwright() as p:
    b = browser(p)
    pg = page(b, 1440, 900)
    agent(pg, "chat")
    pg.click("#ag-new"); pg.fill("#ag-ta", hlm.stub([{"tool_calls": [{"name": "container__restart", "arguments": {"host": "lab1", "name": "worker"}}]}, {"text": "x"}])); pg.click("#ag-send")
    pg.wait_for_selector("#ag-msgs .ag-confirm [data-ap]", timeout=30000)
    # make the page not refresh the card: keep it as is and wait
    pg.wait_for_timeout(70000)
    shot(pg, "p2-expired-before-click")
    print("card before click:", pg.inner_text("#ag-msgs .ag-confirm")[:300].replace("\n", " | "))
    btn = pg.locator("#ag-msgs .ag-confirm [data-ap]")
    if btn.count() and btn.first.is_visible():
        btn.first.click(); pg.wait_for_timeout(1500)
    shot(pg, "p2-expired-after-click")
    print("card after click:", pg.inner_text("#ag-msgs")[-400:].replace("\n", " | "))
    print("toast:", pg.evaluate("() => [...document.querySelectorAll('.toast, .ag-toast, [role=status], [role=alert]')].map(e=>e.innerText).join(' / ')"))
api.call("PUT", "/api/agent/settings", {"chat_card_ttl_min": 30})
