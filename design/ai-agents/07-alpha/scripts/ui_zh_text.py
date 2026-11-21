"""Find English left over in the zh-CN agent UI."""
from ui import *
import re
with sync_playwright() as p:
    b = browser(p)
    pg = page(b, 1440, 900, lang="zh-CN")
    for v in ("chat", "auto", "approvals", "activity", "policy", "models"):
        agent(pg, v)
        txt = pg.inner_text("#ag-cockpit")
        eng = sorted(set(m for m in re.findall(r"[A-Za-z][A-Za-z ',.]{6,}", txt) if not re.search(r"(docker|systemctl|journalctl|ollama|stub|lab\d|local|http|Europe|qwen|vLLM|sudo|prune|restart|session|\{)", m)))
        print(v, "->", eng[:25])
    pg.goto(BASE + "/#overview"); pg.wait_for_selector(".ag-fab:not([hidden])"); pg.click(".ag-fab"); pg.wait_for_timeout(800)
    shot(pg, "zh-widget")
    print("widget:", pg.inner_text(".ag-panel")[:300].replace("\n", " | "))
