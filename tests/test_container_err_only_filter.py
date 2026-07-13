"""UI guardrails for the 🚨 "Errors only" filter toggle on the Containers tab
(next_ai). This slice adds a compact toggle that collapses the container table to just
the containers that currently have errors — a PURE, non-destructive show/hide over the
SAME already-cached /api/logs/errors payload the badges/roll-up/sort already use.

Pure static checks — no browser. The feature is FRONTEND-ONLY, so these tests assert:
  • the toggle button + empty-note markup + the filter JS are wired into the dashboard;
  • the filter reuses the cached ERRCOUNTS and introduces NO new fetch (still exactly
    one /api/logs/errors call);
  • the state (ERR_ONLY) is module-level and re-applied on every poll re-render via
    reapplyErrSignal — so the 10s poll doesn't wipe the filter (persistence);
  • it's non-destructive: it toggles hidden on tr.ctrow nodes only, never the totals /
    placeholder row, and OFF restores every row;
  • an empty note appears when ON but zero rows have errors, and the toggle degrades
    (hides / inert) when there's no payload or no errors;
  • every new visible i18n key exists in BOTH locales (en + zh-CN) with parity.
"""
import json
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASH = os.path.join(ROOT, "static", "dashboard.html")
EN = os.path.join(ROOT, "locales", "en.json")
ZH = os.path.join(ROOT, "locales", "zh-CN.json")

NEW_KEYS = [
    "ct.err_only",
    "ct.err_only_hint",
    "ct.err_only_aria",
    "ct.err_only_empty",
]


class TestFilterMarkup(unittest.TestCase):
    def setUp(self):
        with open(DASH, encoding="utf-8") as f:
            self.html = f.read()

    def test_toggle_button_present(self):
        self.assertIn('id="cterr-filter"', self.html)
        # A two-state pill: exposes aria-pressed for a11y + starts hidden (inert until
        # the payload loads and there's ≥1 error).
        self.assertRegex(self.html, r'id="cterr-filter"[^>]*aria-pressed="false"')
        self.assertRegex(self.html, r'id="cterr-filter"[^>]*\shidden')

    def test_empty_note_present(self):
        self.assertIn('id="cterr-empty"', self.html)

    def test_filter_functions_present(self):
        for fn in ("function applyErrFilter(", "function toggleErrOnly("):
            self.assertIn(fn, self.html, f"missing filter JS: {fn}")

    def test_state_is_module_level(self):
        self.assertIn("let ERR_ONLY = false;", self.html)

    def test_toggle_uses_new_i18n_keys(self):
        for k in NEW_KEYS:
            self.assertIn(k, self.html, f"dashboard never uses {k}")


class TestPersistenceAndReapply(unittest.TestCase):
    def setUp(self):
        with open(DASH, encoding="utf-8") as f:
            self.html = f.read()

    def test_filter_reapplied_on_poll_render(self):
        # applyErrFilter must run inside reapplyErrSignal so the poll re-render (which
        # calls reapplyErrSignal) doesn't lose the filter → persistence across renders.
        m = re.search(r"function reapplyErrSignal\(\)\{(.*?)\}", self.html, re.S)
        self.assertIsNotNone(m, "reapplyErrSignal not found")
        self.assertIn("applyErrFilter()", m.group(1),
                      "applyErrFilter must be re-applied on every poll re-render")

    def test_reapply_still_wired_twice(self):
        # reapplyErrSignal still runs on both the fetch resolve and the poll render.
        self.assertGreaterEqual(self.html.count("reapplyErrSignal()"), 2)


class TestNonDestructive(unittest.TestCase):
    def setUp(self):
        with open(DASH, encoding="utf-8") as f:
            self.html = f.read()

    def test_hides_only_ctrows(self):
        # It selects tr.ctrow and toggles .hidden — never rebuilds rows and never
        # touches the totals/placeholder row.
        m = re.search(r"function applyErrFilter\(\)\{(.*?)\n\}", self.html, re.S)
        self.assertIsNotNone(m)
        body = m.group(1)
        self.assertIn("querySelectorAll('tr.ctrow')", body)
        self.assertIn("r.hidden", body)

    def test_off_restores_every_row(self):
        # When the filter is OFF (or no payload), every row is un-hidden.
        self.assertRegex(self.html,
                         r"if\(!ERR_ONLY \|\| !byName\)\{ r\.hidden = false; return; \}")

    def test_no_row_rebuild_in_filter(self):
        # The filter must not call the row template or innerHTML on the tbody.
        m = re.search(r"function applyErrFilter\(\)\{(.*?)\n\}", self.html, re.S)
        body = m.group(1)
        self.assertNotIn("innerHTML", body)


class TestNoNewFetch(unittest.TestCase):
    def setUp(self):
        with open(DASH, encoding="utf-8") as f:
            self.html = f.read()

    def test_single_logs_errors_fetch(self):
        n = len(re.findall(r"fetch\('/api/logs/errors'\)", self.html))
        self.assertEqual(n, 1, f"expected exactly one /api/logs/errors fetch, found {n}")

    def test_filter_reuses_cached_payload(self):
        m = re.search(r"function applyErrFilter\(\)\{(.*?)\n\}", self.html, re.S)
        body = m.group(1)
        self.assertIn("ERRCOUNTS", body)
        self.assertNotIn("fetch(", body)


class TestEmptyAndDegrade(unittest.TestCase):
    def setUp(self):
        with open(DASH, encoding="utf-8") as f:
            self.html = f.read()

    def test_empty_note_when_on_with_zero_errors(self):
        # empty note shown only when ON, payload present, and 0 rows have errors.
        self.assertIn("empty.hidden = !(ERR_ONLY && !!byName && shownWithErr===0)",
                      self.html)

    def test_toggle_hidden_when_no_payload_or_no_errors(self):
        # The toggle only shows once the payload loads AND ≥1 container has errors.
        self.assertIn("const show = !!byName && withErr>0;", self.html)
        self.assertIn("btn.hidden = !show;", self.html)


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
        # Real zh-CN, not an English echo (the emoji-only key is allowed to share glyphs).
        for k in ("ct.err_only_hint", "ct.err_only_aria", "ct.err_only_empty"):
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
