from ui import *
with sync_playwright() as p:
    b = browser(p)
    pg = page(b, 1440, 900)
    before = pg.evaluate("() => Object.assign({}, localStorage)")
    sel = pg.locator("select:has(option:has-text('简体中文'))").first
    sel.select_option(label="简体中文"); pg.wait_for_timeout(2000)
    after = pg.evaluate("() => Object.assign({}, localStorage)")
    print({k: after[k] for k in after if before.get(k) != after[k]})
