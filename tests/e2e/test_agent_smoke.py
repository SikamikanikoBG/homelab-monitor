"""Playwright smoke of the agent UI against a running instance (the alpha
sandbox), skipped unless ALPHA_URL is set and playwright is installed. Not part
of CI; run it by hand:

    ALPHA_URL=http://ardi:9831 ALPHA_PASSWORD=... [CHROMIUM=/path/to/chrome] \
      python -m pytest tests/e2e -q

Uses the stub model inside the alpha network (http://hlm-alpha-stub:18099/v1)
so it is deterministic, and saves screenshots to $SHOTS (default: a temp dir).
Covers: login, the Agent tab and its badge, a confirm card approved in the UI
(the container really restarts on the sandbox box), a Passive refusal, the
Ask widget on the Overview at phone width, kiosk mode without the button, and
both themes.
"""
import json
import os
import tempfile
import time
import unittest

import pytest

playwright = pytest.importorskip("playwright.sync_api")
BASE = os.environ.get("ALPHA_URL")
PW = os.environ.get("ALPHA_PASSWORD", "alpha-agents-2026")
STUB = os.environ.get("ALPHA_STUB", "http://hlm-alpha-stub:18099/v1")
SHOTS = os.environ.get("SHOTS") or tempfile.mkdtemp(prefix="agent-shots-")


