"""UI guardrails for the 💡 cost-unlock nudge on the Costs tab (next_ai).

On a fresh/priceless install (no positive `kwh_price` → `cost_month.enabled==false`),
the cost card, savings KPI, cost forecast and monthly-budget gauge are ALL hidden by
design — leaving the Costs area a dead spot. This slice adds a SINGLE additive nudge
that turns that dead spot into a call-to-action, and vanishes the instant a price is set
(priced-state rendering byte-identical to before).

Pure static checks — no browser. The feature is FRONTEND-ONLY, driven off the
already-polled /api/forecast `cost_month.enabled` flag. These tests assert:
  • the nudge card markup + CTA button + its i18n keys are wired into the dashboard;
  • it is shown ONLY when cost is disabled (cost_month.enabled false) and HIDDEN when
    enabled — the critical priced-state invariant;
  • the CTA reuses the REAL existing Settings-open mechanism (showTab('alerts') +
    setSettingsPane('costs')) — no invented nav;
  • it adds NO new fetch/poll (reuses the FORECAST payload);
  • it does NOT alter the existing cost-card hide logic (costfc-card still toggles on
    cm.enabled exactly as before);
  • every new visible i18n key exists in BOTH locales (en + zh-CN) with full parity.

A backend behavioural check confirms the flag the nudge keys off actually flips:
cost_month.enabled is False with no price and True once a price is set.
"""
import json
import os
import re
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASH = os.path.join(ROOT, "static", "dashboard.html")
EN = os.path.join(ROOT, "locales", "en.json")
ZH = os.path.join(ROOT, "locales", "zh-CN.json")

NEW_KEYS = [
    "costunlock.title",
    "costunlock.body",
    "costunlock.cta",
]


class TestNudgeMarkup(unittest.TestCase):
    def setUp(self):
        with open(DASH, encoding="utf-8") as f:
            self.html = f.read()

    def test_nudge_card_present_and_hidden_by_default(self):
        # A new element (NOT the existing cost card) that starts hidden — it only
        # appears once the poll confirms cost is disabled.
        self.assertIn('id="cost-unlock-card"', self.html)
        self.assertRegex(self.html, r'id="cost-unlock-card"[^>]*\shidden')

    def test_cta_button_present(self):
        self.assertIn('id="cost-unlock-btn"', self.html)

    def test_nudge_uses_new_i18n_keys(self):
        for k in NEW_KEYS:
            self.assertIn(k, self.html, f"dashboard never uses {k}")


class TestShowHideLogic(unittest.TestCase):
    def setUp(self):
        with open(DASH, encoding="utf-8") as f:
            self.html = f.read()

    def _nudge_block(self):
        m = re.search(r"const uc=document\.getElementById\('cost-unlock-card'\);(.*?)\n  // Monthly power-cost budget gauge",
                      self.html, re.S)
        self.assertIsNotNone(m, "cost-unlock render block not found in renderForecast")
        return m.group(1)

    def test_shown_only_when_cost_disabled(self):
        body = self._nudge_block()
        # Hidden when the priced flag is on; shown when it is off.
        self.assertIn("if(cm.enabled){ uc.hidden=true; }", body)
        self.assertIn("uc.hidden=false;", body)

    def test_keys_off_cost_month_enabled(self):
        # The nudge decision is driven by the same cm=j.cost_month flag the cost card
        # already uses — no separate/derived signal.
        self.assertIn("const cm=j.cost_month||{};", self.html)


class TestSettingsWiring(unittest.TestCase):
    def setUp(self):
        with open(DASH, encoding="utf-8") as f:
            self.html = f.read()

    def test_cta_reuses_existing_settings_open_mechanism(self):
        m = re.search(r"ub\.onclick=\(\)=>\{(.*?)\};", self.html, re.S)
        self.assertIsNotNone(m, "cost-unlock CTA onclick not found")
        body = m.group(1)
        # The exact existing jump used by the costs-disabled hint (line ~4001).
        self.assertIn("showTab('alerts')", body)
        self.assertIn("setSettingsPane('costs')", body)

    def test_settings_open_targets_are_real(self):
        # The mechanism the CTA invokes must actually exist in the app.
        self.assertIn("function showTab(", self.html)
        self.assertIn("function setSettingsPane(", self.html)


