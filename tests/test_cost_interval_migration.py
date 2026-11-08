"""Regression test for #01 cost-interval-not-stored: SAMPLE_INTERVAL must never
retroactively reprice history. Seeds raw + rollup rows as if written at
SAMPLE_INTERVAL=10, then flips app.INTERVAL to 5 (simulating the user changing
SAMPLE_INTERVAL) WITHOUT touching the already-written rows, and asserts every
cost/energy endpoint returns the same numbers before and after. A freshly
written row after the flip must price at the NEW interval -- only history is
protected.
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app
from backend.db.repos import samples as samples_repo
from backend.db.repos import host_samples as hs_repo


def _clean():
    with app.LOCK:
        for tbl in ("samples", "samples_1h", "host_samples", "host_samples_1h", "power_proc"):
            app.DB.execute(f"DELETE FROM {tbl} WHERE 1=1" if tbl != "power_proc" else "DELETE FROM power_proc")
        app.DB.execute("DELETE FROM host_samples WHERE host='mh1'")
        app.DB.execute("DELETE FROM host_samples_1h WHERE host='mh1'")
        app.DB.commit()


class TestCostHistoryImmuneToIntervalChange(unittest.TestCase):
    """Seed history as if written at interval=10, then flip app.INTERVAL to 5
    and confirm /api/cost, /api/costs, /api/costs/entity are unchanged for the
    already-written window."""

    OLD_INTERVAL = 10
    NEW_INTERVAL = 5
    WATTS = 200

    def setUp(self):
        _clean()
        self.now = int(time.time())
        self.orig_interval = app.INTERVAL
        self._seed_history(self.OLD_INTERVAL, self.WATTS)
        app.save_settings({"kwh_price": "0.30", "currency": "$", "tariff_mode": "single",
                           "system_idle_watts": ""})

    def tearDown(self):
        app.INTERVAL = self.orig_interval
        _clean()

    def _seed_history(self, interval_sec, watts, anchor=None):
        """One full hour of samples exactly as the real write path would have
        produced them at `interval_sec`: raw rows carry interval_sec, the
        rollup's wsec is power*cnt*interval_sec (matching rollup_now's math).
        `anchor` picks the hour to fill; a second seed passes a DIFFERENT
        anchor so it lands as a genuinely new rollup row instead of
        overwriting/ignoring the first hour's."""
        anchor = self.now if anchor is None else anchor
        with app.LOCK:
            n = 3600 // interval_sec
            for i in range(n):
                ts = anchor - 3600 + i * interval_sec
                app.DB.execute(
                    "INSERT OR REPLACE INTO samples(ts,util,mem_used,mem_total,power,temp,interval_sec) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (ts, 50, 0, 0, watts, 0, interval_sec))
                app.DB.execute(
                    "INSERT INTO power_proc(ts,kind,name,watts,interval_sec) VALUES(?,?,?,?,?)",
                    (ts, "gpu", "svc", watts, interval_sec))
            app.DB.commit()
            app.DB.executescript(f"""
                INSERT OR IGNORE INTO samples_1h(ts,util,mem_used,mem_total,power,temp,
                    cpu,ram_used,ram_total,load1,ctemp,cpu_power,dram_power,cnt,wsec,cpu_wsec,dram_wsec)
                SELECT (ts/3600)*3600, AVG(util), AVG(mem_used), AVG(mem_total), AVG(power), AVG(temp),
                    AVG(cpu), AVG(ram_used), AVG(ram_total), AVG(load1), AVG(ctemp),
                    AVG(cpu_power), AVG(dram_power), COUNT(*), SUM(COALESCE(power,0)*interval_sec),
                    SUM(COALESCE(cpu_power,0)*interval_sec), SUM(COALESCE(dram_power,0)*interval_sec)
                FROM samples GROUP BY (ts/3600)*3600;
            """)
            app.DB.commit()

    def _get(self, path):
        return app.app.test_client().get(path).get_json()

    def test_api_cost_unchanged_after_interval_change(self):
        before = self._get("/api/cost?range=30d")
        app.INTERVAL = self.NEW_INTERVAL
        after = self._get("/api/cost?range=30d")
        self.assertEqual(before["kwh"], after["kwh"])
        self.assertEqual(before["cost"], after["cost"])

    def test_api_costs_unchanged_after_interval_change(self):
        before = self._get("/api/costs?range=30d")
        app.INTERVAL = self.NEW_INTERVAL
        after = self._get("/api/costs?range=30d")
        self.assertEqual(before["machines"][0]["energy_kwh"],
                         after["machines"][0]["energy_kwh"])
        self.assertEqual(before["machines"][0]["cost"], after["machines"][0]["cost"])
        before_bd = {b["name"]: b["energy_kwh"] for b in before["breakdown"]}
        after_bd = {b["name"]: b["energy_kwh"] for b in after["breakdown"]}
        self.assertEqual(before_bd, after_bd)

    def test_api_costs_entity_unchanged_after_interval_change(self):
        before = self._get("/api/costs/entity?name=svc&kind=gpu&range=30d")
        app.INTERVAL = self.NEW_INTERVAL
        after = self._get("/api/costs/entity?name=svc&kind=gpu&range=30d")
        self.assertEqual(before["energy_kwh"], after["energy_kwh"])
        self.assertEqual(before["cost"], after["cost"])

    def test_new_row_after_interval_change_uses_new_interval(self):
        """A row written AFTER the interval change, with the new interval_sec,
        must price at the NEW cadence -- only history is protected."""
        app.INTERVAL = self.NEW_INTERVAL
        before = self._get("/api/cost?range=30d")["kwh"]["d30"]
        # One more, DIFFERENT hour of samples at the NEW interval/cadence.
        self._seed_history(self.NEW_INTERVAL, self.WATTS, anchor=self.now - 7200)
        after = self._get("/api/cost?range=30d")["kwh"]["d30"]
        # A whole extra hour at 200 W = 0.2 kWh, correctly priced at the new interval.
        self.assertAlmostEqual(after - before, 0.2, delta=0.01)


class TestApiCostsCpuDramEnergyImmuneToIntervalChange(unittest.TestCase):
    """Regression for the residual gap: /api/costs' hub-local energy_kwh.cpu
    and .dram used to be derived as AVG(cpu_power)*cnt*INTERVAL (re-read at
    the CURRENT global interval), unlike energy_kwh.gpu which already read
    SUM(wsec). Seeds samples_1h directly with cpu_power/dram_power AND their
    matching cpu_wsec/dram_wsec (as rollup_now would have written them at
    OLD_INTERVAL), flips app.INTERVAL, and asserts both fields -- plus
    cost_range, which is derived from the same comp_kwh values -- are
    byte-identical before/after."""

    OLD_INTERVAL = 10
    NEW_INTERVAL = 5
    CPU_W = 40
    DRAM_W = 15

    def setUp(self):
        _clean()
        self.now = int(time.time())
        self.orig_interval = app.INTERVAL
        h = (self.now // 3600) * 3600
        cnt = 3600 // self.OLD_INTERVAL
        with app.LOCK:
            app.DB.execute(
                "INSERT INTO samples_1h(ts,power,cpu_power,dram_power,cnt,wsec,cpu_wsec,dram_wsec) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (h, 0, self.CPU_W, self.DRAM_W, cnt, 0,
                 self.CPU_W * cnt * self.OLD_INTERVAL, self.DRAM_W * cnt * self.OLD_INTERVAL))
            app.DB.commit()
        app.save_settings({"kwh_price": "0.30", "currency": "$", "tariff_mode": "single",
                           "system_idle_watts": ""})

    def tearDown(self):
        app.INTERVAL = self.orig_interval
        _clean()

    def _get(self, path):
        return app.app.test_client().get(path).get_json()

    def test_cpu_dram_energy_kwh_unchanged_after_interval_change(self):
        before = self._get("/api/costs?range=30d")
        app.INTERVAL = self.NEW_INTERVAL
        after = self._get("/api/costs?range=30d")
        self.assertEqual(before["machines"][0]["energy_kwh"]["cpu"],
                         after["machines"][0]["energy_kwh"]["cpu"])
        self.assertEqual(before["machines"][0]["energy_kwh"]["dram"],
                         after["machines"][0]["energy_kwh"]["dram"])
        self.assertEqual(before["machines"][0]["cost_range"], after["machines"][0]["cost_range"])
        # sanity: the value is actually nonzero, so the assertion is meaningful
        self.assertGreater(before["machines"][0]["energy_kwh"]["cpu"], 0)
        self.assertGreater(before["machines"][0]["energy_kwh"]["dram"], 0)


class TestRollupNowWsecAccumulation(unittest.TestCase):
    """Unit-level guarantee: wsec accumulates SUM(power_i * interval_i) across
    calls within the same hour bucket, even when interval_sec changes mid-hour
    -- the exact case the old AVG(power)*cnt*INTERVAL approach got wrong."""

    def setUp(self):
        self.db_conn = app.DB
        with app.LOCK:
            app.DB.execute("DELETE FROM samples_1h")
            app.DB.commit()

    def tearDown(self):
        with app.LOCK:
            app.DB.execute("DELETE FROM samples_1h")
            app.DB.commit()

    def test_wsec_sums_per_row_energy_across_interval_change(self):
        h = (int(time.time()) // 3600) * 3600
        calls = [(h + 0, 100, 10), (h + 10, 100, 10), (h + 20, 200, 5), (h + 25, 200, 5)]
        with app.LOCK:
            for ts, power, interval_sec in calls:
                samples_repo.rollup_now(app.DB, ts, 0, 0, 0, power, 0, interval_sec)
            app.DB.commit()
            wsec = app.DB.execute(
                "SELECT wsec FROM samples_1h WHERE ts=?", (h,)).fetchone()[0]
        expect = sum(power * interval_sec for _, power, interval_sec in calls)
        self.assertAlmostEqual(wsec, expect, places=6)

    def test_cpu_dram_wsec_sum_per_row_energy_across_interval_change(self):
        h = (int(time.time()) // 3600) * 3600
        # (ts, cpu_power, dram_power, interval_sec)
        calls = [(h + 0, 40, 15, 10), (h + 10, 40, 15, 10), (h + 20, 60, 20, 5)]
        with app.LOCK:
            for ts, cpu_power, dram_power, interval_sec in calls:
                samples_repo.rollup_now(app.DB, ts, 0, 0, 0, 0, 0, interval_sec,
                                        cpu_power=cpu_power, dram_power=dram_power)
            app.DB.commit()
            cpu_wsec, dram_wsec = app.DB.execute(
                "SELECT cpu_wsec, dram_wsec FROM samples_1h WHERE ts=?", (h,)).fetchone()
        expect_cpu = sum(cp * i for _, cp, dp, i in calls)
        expect_dram = sum(dp * i for _, cp, dp, i in calls)
        self.assertAlmostEqual(cpu_wsec, expect_cpu, places=6)
        self.assertAlmostEqual(dram_wsec, expect_dram, places=6)


class TestHostSamplesRecordWsecAccumulation(unittest.TestCase):
    """Same guarantee as above, for host_samples.record's three per-component
    watt-second accumulators."""

    HOST = "wsec-test-host"

    def setUp(self):
        with app.LOCK:
            app.DB.execute("DELETE FROM host_samples WHERE host=?", (self.HOST,))
            app.DB.execute("DELETE FROM host_samples_1h WHERE host=?", (self.HOST,))
            app.DB.commit()

    def tearDown(self):
        with app.LOCK:
            app.DB.execute("DELETE FROM host_samples WHERE host=?", (self.HOST,))
            app.DB.execute("DELETE FROM host_samples_1h WHERE host=?", (self.HOST,))
            app.DB.commit()

    def test_gpu_wsec_sums_across_interval_change(self):
        h = (int(time.time()) // 3600) * 3600
        calls = [(h + 0, 100, 10), (h + 10, 100, 10), (h + 20, 300, 5)]
        with app.LOCK:
            for ts, gpu_power, interval_sec in calls:
                hs_repo.record(app.DB, ts, self.HOST, interval_sec, gpu_power=gpu_power)
            app.DB.commit()
            row = app.DB.execute(
                "SELECT gpu_wsec, cpu_wsec, dram_wsec FROM host_samples_1h WHERE ts=? AND host=?",
                (h, self.HOST)).fetchone()
        expect_gpu = sum(p * i for _, p, i in calls)
        self.assertAlmostEqual(row[0], expect_gpu, places=6)
        self.assertEqual(row[1], 0)     # no cpu_power ever passed -> COALESCE(...,0)*interval = 0
        self.assertEqual(row[2], 0)


class TestGpuSessionsUnchangedAfterIntervalChange(unittest.TestCase):
    """Regression for the reviewer-flagged gap: /api/sessions (backend/api/gpu.py)
    calls _app._gpu_sessions() over sessions_since() rows. A GPU-busy session
    that spans only history written at OLD_INTERVAL must not get repriced when
    app.INTERVAL later changes -- same guarantee as /api/cost, applied to the
    Experiments tab's session reconstruction."""

    OLD_INTERVAL = 10
    NEW_INTERVAL = 5
    WATTS = 200

    def setUp(self):
        _clean()
        self.now = int(time.time())
        self.orig_interval = app.INTERVAL
        self._seed_history(self.OLD_INTERVAL, self.WATTS)
        app.save_settings({"kwh_price": "0.30", "currency": "$", "tariff_mode": "single",
                           "system_idle_watts": ""})

    def tearDown(self):
        app.INTERVAL = self.orig_interval
        _clean()

    def _seed_history(self, interval_sec, watts, anchor=None):
        """A contiguous run of GPU-busy samples (util=50 >= _ACTIVE_UTIL=20) as
        the real write path would have produced them at `interval_sec`, so
        _gpu_sessions() reconstructs them as one session."""
        anchor = self.now if anchor is None else anchor
        with app.LOCK:
            n = 3600 // interval_sec
            for i in range(n):
                ts = anchor - 3600 + i * interval_sec
                app.DB.execute(
                    "INSERT OR REPLACE INTO samples(ts,util,mem_used,mem_total,power,temp,interval_sec) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (ts, 50, 0, 0, watts, 0, interval_sec))
            app.DB.commit()

    def _get(self, path):
        return app.app.test_client().get(path).get_json()

    def test_session_energy_and_cost_unchanged_after_interval_change(self):
        before = self._get("/api/sessions?range=30d")
        self.assertEqual(before["totals"]["count"], 1)
        before_session = before["sessions"][0]

        app.INTERVAL = self.NEW_INTERVAL
        after = self._get("/api/sessions?range=30d")
        after_session = after["sessions"][0]

        self.assertEqual(before_session["energy_kwh"], after_session["energy_kwh"])
        self.assertEqual(before_session["cost"], after_session["cost"])
        self.assertEqual(before["totals"]["energy_kwh"], after["totals"]["energy_kwh"])
        self.assertEqual(before["totals"]["cost"], after["totals"]["cost"])
        # sanity: nonzero, so the equality above is meaningful
        self.assertGreater(before_session["energy_kwh"], 0)

    def test_new_session_after_interval_change_uses_new_interval(self):
        """A GPU-busy session recorded entirely AFTER the interval flip, with
        the new interval_sec, must price at the NEW cadence. _gpu_sessions()
        only splits a session on IDLE rows between busy runs (gap-detection
        counts consecutive idle rows, not elapsed time), so a few explicit
        idle rows are seeded between the two busy windows to force a split."""
        app.INTERVAL = self.NEW_INTERVAL
        rows_before = samples_repo.sessions_since(self.now - 3600, conn=app.DB)
        sessions_before = app._gpu_sessions(rows_before, app.INTERVAL, price=0.30)
        self.assertEqual(len(sessions_before), 1)
        energy_before = sessions_before[0]["energy_kwh"]

        # A few idle rows (util below _ACTIVE_UTIL) to force the session split,
        # then a second, later hour of samples written entirely at NEW_INTERVAL.
        gap_start = self.now + 60
        with app.LOCK:
            for i in range(5):
                app.DB.execute(
                    "INSERT OR REPLACE INTO samples(ts,util,mem_used,mem_total,power,temp,interval_sec) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (gap_start + i * self.NEW_INTERVAL, 0, 0, 0, 0, 0, self.NEW_INTERVAL))
            app.DB.commit()
        self._seed_history(self.NEW_INTERVAL, self.WATTS, anchor=self.now + 3600 + 3600)
        rows_after = samples_repo.sessions_since(self.now - 3600, conn=app.DB)
        sessions_after = app._gpu_sessions(rows_after, app.INTERVAL, price=0.30)
        self.assertEqual(len(sessions_after), 2)
        new_session = max(sessions_after, key=lambda s: s["start"])
        # Same hour, same wattage, correctly priced at NEW_INTERVAL -- and
        # matching the OLD_INTERVAL session's energy, since each row prices
        # at its own interval_sec regardless of app.INTERVAL.
        self.assertAlmostEqual(new_session["energy_kwh"], energy_before, places=3)
        self.assertGreater(new_session["energy_kwh"], 0)


if __name__ == "__main__":
    unittest.main()
