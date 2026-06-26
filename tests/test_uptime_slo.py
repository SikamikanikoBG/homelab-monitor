"""Unit tests for the SLO error-budget + burn-rate view on uptime checks:
target parsing (percent string → fraction, garbage → default, 100% no-budget),
budget-consumed math at known down/total, burn-rate >1 when recent failures
spike, clamp/guard at target=100%, total=0 → not sufficient + no NaN, the `slo`
sub-object present in uptime_overview (and existing keys untouched), settings
round-trip for slo_target, the digest mention firing ONLY when over budget /
burning, and that the public /status payload carries NO slo internals. Pure
stats over stored rows — NO network."""
import os
import sys
import json
import math
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


def _clean_db():
    with app.LOCK:
        app.DB.execute("DELETE FROM uptime_checks")
        app.DB.execute("DELETE FROM uptime_results")
        app.DB.execute("DELETE FROM settings")
        app.DB.commit()
    app._uptime_due.clear()


def _no_nan(d):
    for v in d.values():
        if isinstance(v, float):
            assert not (math.isnan(v) or math.isinf(v)), v


class TestSloTargetParse(unittest.TestCase):
    def test_percent_string_to_fraction(self):
        self.assertAlmostEqual(app._parse_slo_target("99.9"), 0.999)
        self.assertAlmostEqual(app._parse_slo_target("99.95%"), 0.9995)
        self.assertAlmostEqual(app._parse_slo_target(" 95 "), 0.95)

    def test_garbage_and_empty_default(self):
        for bad in (None, "", "  ", "abc", "-5", "0", "120", "nan"):
            self.assertAlmostEqual(app._parse_slo_target(bad), 0.999, msg=bad)

    def test_hundred_percent_allowed(self):
        self.assertAlmostEqual(app._parse_slo_target("100"), 1.0)


class TestSloMath(unittest.TestCase):
    def _rows(self, now, n_up, n_down, step=60, span_start=None):
        """Build (ts, up) rows oldest→newest. By default they span n*step seconds."""
        rows = []
        total = n_up + n_down
        start = span_start if span_start is not None else now - total * step
        ups = [1] * n_up + [0] * n_down
        for i, up in enumerate(ups):
            rows.append((start + i * step, up))
        return rows

    def test_budget_consumed_known(self):
        now = int(time.time())
        # 990 up, 10 down → observed_fail 0.01; target 99.9% → allowed_fail 0.001
        # budget consumed = 0.01/0.001*100 = 1000% → clamped to 999.
        # Use a wide enough span (>= 2% of 30d ≈ 12.96h) so data_sufficient is True.
        rows = self._rows(now, 990, 10, step=120)
        slo = app._uptime_slo(rows, now, 0.999)
        self.assertEqual(slo["total"], 1000)
        self.assertEqual(slo["down"], 10)
        self.assertAlmostEqual(slo["observed_fail"], 0.01, places=4)
        self.assertTrue(slo["data_sufficient"])
        self.assertEqual(slo["budget_consumed_pct"], 999.0)
        self.assertTrue(slo["over_budget"])
        _no_nan(slo)

    def test_budget_consumed_partial(self):
        now = int(time.time())
        # 1000 up, 1 down → observed 1/1001 ≈ 0.000999; target 99% → allowed 0.01
        # consumed ≈ 9.99% → green, not over budget.
        rows = self._rows(now, 1000, 1, step=120)
        slo = app._uptime_slo(rows, now, 0.99)
        self.assertLess(slo["budget_consumed_pct"], 20)
        self.assertGreater(slo["budget_consumed_pct"], 5)
        self.assertFalse(slo["over_budget"])
        _no_nan(slo)

    def test_burn_rate_spikes_with_recent_failures(self):
        now = int(time.time())
        # Long clean history, then a burst of failures in the last hour.
        rows = []
        # 2000 clean samples spanning ~2.7 days before the last hour
        start = now - 3 * 86400
        for i in range(2000):
            rows.append((start + i * 120, 1))
        # last hour: 30 samples, 20 down → recent fail frac ~0.67
        for i in range(30):
            up = 0 if i < 20 else 1
            rows.append((now - 3600 + i * 110, up))
        slo = app._uptime_slo(rows, now, 0.999)  # allowed_fail 0.001
        self.assertIsNotNone(slo["burn_1h"])
        self.assertGreater(slo["burn_1h"], 1.0)
        self.assertTrue(slo["burning"])
        _no_nan(slo)

    def test_no_burn_when_clean_recent(self):
        now = int(time.time())
        rows = self._rows(now, 1000, 0, step=120)
        slo = app._uptime_slo(rows, now, 0.999)
        self.assertEqual(slo["burn_1h"], 0.0)
        self.assertFalse(slo["burning"])
        _no_nan(slo)

    def test_target_100_no_budget(self):
        now = int(time.time())
        # target 100% → allowed_fail 0; any failure = over budget (sentinel 999).
        rows = self._rows(now, 500, 5, step=120)
        slo = app._uptime_slo(rows, now, 1.0)
        self.assertEqual(slo["allowed_fail"], 0.0)
        self.assertEqual(slo["budget_consumed_pct"], 999.0)
        self.assertTrue(slo["over_budget"])
        self.assertIsNone(slo["burn_1h"])  # undefined with no budget
        _no_nan(slo)

    def test_target_100_clean_no_failure(self):
        now = int(time.time())
        rows = self._rows(now, 600, 0, step=120)
        slo = app._uptime_slo(rows, now, 1.0)
        self.assertIsNone(slo["budget_consumed_pct"])  # no budget, no fails → n/a
        self.assertFalse(slo["over_budget"])
        _no_nan(slo)

    def test_total_zero_not_sufficient_no_crash(self):
        now = int(time.time())
        slo = app._uptime_slo([], now, 0.999)
        self.assertEqual(slo["total"], 0)
        self.assertFalse(slo["data_sufficient"])
        self.assertIsNone(slo["budget_consumed_pct"])
        self.assertIsNone(slo["burn_1h"])
        self.assertEqual(slo["window_days_actual"], 0.0)
        _no_nan(slo)

    def test_too_few_samples_collecting(self):
        now = int(time.time())
        # 5 samples over a tiny span → not enough to trust a %.
        rows = self._rows(now, 5, 0, step=60)
        slo = app._uptime_slo(rows, now, 0.999)
        self.assertFalse(slo["data_sufficient"])
        _no_nan(slo)

    def test_window_excludes_old_rows(self):
        now = int(time.time())
        rows = [(now - 40 * 86400, 0), (now - 39 * 86400, 0)]  # outside 30d window
        rows += [(now - i * 120, 1) for i in range(50)]
        slo = app._uptime_slo(rows, now, 0.999)
        self.assertEqual(slo["total"], 50)  # the 2 old down rows excluded
        self.assertEqual(slo["down"], 0)