class TestNoNewFetchAndInvariant(unittest.TestCase):
    def setUp(self):
        with open(DASH, encoding="utf-8") as f:
            self.html = f.read()

    def test_nudge_adds_no_fetch(self):
        m = re.search(r"const uc=document\.getElementById\('cost-unlock-card'\);(.*?)\n  // Monthly power-cost budget gauge",
                      self.html, re.S)
        self.assertNotIn("fetch(", m.group(1),
                         "cost-unlock nudge must reuse the polled FORECAST payload")

    def test_existing_cost_card_hide_logic_unchanged(self):
        # The priced-state invariant: the cost forecast card still toggles on cm.enabled
        # exactly as before — the nudge did NOT alter or un-hide it.
        self.assertIn("if(!cm.enabled){ cc.hidden=true; }", self.html)
        self.assertIn('id="costfc-card" hidden', self.html)


class TestBackendFlagFlips(unittest.TestCase):
    """The nudge keys off /api/forecast cost_month.enabled; confirm that flag is
    False with no price and True once a price is set (the real drive signal)."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, ROOT)
        import app  # noqa: E402
        cls.app = app

    def setUp(self):
        self._saved = {k: self.app.get_settings().get(k)
                       for k in ("kwh_price", "currency", "tariff_mode")}

    def tearDown(self):
        self.app.save_settings({k: (v if v is not None else "")
                                for k, v in self._saved.items()})

    def _seed_month(self, power_w=200):
        now = int(time.time())
        lt = time.localtime(now)
        month_start = int(time.mktime((lt.tm_year, lt.tm_mon, 1, 0, 0, 0, 0, 0, -1)))
        with self.app.LOCK:
            self.app.DB.execute("DELETE FROM samples WHERE ts>=?", (month_start,))
            ts = month_start + 3600
            while ts < now:
                self.app.DB.execute(
                    "INSERT OR REPLACE INTO samples(ts,util,mem_used,mem_total,power,temp) "
                    "VALUES(?,?,?,?,?,?)", (ts, 0, 0, 0, power_w, 0))
                ts += self.app.INTERVAL * 30
            self.app.DB.commit()
        return now

    def test_disabled_with_no_price(self):
        self.app.save_settings({"kwh_price": "0"})
        j = self.app.app.test_client().get("/api/forecast").get_json()
        self.assertIn("cost_month", j)
        self.assertFalse(j["cost_month"].get("enabled"),
                         "cost_month.enabled must be False with no price (nudge shows)")

    def test_enabled_once_price_set(self):
        self._seed_month()
        self.app.save_settings({"kwh_price": "0.30", "currency": "€",
                                "tariff_mode": "single"})
        j = self.app.app.test_client().get("/api/forecast").get_json()
        self.assertTrue(j["cost_month"].get("enabled"),
                        "cost_month.enabled must be True once a price is set (nudge hides)")


class TestI18nParity(unittest.TestCase):
    def setUp(self):
        with open(EN, encoding="utf-8") as f:
            self.en = json.load(f)
        with open(ZH, encoding="utf-8") as f:
            self.zh = json.load(f)

    def test_new_keys_in_both_locales(self):
        for k in NEW_KEYS:
            self.assertIn(k, self.en, f"en.json missing {k}")
            self.assertIn(k, self.zh, f"zh-CN.json missing {k}")

    def test_zh_translations_nonempty_and_distinct(self):
        # Real zh-CN, not an English echo.
        for k in NEW_KEYS:
            self.assertTrue(self.zh[k].strip(), f"{k} empty in zh-CN")
            self.assertNotEqual(self.zh[k], self.en[k], f"{k} not translated in zh-CN")

    def test_full_key_parity(self):
        en_keys = {k for k in self.en if not k.startswith("_")}
        zh_keys = {k for k in self.zh if not k.startswith("_")}
        self.assertEqual(en_keys - zh_keys, set(),
                         "keys in en.json but not zh-CN.json")
        self.assertEqual(zh_keys - en_keys, set(),
                         "keys in zh-CN.json but not en.json")


if __name__ == "__main__":
    unittest.main()
