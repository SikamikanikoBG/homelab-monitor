"""Playwright helpers for the alpha. Screenshots go to ../shots."""
import os, sys, json
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from playwright.sync_api import sync_playwright
BASE = os.environ.get("ALPHA_URL", "http://100.97.120.53:9831")
PW = os.environ.get("ALPHA_PASSWORD", "alpha-agents-2026")
CHROME = r"C:/Users/arsen/AppData/Local/ms-playwright/chromium-1243/chrome-win64/chrome.exe"
SHOTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "shots")
os.makedirs(SHOTS, exist_ok=True)
ERRORS = []

def browser(p):
    return p.chromium.launch(executable_path=CHROME, headless=True)

def page(b, w=1440, h=900, dark=False, locale="en-US", login=True, url="/", lang=None):
    ctx = b.new_context(viewport={"width": w, "height": h}, color_scheme="dark" if dark else "light", locale=locale,
                        is_mobile=w < 500, has_touch=w < 500)
    import urllib.request
    ver = json.load(urllib.request.urlopen(BASE + "/healthz", timeout=10))["version"]
    ctx.add_init_script("localStorage.setItem('hl_seen_version', %s); localStorage.setItem('hl_theme', %s);"
                        % (json.dumps(ver), json.dumps("dark" if dark else "light")))
    if lang:
        ctx.add_init_script("localStorage.setItem('hl_lang', %s);" % json.dumps(lang))
    pg = ctx.new_page()
    tag = f"{w}{'d' if dark else 'l'}"
    pg.on("console", lambda m: ERRORS.append((tag, "console." + m.type, m.text[:300])) if m.type in ("error", "warning") else None)
    pg.on("pageerror", lambda e: ERRORS.append((tag, "pageerror", str(e)[:300])))
    pg.on("response", lambda r: ERRORS.append((tag, "http %d" % r.status, r.url[:200])) if r.status >= 400 and "/api/auth/check" not in r.url else None)
    if login:
        pg.goto(BASE + "/login")
        pg.fill("#pw", PW)
        pg.click("#go")
        pg.wait_for_url(lambda u: "/login" not in u, timeout=15000)
    if url:
        pg.goto(BASE + url)
        pg.wait_for_load_state("networkidle")
    # close "what's new" modal if shown
    try:
        for sel in ["button:has-text('Got it')", "button:has-text('Close')", ".modal .close", "[aria-label=Close]"]:
            el = pg.locator(sel).first
            if el.is_visible(timeout=500):
                el.click(); break
    except Exception:
        pass
    return pg

def shot(pg, name, full=False):
    path = os.path.join(SHOTS, name + ".png")
    pg.screenshot(path=path, full_page=full)
    return path

def dump_errors():
    seen = set()
    for e in ERRORS:
        if e not in seen:
            seen.add(e); print("  !!", e)


def agent(pg, view="chat"):
    if not pg.locator('[data-agv="chat"]').first.is_visible():
        pg.goto(BASE + "/#agent")
        try:
            pg.wait_for_selector('[data-agv="chat"]', timeout=5000)
        except Exception:
            pg.locator('[data-t="agent"], [data-tab="agent"]').first.click()
            pg.wait_for_selector('[data-agv="chat"]', timeout=10000)
    pg.locator('[data-agv="%s"]' % view).first.click()
    pg.wait_for_timeout(900)
