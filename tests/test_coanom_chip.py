"""Tests for the 🔗 co-anomaly discoverability chip in the Anomalies card header.

The chip is FRONTEND-ONLY and ADDITIVE: it derives purely from the already-fetched
`/api/anomaly_ribbon` payload the loader holds in `ANOM_TL` (specifically
`co_anomaly_windows`) — NO new fetch, NO new endpoint, NO backend change. It shows
"🔗 N correlated moments" in the `#anom-card` header when `co_anomaly_windows > 0`,
and hides cleanly (no throw, no layout shift) when 0 / absent / not yet loaded.
Clicking it scrolls the ribbon strip into view (reduced-motion aware).

Coverage:
  • the chip element + its click/scroll wiring exist in the markup;
  • the derive helper `renderCoAnomChip()` reads `co_anomaly_windows` and shows
    (>0) / hides (0 or absent or null payload) accordingly;
  • the chip is refreshed from the same place the ribbon renders + on tab activation;
  • it introduces NO new fetch (grep the wiring — still exactly one ribbon fetch);
  • the new visible i18n keys exist in BOTH locales (en + zh-CN) with parity.
"""
import json
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EN = os.path.join(ROOT, "locales", "en.json")
ZH = os.path.join(ROOT, "locales", "zh-CN.json")
HTML = os.path.join(ROOT, "static", "dashboard.html")


class TestCoAnomChipMarkup(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(HTML, encoding="utf-8") as f:
            cls.html = f.read()

    def test_chip_element_present_in_header(self):
        # A dedicated chip button lives in the Anomalies card header, hidden by default.
        self.assertIn('id="coanom-chip"', self.html)
        m = re.search(r'<button[^>]*id="coanom-chip"[^>]*>', self.html)
        self.assertIsNotNone(m, "coanom-chip should be a <button> affordance")
        tag = m.group(0)
        self.assertIn("hidden", tag, "chip must start hidden (degrade with no payload)")
        self.assertIn("chip", tag, "chip must reuse the .chip token recipe")

    def test_derive_helper_reads_co_anomaly_windows(self):
        self.assertIn("function renderCoAnomChip(", self.html)
        # It derives from the already-held payload, not a new fetch.
        self.assertIn("ANOM_TL.co_anomaly_windows", self.html)

    def test_helper_shows_when_positive_hides_otherwise(self):
        # Extract the helper body and assert both the show and hide branches exist.
        body = self.html.split("function renderCoAnomChip(", 1)[1]
        body = body.split("function _atlColor(", 1)[0]
        self.assertIn("el.hidden=true", body, "must hide when not >0")
        self.assertIn("el.hidden=false", body, "must show when >0")
        self.assertIn("n>0", body, "gate on co_anomaly_windows > 0")
        # null-safe: guards the payload before touching the field.
        self.assertIn("ANOM_TL &&", body)

    def test_click_scrolls_ribbon_and_respects_reduced_motion(self):
        body = self.html.split("function renderCoAnomChip(", 1)[1]
        body = body.split("function _atlColor(", 1)[0]
        self.assertIn("anom-timeline-wrap", body, "click targets the ribbon wrap")
        self.assertIn("scrollIntoView", body)
        self.assertIn("prefers-reduced-motion", body,
                      "scroll/flash must respect reduced-motion")

    def test_chip_refreshed_from_render_and_tab_activation(self):
        # Called from renderAnomalyTimeline (so it's never stale when the payload
        # renders) and on GPU-tab activation (so it hydrates from cache).
        render_body = self.html.split("function renderAnomalyTimeline(", 1)[1]
        render_body = render_body.split("\n}", 1)[0]
        self.assertIn("renderCoAnomChip()", render_body)
        self.assertIn("loadAnomalyTimeline(); renderCoAnomChip()", self.html)

    def test_no_new_fetch_introduced(self):
        # Frontend-only, additive: the ribbon endpoint must still be fetched exactly
        # once (in loadAnomalyTimeline). The chip reuses ANOM_TL — no second fetch.
        self.assertEqual(self.html.count("fetch('/api/anomaly_ribbon')"), 1)
        # The chip helper contains no fetch at all.
        body = self.html.split("function renderCoAnomChip(", 1)[1]
        body = body.split("function _atlColor(", 1)[0]
        self.assertNotIn("fetch(", body)


class TestCoAnomChipI18n(unittest.TestCase):
    NEW_KEYS = ["anom.coanom_chip", "anom.coanom_tip"]

    def setUp(self):
        with open(EN, encoding="utf-8") as f:
            self.en = json.load(f)
        with open(ZH, encoding="utf-8") as f:
            self.zh = json.load(f)

    def test_new_keys_in_both_locales(self):
        for k in self.NEW_KEYS:
            self.assertIn(k, self.en, f"en.json missing {k}")
            self.assertIn(k, self.zh, f"zh-CN.json missing {k}")
        # count placeholder preserved in the translated chip string
        self.assertIn("{n}", self.en["anom.coanom_chip"])
        self.assertIn("{n}", self.zh["anom.coanom_chip"])

    def test_full_key_parity(self):
        en_keys = {k for k in self.en if not k.startswith("_")}
        zh_keys = {k for k in self.zh if not k.startswith("_")}
        self.assertEqual(en_keys - zh_keys, set())
        self.assertEqual(zh_keys - en_keys, set())


if __name__ == "__main__":
    unittest.main()
