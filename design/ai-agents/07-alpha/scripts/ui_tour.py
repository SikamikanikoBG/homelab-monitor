"""Tour every Agent view at desktop/phone, light/dark. Usage: ui_tour.py [prefix] [lang]"""
from ui import *
import sys
prefix = sys.argv[1] if len(sys.argv) > 1 else "t"
lang = sys.argv[2] if len(sys.argv) > 2 else None
with sync_playwright() as p:
    b = browser(p)
    for w, h in ((1440, 900), (390, 844)):
        for dark in (False, True):
            tag = f"{prefix}-{'desk' if w > 500 else 'phone'}-{'dark' if dark else 'light'}"
            pg = page(b, w, h, dark, url="/", lang=lang)
            for v in ("chat", "auto", "approvals", "activity", "policy", "models"):
                try:
                    agent(pg, v)
                    shot(pg, f"{tag}-{v}", full=(w < 500))
                except Exception as e:
                    print("FAIL", tag, v, str(e)[:200])
            pg.context.close()
    dump_errors()
