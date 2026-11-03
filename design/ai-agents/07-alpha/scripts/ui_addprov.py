from ui import *
with sync_playwright() as p:
    b = browser(p)
    pg = page(b, 1440, 900)
    agent(pg, "models")
    pg.evaluate("window.scrollTo(0, document.body.scrollHeight)"); pg.wait_for_timeout(500)
    shot(pg, "m-add-provider-form")
    print(pg.evaluate("() => [...document.querySelectorAll('#ag-cockpit input, #ag-cockpit select')].filter(e=>e.offsetParent).map(e=>[e.tagName, e.id||e.name, e.type, e.placeholder])"))
    reqs = []
    pg.on("request", lambda r: reqs.append((r.url, r.post_data)) if r.method == "POST" and "/providers" in r.url else None)
    # try adding with env: key
    sel = pg.locator("#agm-preset")  #("#ag-cockpit select").last
    print("preset options:", sel.evaluate("s=>[...s.options].map(o=>o.value)"))
    sel.select_option("openai"); pg.wait_for_timeout(500)
    shot(pg, "m-add-openai-form")
    print(pg.evaluate("() => [...document.querySelectorAll('#ag-cockpit input, #ag-cockpit select')].filter(e=>e.offsetParent).map(e=>[e.tagName, e.id||e.name, e.type, e.placeholder])"))
    print(pg.inner_text("#ag-cockpit")[-700:].replace("\n", " | "))
    pg.fill("#agm-key", "env:OPENAI_KEY_ALPHA")
    pg.locator("#ag-cockpit button", has_text="Add").last.click(); pg.wait_for_timeout(2000)
    print("UI sent:", reqs[:2])
import hlm
c = hlm.Client(); c.login()
for p_ in c.call("GET", "/api/agent/providers")[1]["providers"]:
    if p_["preset"] == "openai" and p_["label"] != "leak check":
        print("stored:", p_["key_ref"], p_["key_last4"], p_["label"]); c.call("DELETE", "/api/agent/providers/" + p_["id"])
