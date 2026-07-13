"""Tests for the monthly power-cost budget (E1) — a persistent `monthly_cost_budget`
setting plus a glanceable `budget` block on the AUTHED /api/forecast cost surface.

The budget block is pure display off the EXISTING _cost_projection (no new query,
no LLM). It is AUTHED-only: it must NEVER appear on any public status/feed/report
surface. Default is "0" (disabled) so a fresh install behaves byte-identically to
before until a budget is set."""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


def _seed_month(power_w=200):
    """Seed this-month `samples` (one per ~half hour) so _cost_projection has data.
    Mirrors the seeding used by the existing forecast/cost tests."""
    now = int(time.time())
    lt = time.localtime(now)
    month_start = int(time.mktime((lt.tm_year, lt.tm_mon, 1, 0, 0, 0, 0, 0, -1)))
    with app.LOCK:
        app.DB.execute("DELETE FROM samples WHERE ts>=?", (month_start,))
        ts = month_start + 3600
        while ts < now:
            app.DB.execute("INSERT OR REPLACE INTO samples(ts,util,mem_used,mem_total,power,temp) "
                           "VALUES(?,?,?,?,?,?)", (ts, 0, 0, 0, power_w, 0))
            ts += app.INTERVAL * 30
        app.DB.commit()
    return now