class TestSloInOverview(unittest.TestCase):
    def setUp(self):
        _clean_db()

    def test_slo_present_existing_keys_intact(self):
        cid, _ = app.create_uptime_check(
            {"label": "api", "type": "http", "target": "https://api.example"})
        now = int(time.time())
        with app.LOCK:
            rows = [(cid, now - i * 120, 1 if i % 100 else 0, 5.0, 200, None)
                    for i in range(800)]
            app.DB.executemany(
                "INSERT INTO uptime_results(check_id,ts,up,latency_ms,code,err) VALUES(?,?,?,?,?,?)", rows)
            app.DB.commit()
        ov = app.uptime_overview()
        c = ov["checks"][0]
        # existing keys untouched
        for k in ("state", "uptime", "window_total", "strip", "last_checked"):
            self.assertIn(k, c)
        # new slo sub-object
        self.assertIn("slo", c)
        slo = c["slo"]
        for k in ("target", "allowed_fail", "total", "down", "budget_consumed_pct",
                  "burn_1h", "window_days_actual", "data_sufficient"):
            self.assertIn(k, slo)
        self.assertGreater(slo["total"], 0)
        _no_nan(slo)

    def test_slo_target_setting_round_trip(self):
        app.save_settings({"slo_target": "99.5"})
        self.assertEqual(app.get_settings()["slo_target"], "99.5")
        cid, _ = app.create_uptime_check(
            {"label": "x", "type": "tcp", "target": "h:1"})
        now = int(time.time())
        with app.LOCK:
            rows = [(cid, now - i * 120, 1, 5.0, None, None) for i in range(400)]
            app.DB.executemany(
                "INSERT INTO uptime_results(check_id,ts,up,latency_ms,code,err) VALUES(?,?,?,?,?,?)", rows)
            app.DB.commit()
        ov = app.uptime_overview()
        self.assertAlmostEqual(ov["checks"][0]["slo"]["target"], 0.995)

    def test_status_payload_has_no_slo(self):
        cid, _ = app.create_uptime_check(
            {"label": "secret-api", "type": "http", "target": "https://internal"})
        now = int(time.time())
        with app.LOCK:
            rows = [(cid, now - i * 120, 1, 5.0, 200, None) for i in range(400)]
            app.DB.executemany(
                "INSERT INTO uptime_results(check_id,ts,up,latency_ms,code,err) VALUES(?,?,?,?,?,?)", rows)
            app.DB.commit()
        pub = app.build_public_status()
        blob = json.dumps(pub)
        self.assertNotIn("slo", blob)
        self.assertNotIn("budget_consumed", blob)
        self.assertNotIn("burn_", blob)


class TestSloDigestMention(unittest.TestCase):
    def setUp(self):
        _clean_db()

    def _seed_check(self, label, n_up, n_down):
        cid, _ = app.create_uptime_check(
            {"label": label, "type": "http", "target": "https://%s.example" % label})
        now = int(time.time())
        rows = []
        for i in range(n_up):
            rows.append((cid, now - (n_up + n_down - i) * 120, 1, 5.0, 200, None))
        for i in range(n_down):
            rows.append((cid, now - (n_down - i) * 120, 0, None, None, "fail"))
        with app.LOCK:
            app.DB.executemany(
                "INSERT INTO uptime_results(check_id,ts,up,latency_ms,code,err) VALUES(?,?,?,?,?,?)", rows)
            app.DB.commit()
        return cid

    def _fleet_lines(self):
        for header, lines in app._digest_sections():
            if header == "Fleet / Uptime":
                return lines
        return []

    def test_mention_appears_when_over_budget(self):
        app.save_settings({"slo_target": "99.9"})  # allowed_fail 0.001
        # 500 up, 20 down → observed 0.0385 >> 0.001 → way over budget.
        self._seed_check("payments", 500, 20)
        lines = self._fleet_lines()
        joined = " ".join(lines)
        self.assertIn("payments", joined)
        self.assertIn("error budget", joined)

    def test_silent_when_within_budget(self):
        app.save_settings({"slo_target": "99.0"})  # allowed_fail 0.01
        # 1000 up, 0 down → 0% consumed → nothing notable.
        self._seed_check("healthy", 1000, 0)
        lines = self._fleet_lines()
        joined = " ".join(lines)
        self.assertNotIn("error budget", joined)
        self.assertNotIn("burning", joined)


if __name__ == "__main__":
    unittest.main()
