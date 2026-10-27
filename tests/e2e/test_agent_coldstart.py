"""The owner's first visit, timed (UAT-1): a fresh hub with no password, the
Overview invitation, the setup wizard, and the first real answer.

Needs a cold alpha (tests/alpha/up.sh with ALPHA_COLD=1, and ALPHA_CUSTOM_AI
pointing at a model server so the setup finds one) and Playwright. Not part of
CI; run it by hand:

    ALPHA_URL=http://ardi:9831 ALPHA_COLD=1 [CHROMIUM=/path/to/chrome] [SHOTS=dir] \
      python -m pytest tests/e2e/test_agent_coldstart.py -q -s

It consumes the cold state (it sets the password), so it runs once per up.sh.
Screenshots of every step go to $SHOTS; the timings are printed.
"""
import json
import os
import tempfile
import time
import unittest
import urllib.request

import pytest

playwright = pytest.importorskip("playwright.sync_api")
BASE = os.environ.get("ALPHA_URL")
COLD = os.environ.get("ALPHA_COLD")
PW = os.environ.get("ALPHA_PASSWORD", "alpha-agents-2026")
SHOTS = os.environ.get("SHOTS") or tempfile.mkdtemp(prefix="agent-cold-")
ANSWER_S = float(os.environ.get("ANSWER_S", "120"))


@unittest.skipUnless(BASE and COLD, "set ALPHA_URL and ALPHA_COLD=1 (a fresh alpha) to run the walk-through")
class TestColdStart(unittest.TestCase):
    def test_overview_to_first_answer(self):
        ver = json.load(urllib.request.urlopen(BASE + "/healthz", timeout=10))["version"]
        os.makedirs(SHOTS, exist_ok=True)
        marks = []
        with playwright.sync_playwright() as p:
            exe = os.environ.get("CHROMIUM")
            b = p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()
            # the invitation and the first wizard step in every theme/width, before anything is set
            for theme in ("dark", "light"):
                for name, vp in (("desktop", {"width": 1440, "height": 950}), ("phone", {"width": 390, "height": 844})):
                    ctx = b.new_context(viewport=vp)
                    ctx.add_init_script("localStorage.setItem('hl_seen_version', %s); localStorage.setItem('hl_theme', %s)"
                                        % (json.dumps(ver), json.dumps(theme)))
                    pg = ctx.new_page()
                    pg.goto(BASE + "/")
                    pg.wait_for_selector("#ag-home[data-state=invite] .ag-invite", timeout=30000)
                    pg.evaluate("window.scrollTo(0, document.getElementById('ag-home').getBoundingClientRect().top + scrollY - 120)")
                    time.sleep(0.8)
                    pg.screenshot(path=os.path.join(SHOTS, "invite-%s-%s.png" % (name, theme)))
                    pg.click("#ag-home [data-agsetup]")
                    pg.wait_for_selector("#agz-pw")
                    pg.fill("#agz-pw", "short")
                    time.sleep(0.3)
                    pg.screenshot(path=os.path.join(SHOTS, "setup1-%s-%s.png" % (name, theme)))
                    pg.keyboard.press("Escape")
                    if name == "phone" and theme == "light":
                        pg.click("#agh-later")                   # Not now: folds to a strip (this browser)
                        pg.wait_for_selector("#ag-home[data-state=strip]")
                        time.sleep(0.4)
                        pg.screenshot(path=os.path.join(SHOTS, "strip-%s-%s.png" % (name, theme)))
                    ctx.close()

            # the walk-through itself, timed: desktop, dark
            ctx = b.new_context(viewport={"width": 1440, "height": 950})
            ctx.add_init_script("localStorage.setItem('hl_seen_version', %s); localStorage.setItem('hl_theme', 'dark')" % json.dumps(ver))
            pg = ctx.new_page()
            errors = []
            pg.on("pageerror", lambda e: errors.append(str(e)))
            t0 = time.time()
            pg.goto(BASE + "/")
            pg.wait_for_selector("#ag-home[data-state=invite] .ag-invite", timeout=30000)
            found = pg.inner_text("#ag-home .ag-steps3")
            marks.append(("overview shows the invitation", time.time() - t0))
            pg.click("#ag-home [data-agsetup]")
            pg.fill("#agz-pw", PW)
            pg.fill("#agz-pw2", PW)
            pg.click("[data-wz=pw]")
            pg.wait_for_selector(".ag-radios, .ag-wiz .ag-empty", timeout=60000)
            marks.append(("password set, models listed", time.time() - t0))
            pg.wait_for_selector(".ag-radio.on .ag-state.ok", timeout=180000)
            pg.screenshot(path=os.path.join(SHOTS, "setup2-desktop-dark.png"))
            marks.append(("a tested model pre-selected", time.time() - t0))
            pg.click("[data-wz=model]")
            pg.wait_for_selector("input[name=agz-p]")
            time.sleep(1)
            pg.screenshot(path=os.path.join(SHOTS, "setup3-desktop-dark.png"))
            pg.click("[data-wz=finish]")
            pg.wait_for_selector(".ag-hello [data-sug]", timeout=30000)
            marks.append(("agent on, chat with suggestions", time.time() - t0))
            pg.screenshot(path=os.path.join(SHOTS, "chat-first-desktop-dark.png"))
            pg.click(".ag-hello [data-sug]")
            pg.wait_for_selector("article.ag-msg .ag-foot", timeout=ANSWER_S * 1000)
            marks.append(("first real answer on screen", time.time() - t0))
            answer = pg.inner_text("article.ag-msg")
            time.sleep(6)                               # the title and follow-ups arrive after the answer
            pg.screenshot(path=os.path.join(SHOTS, "chat-answer-desktop-dark.png"))
            pg.evaluate("location.hash = '#overview'")
            pg.wait_for_selector("#ag-home[data-state=ask]", timeout=20000)
            pg.evaluate("window.scrollTo(0, document.getElementById('ag-home').getBoundingClientRect().top + scrollY - 120)")
            time.sleep(1)
            pg.screenshot(path=os.path.join(SHOTS, "overview-ask-desktop-dark.png"))
            ctx.close()
            b.close()
        print("\nfound line:", found.replace("\n", " | "))
        for what, t in marks:
            print("%6.1f s  %s" % (t, what))
        print("answer:\n" + answer)
        print("screenshots:", SHOTS)
        self.assertEqual(errors, [])
        self.assertLess(marks[-1][1], 120, "the walk-through took over two minutes")