class TestBudgetSetting(unittest.TestCase):
    """The setting exists, defaults to OFF, round-trips through /api/settings and
    rejects junk — mirroring the existing numeric settings, no new persistence."""

    def test_default_is_off(self):
        self.assertIn("monthly_cost_budget", app.SETTING_DEFAULTS)
        self.assertEqual(app.SETTING_DEFAULTS["monthly_cost_budget"], "0")

    def test_round_trips_via_api_settings(self):
        c = app.app.test_client()
        try:
            r = c.post("/api/settings", json={"monthly_cost_budget": "42.50"})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.get_json()["settings"]["monthly_cost_budget"], "42.50")
            # persisted, not just echoed
            self.assertEqual(app.get_settings()["monthly_cost_budget"], "42.50")
            self.assertAlmostEqual(app._budget_amount(), 42.5, places=3)
        finally:
            app.save_settings({"monthly_cost_budget": "0"})

    def test_rejects_non_numeric(self):
        c = app.app.test_client()
        r = c.post("/api/settings", json={"monthly_cost_budget": "abc"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("monthly_cost_budget", (r.get_json() or {}).get("error", ""))

    def test_rejects_negative(self):
        c = app.app.test_client()
        r = c.post("/api/settings", json={"monthly_cost_budget": "-5"})
        self.assertEqual(r.status_code, 400)

    def test_blank_is_allowed_and_disables(self):
        c = app.app.test_client()
        r = c.post("/api/settings", json={"monthly_cost_budget": ""})
        self.assertEqual(r.status_code, 200)
        # blank / "0" / garbage all coerce to disabled (0.0)
        app.save_settings({"monthly_cost_budget": ""})
        self.assertEqual(app._budget_amount(), 0.0)
        app.save_settings({"monthly_cost_budget": "0"})
        self.assertEqual(app._budget_amount(), 0.0)
        app.save_settings({"monthly_cost_budget": "junk"})
        self.assertEqual(app._budget_amount(), 0.0)
        app.save_settings({"monthly_cost_budget": "0"})


class TestBudgetStatus(unittest.TestCase):
    """_budget_status thresholds + percentages off a seeded projection dict."""

    def _cm(self, mtd, proj, currency="€"):
        return {"enabled": True, "currency": currency,
                "month_to_date": mtd, "projected_month": proj}

    def test_disabled_when_budget_zero(self):
        self.assertEqual(app._budget_status(self._cm(10, 20), 0), {"enabled": False})
        self.assertEqual(app._budget_status(self._cm(10, 20), 0.0), {"enabled": False})
        self.assertEqual(app._budget_status(self._cm(10, 20), -3), {"enabled": False})

    def test_disabled_when_projection_disabled(self):
        self.assertEqual(app._budget_status({"enabled": False}, 50), {"enabled": False})
        self.assertEqual(app._budget_status(None, 50), {"enabled": False})

    def test_status_ok(self):
        # projected under budget -> ok
        b = app._budget_status(self._cm(10.0, 40.0), 100.0)
        self.assertTrue(b["enabled"])
        self.assertEqual(b["status"], "ok")
        self.assertEqual(b["budget"], 100.0)
        self.assertEqual(b["pct_used"], 10.0)
        self.assertEqual(b["pct_projected"], 40.0)
        self.assertEqual(b["currency"], "€")

    def test_status_warn(self):
        # MTD under budget but projected over -> warn
        b = app._budget_status(self._cm(60.0, 120.0), 100.0)
        self.assertEqual(b["status"], "warn")
        self.assertEqual(b["pct_used"], 60.0)
        self.assertEqual(b["pct_projected"], 120.0)

    def test_status_over(self):
        # MTD already over budget -> over (regardless of projection)
        b = app._budget_status(self._cm(150.0, 300.0), 100.0)
        self.assertEqual(b["status"], "over")
        self.assertEqual(b["pct_used"], 150.0)

    def test_percentages_correct(self):
        b = app._budget_status(self._cm(25.0, 75.0), 50.0)
        self.assertAlmostEqual(b["pct_used"], 50.0, places=1)
        self.assertAlmostEqual(b["pct_projected"], 150.0, places=1)

    def test_currency_follows_projection(self):
        b = app._budget_status(self._cm(1.0, 2.0, currency="$"), 100.0)
        self.assertEqual(b["currency"], "$")


class TestBudgetForecastBlock(unittest.TestCase):
    """The /api/forecast payload carries a `budget` block that toggles on the
    setting and reuses _cost_projection (no LLM, no new heavy query)."""

    def setUp(self):
        self._saved = {k: app.get_settings().get(k)
                       for k in ("kwh_price", "currency", "tariff_mode", "monthly_cost_budget")}

    def tearDown(self):
        app.save_settings({k: (v if v is not None else "") for k, v in self._saved.items()})

    def test_block_absent_meaning_when_budget_zero(self):
        _seed_month()
        app.save_settings({"kwh_price": "0.30", "currency": "€",
                           "tariff_mode": "single", "monthly_cost_budget": "0"})
        j = app.app.test_client().get("/api/forecast").get_json()
        self.assertIn("budget", j)
        self.assertFalse(j["budget"]["enabled"])

    def test_block_enabled_with_budget(self):
        _seed_month()
        app.save_settings({"kwh_price": "0.30", "currency": "€", "tariff_mode": "single"})
        cm = app.app.test_client().get("/api/forecast").get_json()["cost_month"]
        self.assertTrue(cm["enabled"])
        # pick a budget above projected so status is unambiguously ok
        budget = round(cm["projected_month"] + 10.0, 2)
        app.save_settings({"monthly_cost_budget": str(budget)})
        j = app.app.test_client().get("/api/forecast").get_json()
        b = j["budget"]
        self.assertTrue(b["enabled"])
        self.assertEqual(b["status"], "ok")
        self.assertEqual(b["budget"], budget)
        self.assertEqual(b["month_to_date"], cm["month_to_date"])
        self.assertEqual(b["projected_month"], cm["projected_month"])
        self.assertEqual(b["currency"], "€")

    def test_block_over_when_budget_below_mtd(self):
        _seed_month()
        app.save_settings({"kwh_price": "0.30", "currency": "€", "tariff_mode": "single"})
        cm = app.app.test_client().get("/api/forecast").get_json()["cost_month"]
        # budget well below MTD -> over
        budget = max(0.01, round(cm["month_to_date"] * 0.5, 2))
        app.save_settings({"monthly_cost_budget": str(budget)})
        b = app.app.test_client().get("/api/forecast").get_json()["budget"]
        self.assertEqual(b["status"], "over")

    def test_default_off_leaves_forecast_shape_intact(self):
        # With no budget set, all the established forecast keys still exist.
        app.save_settings({"monthly_cost_budget": "0"})
        j = app.app.test_client().get("/api/forecast").get_json()
        for k in ("now", "disk", "cost_month", "anomalies", "vram", "budget"):
            self.assertIn(k, j)
        self.assertFalse(j["budget"]["enabled"])


class TestBudgetPrivacyAndLLM(unittest.TestCase):
    """Budget/cost is AUTHED-only and LLM-free."""

    def test_absent_from_public_status(self):
        app.save_settings({"kwh_price": "0.30", "currency": "€",
                           "monthly_cost_budget": "50"})
        try:
            j = app.build_public_status()
            blob = repr(j)
            self.assertNotIn("budget", blob)
            self.assertNotIn("monthly_cost_budget", blob)
            self.assertNotIn("month_to_date", blob)
        finally:
            app.save_settings({"monthly_cost_budget": "0"})

    def test_absent_from_api_status(self):
        app.save_settings({"kwh_price": "0.30", "currency": "€",
                           "monthly_cost_budget": "50"})
        try:
            sp = app.STATUS_PAGE
            app.STATUS_PAGE = True
            try:
                r = app.app.test_client().get("/api/status")
                blob = r.get_data(as_text=True)
                self.assertNotIn("monthly_cost_budget", blob)
                self.assertNotIn("month_to_date", blob)
            finally:
                app.STATUS_PAGE = sp
        finally:
            app.save_settings({"monthly_cost_budget": "0"})

    def test_budget_status_is_llm_free(self):
        # Tripwire: computing the budget block must not call any LLM helper. Wrap
        # the common LLM entrypoints; if the read path touches them, fail loudly.
        called = {"n": 0}
        wrapped = []
        for name in ("_llm_chat", "llm_chat", "_llm_complete", "ollama_generate",
                     "_ollama_chat"):
            fn = getattr(app, name, None)
            if callable(fn):
                def make(orig):
                    def spy(*a, **k):
                        called["n"] += 1
                        return orig(*a, **k)
                    return spy
                wrapped.append((name, fn))
                setattr(app, name, make(fn))
        try:
            cm = {"enabled": True, "currency": "€",
                  "month_to_date": 30.0, "projected_month": 90.0}
            b = app._budget_status(cm, 100.0)
            self.assertTrue(b["enabled"])
        finally:
            for name, fn in wrapped:
                setattr(app, name, fn)
        self.assertEqual(called["n"], 0)


if __name__ == "__main__":
    unittest.main()
