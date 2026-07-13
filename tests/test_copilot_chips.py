"""Tests for the 💬 suggested-question chips on the Lab Copilot ask-box.

The chips are a static, curated demo-enablement affordance: a small row of one-click
example questions inside `#copilot-ask` that each fill `#copilot-q` and fire the EXISTING
`copilotAsk()` flow verbatim. They are purely presentational strings until clicked — the
LLM fires ONLY on an explicit click, exactly like typing a question.

Coverage (frontend-only slice — parses static/dashboard.html + the two locale files):
  • the chip row exists inside `#copilot-ask` (so it inherits the ask-box hidden/LLM gate);
  • every chip is a real <button> carrying a data-q-key i18n key + data-i18n label;
  • the click wiring fills `#copilot-q` from the localized key and calls copilotAsk()
    (static handler-string check — reuses the existing ask fn, does not fork it);
  • TRIPWIRE: the chip wiring adds NO fetch / LLM call on render — it only fires on click;
  • i18n parity: every new copilot.chip_* / try_label key exists in BOTH locales with no
    placeholder drift.
"""
import json
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASH = os.path.join(ROOT, "static", "dashboard.html")
EN = os.path.join(ROOT, "locales", "en.json")
ZH = os.path.join(ROOT, "locales", "zh-CN.json")

CHIP_KEYS = [
    "copilot.chip_overheat",
    "copilot.chip_costliest",
    "copilot.chip_diskfill",
    "copilot.chip_powerspike",
    "copilot.chip_savings",
]
NEW_KEYS = ["copilot.try_label"] + CHIP_KEYS


def _read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


class TestChipMarkup(unittest.TestCase):
    def setUp(self):
        self.html = _read(DASH)

    def test_chip_row_present(self):
        self.assertIn('id="copilot-chips"', self.html)

    def test_chip_row_lives_inside_ask_box(self):
        # #copilot-chips must appear AFTER #copilot-ask opens and BEFORE #copilot-answer,
        # so it sits inside the ask-box container and inherits its hidden/LLM gate.
        ask = self.html.index('id="copilot-ask"')
        chips = self.html.index('id="copilot-chips"')
        answer = self.html.index('id="copilot-answer"')
        self.assertLess(ask, chips)
        self.assertLess(chips, answer)

    def test_each_chip_is_a_real_button_with_i18n_key(self):
        # Grab the chip row block and assert one real <button> per curated key.
        block = self.html[self.html.index('id="copilot-chips"'):self.html.index('id="copilot-answer"')]
        for key in CHIP_KEYS:
            self.assertIn('data-q-key="%s"' % key, block, key)
            self.assertIn('data-i18n="%s"' % key, block, key)
        # real <button> elements (free a11y — focusable, Enter/Space activate)
        self.assertEqual(block.count('class="osvar copilot-chip"'), len(CHIP_KEYS))
        self.assertEqual(block.count('type="button"'), len(CHIP_KEYS))

    def test_no_dynamic_innerhtml_interpolation_in_chip_row(self):
        # Chip labels are static data-i18n strings, never innerHTML-interpolated.
        block = self.html[self.html.index('id="copilot-chips"'):self.html.index('id="copilot-answer"')]
        self.assertNotIn("innerHTML", block)


class TestChipWiring(unittest.TestCase):
    def setUp(self):
        self.html = _read(DASH)

    def test_click_fills_input_and_calls_ask(self):
        # The chip handler must reuse the EXISTING ask flow: set #copilot-q then copilotAsk().
        m = re.search(r"#copilot-chips \.copilot-chip.*?copilotAsk\(\);", self.html, re.S)
        self.assertIsNotNone(m, "chip click wiring not found")
        handler = m.group(0)
        self.assertIn("copilot-q", handler)
        self.assertIn("I18N.t(chip.getAttribute('data-q-key')", handler)
        self.assertIn(".value=", handler)
        self.assertIn("copilotAsk();", handler)

    def test_tripwire_no_fetch_on_render(self):
        # The chip wiring block must not fetch / call the LLM on wire-up — only on click.
        m = re.search(r"// Suggested-question chips:.*?\}\);\s*\n", self.html, re.S)
        self.assertIsNotNone(m)
        block = m.group(0)
        self.assertNotIn("fetch(", block)
        self.assertNotIn("EventSource", block)
        self.assertNotIn("/api/copilot", block)


class TestChipAutoHide(unittest.TestCase):
    """The chip row fades out once an answer is shown / a question is typed, and
    returns when the input is empty and no answer is present. Presentational-only
    polish that must not touch the ask flow or the no-LLM-on-render invariant."""

    def setUp(self):
        self.html = _read(DASH)

    def test_sync_helper_present(self):
        self.assertIn("function _copilotSyncChips(", self.html)

    def test_helper_toggles_on_answer_and_typed_state(self):
        src = self.html[self.html.index("function _copilotSyncChips("):]
        src = src[:src.index("\n}")]
        self.assertIn("copilot-chips", src)
        self.assertIn("copilot-answer", src)
        self.assertIn("copilot-q", src)
        # hidden when there is an answer OR typed text
        self.assertIn("chips.hidden = hasAnswer || typed;", src)

    def test_helper_has_no_llm_side_effects(self):
        # Pure DOM toggle — never fetches or calls the ask flow.
        src = self.html[self.html.index("function _copilotSyncChips("):]
        src = src[:src.index("\n}")]
        for bad in ("fetch(", "copilotAsk(", "EventSource", "/api/copilot"):
            self.assertNotIn(bad, src, bad)

    def test_sync_called_from_ask_and_on_input(self):
        ask = self.html[self.html.index("async function copilotAsk("):]
        ask = ask[:ask.index("\n}")]
        self.assertIn("_copilotSyncChips();", ask)
        # input listener re-shows chips when the box is cleared
        self.assertIn("q.addEventListener('input', _copilotSyncChips)", self.html)


class TestI18nParity(unittest.TestCase):
    def setUp(self):
        self.en = json.loads(_read(EN))
        self.zh = json.loads(_read(ZH))

    def test_new_keys_present_both_locales(self):
        for key in NEW_KEYS:
            self.assertIn(key, self.en, "missing in en: %s" % key)
            self.assertIn(key, self.zh, "missing in zh-CN: %s" % key)

    def test_no_placeholder_drift(self):
        # None of these curated strings carry {placeholders}; assert parity anyway.
        for key in NEW_KEYS:
            en_ph = set(re.findall(r"\{[^}]+\}", self.en[key]))
            zh_ph = set(re.findall(r"\{[^}]+\}", self.zh[key]))
            self.assertEqual(en_ph, zh_ph, key)

    def test_zh_is_not_english_echo(self):
        # Real translation, not a machine echo of the English string.
        for key in CHIP_KEYS:
            self.assertNotEqual(self.en[key], self.zh[key], key)


if __name__ == "__main__":
    unittest.main()
