"""UI guardrails for the anomaly-ribbon + cohesion pass (next_ai).

Pure static checks — no browser needed:
  • the ribbon markup + render functions are wired into the dashboard
  • every new visible i18n key exists in BOTH locales (en + zh-CN parity)
  • the locale files stay valid JSON and structurally mirror each other
"""
import json
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASH = os.path.join(ROOT, "static", "dashboard.html")
EN = os.path.join(ROOT, "locales", "en.json")
ZH = os.path.join(ROOT, "locales", "zh-CN.json")

NEW_KEYS = [
    "anom.chip_quiet", "anom.chip_fired",
    "ribbon.title", "ribbon.quiet", "ribbon.elevated", "ribbon.fired",
    "ribbon.collecting", "ribbon.vs", "ribbon.click_explain",
    "ribbon.aria", "ribbon.cap", "ribbon.cap_host",
    "anom.series.cpu", "anom.series.ram", "anom.series.load",
]


class TestRibbonMarkup(unittest.TestCase):
    def setUp(self):
        with open(DASH, encoding="utf-8") as f:
            self.html = f.read()

    def test_ribbon_containers_present(self):
        for el in ('id="gpu-ribbon"', 'id="host-ribbon"',
                   'class="ribbon-wrap"', 'data-i18n="ribbon.title"'):
            self.assertIn(el, self.html, f"missing ribbon markup: {el}")

    def test_ribbon_render_functions_present(self):
        for fn in ("function renderRibbon(", "function drawRibbonCanvas(",
                   "function _ribbonScore(", "function explainRibbonCell("):
            self.assertIn(fn, self.html, f"missing ribbon JS: {fn}")

    def test_ribbon_reuses_explain_endpoint(self):
        # the click flow must hit the existing copilot explain endpoint
        self.assertIn("/api/copilot/explain", self.html)


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

    def test_full_key_parity(self):
        en_keys = {k for k in self.en if not k.startswith("_")}
        zh_keys = {k for k in self.zh if not k.startswith("_")}
        self.assertEqual(en_keys - zh_keys, set(),
                         "keys in en.json but not zh-CN.json")
        self.assertEqual(zh_keys - en_keys, set(),
                         "keys in zh-CN.json but not en.json")


if __name__ == "__main__":
    unittest.main()
