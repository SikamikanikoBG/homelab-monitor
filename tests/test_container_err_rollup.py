"""UI guardrails for the 🚨 Container error roll-up KPI + error-first row sort
(next_ai). This slice completes the per-container recent-error badge feature by
making the signal glanceable (a tab-level roll-up chip) and actionable (float the
error-bearing rows to the top).

Pure static checks — no browser needed. The feature is FRONTEND-ONLY: it reuses the
SAME cached /api/logs/errors payload the badges already hold, so these tests assert:
  • the roll-up markup + the sort/roll-up JS are wired into the dashboard;
  • no NEW fetch is introduced on this path (still a single /api/logs/errors call
    from loadContainerErrors, computed client-side thereafter);
  • the sort is a stable, error-first, non-destructive re-order (decorate-sort with
    (b.e - a.e) || (a.i - b.i)) that degrades when no payload is present;
  • the truncated 40-cap is surfaced honestly (a dedicated i18n note);
  • every new visible i18n key exists in BOTH locales (en + zh-CN parity), and the
    locales stay full-key-parity JSON.
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
    "ct.err_rollup_some",
    "ct.err_rollup_none",
    "ct.err_rollup_truncated",
]


class TestRollupMarkup(unittest.TestCase):
    def setUp(self):
        with open(DASH, encoding="utf-8") as f:
            self.html = f.read()

    def test_rollup_container_present(self):
        # The tab-level roll-up chip host element sits above the container table.
        self.assertIn('id="cterr-rollup"', self.html)
        self.assertIn("cterr-rollup", self.html)

    def test_rollup_render_functions_present(self):
        for fn in ("function renderErrRollup(", "function errRollupStats(",
                   "function sortErrRowsStable(", "function applyErrSort(",
                   "function reapplyErrSignal("):
            self.assertIn(fn, self.html, f"missing roll-up/sort JS: {fn}")

    def test_rollup_uses_new_i18n_keys(self):
        for k in NEW_KEYS:
            self.assertIn(k, self.html, f"dashboard never uses {k}")

    def test_reapply_wired_into_poll_render(self):
        # The sort + roll-up must be re-applied after the poll re-render so the 10s
        # poll doesn't wipe the order — and it must be called from loadContainerErrors.
        self.assertGreaterEqual(self.html.count("reapplyErrSignal()"), 2,
                                "reapplyErrSignal must run on both fetch and poll render")


class TestNoNewFetch(unittest.TestCase):
    """The slice is compute-only: it must NOT add a second fetch to /api/logs/errors
    (or any new endpoint on this path). Still exactly one fetch call in the loader."""
    def setUp(self):
        with open(DASH, encoding="utf-8") as f:
            self.html = f.read()

    def test_single_logs_errors_fetch(self):
        n = len(re.findall(r"fetch\('/api/logs/errors'\)", self.html))
        self.assertEqual(n, 1, f"expected exactly one /api/logs/errors fetch, found {n}")

    def test_rollup_reuses_cached_payload(self):
        # errRollupStats derives from the cached ERRCOUNTS, not a fresh request.
        self.assertIn("ERRCOUNTS.byName", self.html)
        # and the sort reads the same byName map.
        self.assertRegex(self.html, r"sortErrRowsStable\(rows,\s*ERRCOUNTS\.byName\)")


class TestSortContract(unittest.TestCase):
    """The sort helper's ordering contract is load-bearing, so assert its exact shape:
    decorate with (row, index, errorCount) then sort error-desc, index-asc (stable)."""
    def setUp(self):
        with open(DASH, encoding="utf-8") as f:
            self.html = f.read()

    def test_error_first_stable_comparator(self):
        # highest error count first, ties broken by original index → stable.
        self.assertIn("(b.e - a.e) || (a.i - b.i)", self.html)

    def test_sort_is_nondestructive(self):
        # It re-orders existing <tr> nodes via insertBefore — never rebuilds rows,
        # so the click→drawer bridge, control buttons and totals row survive.
        self.assertIn("tb.insertBefore(r, anchor)", self.html)
        self.assertIn("tr:not(.ctrow)", self.html)   # totals/placeholder stays at bottom

    def test_degrades_without_payload(self):
        # No payload → sort returns rows as-is and the roll-up renders nothing.
        self.assertIn("if(!byName) return rows.slice();", self.html)
        self.assertIn("if(!s){ el.innerHTML=''; return; }", self.html)

    def test_truncated_surfaced_honestly(self):
        # When the 40-cap fired we must say so, not imply a full count.
        self.assertIn("ct.err_rollup_truncated", self.html)
        self.assertIn("if(s.truncated){", self.html)


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

    def test_placeholders_preserved_in_translation(self):
        # {n}/{w} placeholders must survive into zh-CN so tf() can substitute.
        self.assertIn("{n}", self.zh["ct.err_rollup_some"])
        self.assertIn("{w}", self.zh["ct.err_rollup_some"])
        self.assertIn("{w}", self.zh["ct.err_rollup_none"])
        self.assertIn("{n}", self.zh["ct.err_rollup_truncated"])

    def test_full_key_parity(self):
        en_keys = {k for k in self.en if not k.startswith("_")}
        zh_keys = {k for k in self.zh if not k.startswith("_")}
        self.assertEqual(en_keys - zh_keys, set(),
                         "keys in en.json but not zh-CN.json")
        self.assertEqual(zh_keys - en_keys, set(),
                         "keys in zh-CN.json but not en.json")


if __name__ == "__main__":
    unittest.main()