@unittest.skipUnless(BASE, "set ALPHA_URL to run the browser smoke")
class TestAgentSmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = playwright.sync_playwright().start()
        exe = os.environ.get("CHROMIUM")
        cls.browser = cls.pw.chromium.launch(executable_path=exe) if exe else cls.pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.ctx = self.browser.new_context(viewport={"width": 1400, "height": 900})
        # Don't let the first-visit "What's new" modal sit over the page.
        import urllib.request
        ver = json.load(urllib.request.urlopen(BASE + "/healthz", timeout=10))["version"]
        self.ctx.add_init_script("localStorage.setItem('hl_seen_version', %s)" % json.dumps(ver))
        self.page = self.ctx.new_page()
        self.errors = []
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.goto(BASE + "/")
        self.page.wait_for_url("**/login**")
        self.page.fill("#pw", PW)
        self.page.click("#go")
        self.page.wait_for_selector(".ag-fab", state="attached", timeout=20000)

    def tearDown(self):
        self.ctx.close()
        self.assertEqual(self.errors, [])

    def api(self, method, path, body=None):
        return self.page.evaluate(
            """async ([m, p, b]) => { const r = await fetch(p, {method: m, headers: {'Content-Type': 'application/json'},
               body: b ? JSON.stringify(b) : undefined}); return {s: r.status, j: await r.json().catch(() => null)}; }""",
            [method, path, body])

    def shot(self, name):
        self.page.screenshot(path=os.path.join(SHOTS, name + ".png"))

    def use_stub(self):
        self.api("POST", "/api/agent/enable", {"enabled": True})
        provs = self.api("GET", "/api/agent/providers")["j"]["providers"]
        prov = next((p for p in provs if p["base_url"] == STUB), None)
        if not prov:
            prov = self.api("POST", "/api/agent/providers", {"preset": "openai_compatible", "base_url": STUB,
                                                             "label": "stub"})["j"]["provider"]
            self.api("POST", "/api/agent/providers/%s/test" % prov["id"])
        self.api("POST", "/api/agent/models/probe", {"provider_id": prov["id"], "model": "stub-agent"})
        self.api("PUT", "/api/agent/settings", {"default_provider_id": prov["id"], "default_model": "stub-agent"})
        self.api("PUT", "/api/agent/policy/hosts/lab1", {"mode": "active"})
        self.api("PUT", "/api/agent/policy/hosts/lab1/actions/container.restart", {"rule": "ask"})
        self.api("PUT", "/api/agent/policy/hosts/lab2", {"mode": "passive"})

    def chat(self, text):
        self.page.click('[data-agv="chat"]')
        self.page.click("#ag-new")
        self.page.fill("#ag-ta", text)
        self.page.click("#ag-send")

    def test_cockpit_confirm_card_and_refusal(self):
        self.use_stub()
        self.page.goto(BASE + "/#agent")
        self.page.wait_for_selector('[data-agv="chat"]')
        before = self.api("GET", "/api/agent/status")["j"]["pending_approvals"]
        self.chat('#stub [{"tool_calls":[{"name":"container__restart","arguments":{"host":"lab1","name":"web",'
                  '"reason":"browser smoke"}}]},{"text":"Restarted web on lab1."}]')
        self.page.wait_for_selector("#ag-msgs .ag-confirm [data-ap]", timeout=30000)
        self.assertIn("lab1", self.page.inner_text("#ag-msgs .ag-confirm"))
        self.shot("10-confirm-card")
        # a locator, not a handle: live updates repaint the chat, so re-resolve on click
        self.page.locator("#ag-msgs .ag-confirm [data-ap]").click()
        self.page.wait_for_selector("#ag-msgs .ag-confirm.done", timeout=30000)
        self.page.wait_for_function("() => document.querySelector('#ag-msgs').innerText.includes('Restarted web on lab1.')",
                                    timeout=30000)
        self.shot("11-approved")
        self.assertLessEqual(self.api("GET", "/api/agent/status")["j"]["pending_approvals"], before + 1 - 1)
        self.chat('#stub [{"tool_calls":[{"name":"container__restart","arguments":{"host":"lab2","name":"web"}}]},'
                  '{"text":"Not allowed on lab2."}]')
        self.page.wait_for_function("() => document.querySelector('#ag-msgs').innerText.includes('Not allowed on lab2.')",
                                    timeout=30000)
        self.page.locator("#ag-msgs .ag-chip").first.click()
        self.assertIn("Passive", self.page.inner_text("#ag-msgs .ag-chipbody"))
        self.shot("12-passive-refusal")
        for v in ("auto", "approvals", "activity", "policy", "models"):
            self.page.click('[data-agv="%s"]' % v)
            self.page.wait_for_timeout(800)
            self.shot("13-view-" + v)
        self.page.click('[data-agv="activity"]')
        self.page.wait_for_selector("tr[data-run]")
        self.page.click("tr[data-run]")
        self.page.wait_for_selector("#ag-rundetail .ag-step")
        self.shot("14-run-detail")

    def test_widget_phone_kiosk_and_themes(self):
        self.use_stub()
        self.page.set_viewport_size({"width": 390, "height": 800})
        self.page.goto(BASE + "/#overview")
        self.page.reload()                       # pick up "agent enabled" set through the API above
        self.page.wait_for_selector(".ag-fab:not([hidden])")
        self.page.click(".ag-fab")
        self.page.fill("#agw-ta", "How is my lab?")
        self.page.click("#agw-send")
        self.page.wait_for_function("() => document.querySelector('#agw-msgs').innerText.includes('Stub answer')",
                                    timeout=30000)
        box = self.page.eval_on_selector(".ag-panel", "e => e.getBoundingClientRect().width")
        self.assertEqual(int(box), 390)                       # full screen on a phone
        self.shot("20-phone-widget")
        for theme in ("dark", "light"):
            self.page.evaluate("t => { localStorage.setItem('hl_theme', t); }", theme)
            self.page.reload()
            self.page.wait_for_selector(".ag-fab", state="attached")   # hidden on the Agent tab, by design
            self.page.goto(BASE + "/#agent")
            self.page.wait_for_selector('[data-agv="chat"]')
            self.shot("21-agent-" + theme)
        self.page.goto(BASE + "/?display=1")
        self.page.wait_for_timeout(2500)
        disp = self.page.evaluate("() => { const f = document.querySelector('.ag-fab'); return f ? getComputedStyle(f).display : 'none'; }")
        self.assertEqual(disp, "none")


if __name__ == "__main__":
    unittest.main()
