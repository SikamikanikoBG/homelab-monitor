"""Tests for the 🔗 "Explain this moment" slice — clicking a HOT cell on the 🌈
anomaly-timeline ribbon (`renderAnomalyTimeline`) asks the local Copilot to explain
what was happening at that bucket's timestamp, reusing the EXISTING explain surface
(`explainRibbonCell` → POST /api/copilot/explain[/stream]).

Coverage:
  • the ribbon cells the timeline renders carry the CANONICAL series keys that
    `_explain_context` / `_EXPLAIN_COLS` understand (no display/label key leaks into
    the explain point) — for every scored series;
  • `_explain_context` honours a HISTORICAL ts (the clicked bucket), anchoring the
    window/facts at that moment rather than "now", with NO value/baseline supplied
    (the timeline cell only knows score+dir — the server re-derives from the DB);
  • the explain call is LLM-free at the context/facts layer (tripwire) — the LLM
    fires only later, on the explicit streamed generation, never on ribbon load;
  • the frontend wires ONLY hot cells as role=button/keyboard-actionable and routes
    them through the existing explainRibbonCell drawer (static markup checks);
  • quiet ('flat') / null cells stay inert (no button in the adapter);
  • the new visible i18n key exists in BOTH locales (en + zh-CN parity).
"""
import json
import os
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASH = os.path.join(ROOT, "static", "dashboard.html")
EN = os.path.join(ROOT, "locales", "en.json")
ZH = os.path.join(ROOT, "locales", "zh-CN.json")


class TestCanonicalKeyMapping(unittest.TestCase):
    """Every key the ribbon emits must be a key the explain context understands —
    if the ribbon ever emitted a display/label key, the click would explain the
    wrong (or no) series. This is the load-bearing contract for the slice."""

    def test_ribbon_series_keys_are_explain_context_keys(self):
        ribbon_keys = {k for (k, *_rest) in app._ANOMALY_SERIES}
        explain_keys = set(app._EXPLAIN_COLS)          # built from the same tuple
        self.assertTrue(ribbon_keys)
        self.assertEqual(ribbon_keys - explain_keys, set(),
                         "ribbon emits a key the explain context can't resolve")

    def test_disk_io_key_shape_is_canonical(self):
        # disk_io anomaly keys are "disk_io:<dev>"; _explain_context special-cases
        # exactly this prefix (see the disk_io branch). Confirm the contract holds.
        ctx = app._explain_context({"key": "disk_io:sda", "ts": int(time.time()),
                                    "direction": "spike"})
        self.assertEqual(ctx["key"], "disk_io:sda")
        self.assertIn("disk I/O", ctx["label"])

    def test_tlpoint_passes_disk_io_key_through_unchanged(self):
        # The renderer builds explain points as {key:series.key, ...}; for a disk_io
        # row the series key IS "disk_io:<dev>", so the click posts that exact key.
        # Static contract check on the adapter body (no browser).
        with open(DASH, encoding="utf-8") as f:
            html = f.read()
        i = html.index("function _tlPointFromCell(")
        j = html.index("function explainTimelineCell(", i)
        body = html[i:j]
        # No hard-coded GPU/power key allowlist that would drop disk_io keys — the
        # adapter must key on series.key verbatim so disk_io:<dev> flows through.
        self.assertIn("key:series.key", body)
        self.assertNotIn("disk_io", body.replace("series.key", ""))  # no special path needed


