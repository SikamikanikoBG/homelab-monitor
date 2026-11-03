"""Automations UI: Activate dialog, My automations, Run now, Dry run, duplicate, new from prompt."""
from ui import *
import hlm, sys
w = int(sys.argv[1]) if len(sys.argv) > 1 else 1440
tag = "a-desk" if w > 500 else "a-phone"
with sync_playwright() as p:
    b = browser(p)
    pg = page(b, w, 900 if w > 500 else 844)
    agent(pg, "auto")
    card = pg.locator("text=Docker cleanup").first.locator("xpath=ancestor::*[.//button[contains(., 'Activate')]][1]")
    card.locator("button", has_text="Activate").click()
    pg.wait_for_timeout(1000)
    shot(pg, tag + "-activate-dialog", full=True)
    dlg = pg.evaluate("() => { const d=[...document.querySelectorAll('dialog[open], .modal, .ag-dlg, [role=dialog]')].filter(e=>e.offsetParent||e.open); return d.map(e=>e.innerText.slice(0,1500)); }")
    print("dialog:", dlg)
    print("inputs:", pg.evaluate("() => [...document.querySelectorAll('input,select,textarea')].filter(e=>e.offsetParent && e.closest('[role=dialog],dialog,.ag-dlg,.modal')).map(e=>[e.tagName,e.type,e.name||e.id,e.value, e.checked])"))
    dump_errors()