class TestExplainAtHistoricalTs(unittest.TestCase):
    """A clicked bucket carries a ts in the past; the explanation must anchor there."""

    def setUp(self):
        with app.LOCK:
            self._saved = app.DB.execute(
                "SELECT ts,util,mem_used,mem_total,power,temp FROM samples").fetchall()
            app.DB.execute("DELETE FROM samples")
            app.DB.commit()

    def tearDown(self):
        with app.LOCK:
            app.DB.execute("DELETE FROM samples")
            app.DB.executemany(
                "INSERT OR REPLACE INTO samples(ts,util,mem_used,mem_total,power,temp) "
                "VALUES(?,?,?,?,?,?)", self._saved)
            app.DB.commit()

    def _seed_spike_at(self, spike_ts, now):
        # quiet ~20% history across the last ~2h, then a tight 95% burst around spike_ts
        with app.LOCK:
            for i in range(200):
                ts = now - 7200 + i * 30
                v = 95.0 if abs(ts - spike_ts) <= 45 else 20.0 + (i % 3)
                app.DB.execute(
                    "INSERT OR REPLACE INTO samples(ts,util,mem_used,mem_total,power,temp) "
                    "VALUES(?,?,?,?,?,?)", (ts, v, 0, 0, 0, 0))
            app.DB.commit()

    def test_context_window_anchored_at_clicked_ts_no_llm(self):
        now = int(time.time())
        spike_ts = now - 3600            # one hour ago — a historical bucket
        self._seed_spike_at(spike_ts, now)
        # The timeline cell knows only key+ts+dir (NO value/baseline/z).
        point = {"key": "gpu_util", "ts": spike_ts, "direction": "spike"}
        with patch("app._ollama_generate", side_effect=AssertionError("LLM must not run")), \
             patch("app._ollama_generate_stream", side_effect=AssertionError("LLM must not run")):
            ctx = app._explain_context(point, now=now)
            facts = app._explain_facts(ctx)
        # ts is honoured (clamped to the clicked moment, not "now")
        self.assertEqual(ctx["ts"], spike_ts)
        # the window is re-derived from the DB AT that ts and sees the burst
        self.assertIn("window", ctx)
        self.assertIsNotNone(ctx["window"].get("at"))
        self.assertGreater(ctx["window"]["at"], 80.0,
                           "window 'at' should read the ~95% burst at the clicked ts")
        # facts mention the clicked wall-clock minute (anchored, not 'now')
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(spike_ts))
        self.assertTrue(any(when in f for f in facts),
                        "explain facts must be anchored at the clicked bucket time")

    def test_missing_value_baseline_is_tolerated(self):
        # The timeline never supplies value/baseline — the facts must still render.
        now = int(time.time())
        with patch("app._ollama_generate", side_effect=AssertionError("LLM must not run")):
            ctx = app._explain_context({"key": "gpu_util", "ts": now - 600,
                                        "direction": "dip"}, now=now)
            facts = app._explain_facts(ctx)
        self.assertIsNone(ctx.get("value"))
        self.assertIsNone(ctx.get("baseline"))
        self.assertTrue(facts and isinstance(facts[0], str))


class TestFrontendWiring(unittest.TestCase):
    """Static markup guardrails — no browser. Prove the timeline is interactive and
    routes through the ONE existing explain drawer, and that only hot cells are wired."""

    def setUp(self):
        with open(DASH, encoding="utf-8") as f:
            self.html = f.read()

    def test_timeline_adapter_and_reuse_present(self):
        for fn in ("function explainTimelineCell(", "function _tlPointFromCell(",
                   "function renderAnomalyTimeline("):
            self.assertIn(fn, self.html, f"missing timeline JS: {fn}")
        # the adapter must delegate to the EXISTING explainRibbonCell drawer,
        # not fork a second explain modal/endpoint.
        self.assertIn("return explainRibbonCell(_tlPointFromCell", self.html)

    def test_hot_cells_are_button_and_keyboard_actionable(self):
        # role=button + tabindex + Enter/Space handling wired inside the render loop
        for token in ("cell.setAttribute('role','button')",
                      "cell.setAttribute('tabindex','0')",
                      "e.key==='Enter'||e.key===' '",
                      "cell.addEventListener('click', fire)"):
            self.assertIn(token, self.html, f"missing actionable wiring: {token}")

    def test_only_hot_cells_wired(self):
        # the actionability is gated on `hot` (dir!=='flat' && score·threshold≥z);
        # quiet/flat cells must not get a button.
        self.assertIn("c.dir!=='flat' && (c.score*j.threshold) >= RIBBON_TL_Z", self.html)
        self.assertIn("if(hot){", self.html)
        # the adapter itself refuses quiet/null cells defensively
        self.assertIn("if(!series || !cell || cell.dir==='flat') return;", self.html)

    def test_canonical_series_key_flows_into_point(self):
        # _tlPointFromCell must key the explain point on the series' REAL key.
        self.assertIn("return {key:series.key, ts:cell.ts", self.html)

    def test_no_llm_fetch_on_timeline_load(self):
        # loadAnomalyTimeline must fetch ONLY /api/anomaly_ribbon (LLM-free) — the
        # copilot explain endpoints must not appear inside its body.
        i = self.html.index("async function loadAnomalyTimeline(")
        j = self.html.index("function _atlColor(", i)
        body = self.html[i:j]
        self.assertIn("/api/anomaly_ribbon", body)
        self.assertNotIn("/api/copilot/explain", body)
        self.assertNotIn("explainRibbonCell", body)
        self.assertNotIn("explainTimelineCell", body)


class TestI18nParity(unittest.TestCase):
    NEW_KEYS = ["ribbon.click_explain"]

    def setUp(self):
        with open(EN, encoding="utf-8") as f:
            self.en = json.load(f)
        with open(ZH, encoding="utf-8") as f:
            self.zh = json.load(f)

    def test_new_keys_in_both_locales(self):
        for k in self.NEW_KEYS:
            self.assertIn(k, self.en, f"en.json missing {k}")
            self.assertIn(k, self.zh, f"zh-CN.json missing {k}")

    def test_full_key_parity(self):
        en_keys = {k for k in self.en if not k.startswith("_")}
        zh_keys = {k for k in self.zh if not k.startswith("_")}
        self.assertEqual(en_keys - zh_keys, set())
        self.assertEqual(zh_keys - en_keys, set())


if __name__ == "__main__":
    unittest.main()
