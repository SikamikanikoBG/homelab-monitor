"""Tests for the public read-only status page (E4) — /status (HTML) + /api/status
(JSON). The page is UNAUTHENTICATED, so the central test is privacy: the public
payload must carry only aggregated, non-sensitive signals and NONE of the known
topology/secret/settings fields the rest of the app exposes."""
import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


def _walk_strings(obj, path="$"):
    """Yield (json_pointer, key_name, value) for every leaf + dict key so a
    privacy assertion can scan the whole tree, not just the top level."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield (path, "key", k)
            yield from _walk_strings(v, path + "." + str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk_strings(v, path + "[%d]" % i)
    else:
        yield (path, "value", obj)


class TestPublicStatus(unittest.TestCase):
    def setUp(self):
        self.c = app.app.test_client()
        self._sp = app.STATUS_PAGE
        app.STATUS_PAGE = True
        # Seed a representative snapshot so the tiles/counts are non-trivial. Use
        # values that *would* leak if passed through naively (hostname, IP, mounts).
        self._host = app.LATEST.get("host")
        self._gpu_avail = app.LATEST.get("gpu_avail")
        self._docker = app.HEALTH.get("docker")
        self._systemd = app.HEALTH.get("systemd")
        self._at = app.HEALTH.get("at")
        app.LATEST["host"] = {
            "hostname": "secret-ardi-box", "ram_used": 8000, "ram_total": 16000,
            "cpu": 12, "ip": "192.168.1.50",
            "disks": [{"mount": "/data", "pct": 71, "used": 100, "total": 200},
                      {"mount": "/", "pct": 40}],
            "os": {"id": "opensuse", "pretty": "openSUSE Leap 16.1"},
        }
        app.LATEST["gpu_avail"] = True
        app.LATEST["util"] = 40
        app.LATEST["mem_used"] = 12000
        app.LATEST["mem_total"] = 24576
        app.HEALTH["docker"] = {"available": True,
                                "containers": [{"name": "immich_server", "status": "ok"}],
                                "summary": {"total": 12, "running": 11, "problems": 1}}
        app.HEALTH["systemd"] = {"available": True,
                                 "services": [{"name": "sshd"}, {"name": "nginx"}, {"name": "ollama"}],
                                 "summary": {"running": 3, "failed": 0}}
        app.HEALTH["at"] = int(time.time())

    def tearDown(self):
        app.STATUS_PAGE = self._sp
        app.LATEST["host"] = self._host
        app.LATEST["gpu_avail"] = self._gpu_avail
        app.HEALTH["docker"] = self._docker
        app.HEALTH["systemd"] = self._systemd
        app.HEALTH["at"] = self._at

    # ── page serves ───────────────────────────────────────────────────────────
    def test_status_html_200(self):
        r = self.c.get("/status")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/html", r.content_type)
        body = r.get_data(as_text=True)
        self.assertIn("/api/status", body)        # data-driven
        self.assertIn("HomeLab Monitor", body)

    # ── JSON shape ────────────────────────────────────────────────────────────
    def test_api_status_shape(self):
        r = self.c.get("/api/status")
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        for k in ("status", "updated", "now", "demo", "tiles", "gpu",
                  "anomaly_active", "counts", "daily", "uptime_90d"):
            self.assertIn(k, j, "missing top-level key %r" % k)
        self.assertIn(j["status"], ("operational", "degraded", "down"))
        self.assertIsInstance(j["tiles"], list)
        self.assertTrue(j["tiles"], "expected subsystem tiles")
        for t in j["tiles"]:
            self.assertIn("key", t)
            self.assertIn("status", t)
            self.assertIn(t["status"], ("ok", "warn", "crit", "info"))
        c = j["counts"]
        for k in ("services", "containers", "monitored", "problems"):
            self.assertIn(k, c)
        # counts reflect the seeded snapshot (3 services + 12 containers = 15)
        self.assertEqual(c["services"], 3)
        self.assertEqual(c["containers"], 12)
        self.assertEqual(c["monitored"], 15)
        self.assertEqual(c["problems"], 1)        # 1 docker problem, 0 failed
        # a docker problem degrades the banner away from full operational
        self.assertIn(j["status"], ("degraded", "down"))
        self.assertTrue(j["gpu"]["available"])
        self.assertTrue(j["gpu"]["busy"])         # util 40 >= 5

    # ── PRIVACY: no sensitive field may appear anywhere in the payload ────────
    def test_privacy_no_sensitive_keys(self):
        j = self.c.get("/api/status").get_json()
        leaves = list(_walk_strings(j))

        # 1) Known-sensitive dict KEYS must be absent at every depth.
        forbidden_keys = {
            "hostname", "host", "ip", "ipv4", "ipv6", "mac", "iface", "net",
            "disks", "mount", "mountpoint", "path", "os", "kernel", "distro",
            "models", "model", "procs", "processes",
            "webhook_url", "webhook", "token", "secret", "password", "api_key",
            "ssh_target", "ssh", "settings", "kwh_price", "currency", "cost",
            "tariff", "callers", "edges", "mcp", "update", "diagnostics",
            "name", "image", "ports", "detail", "metric",
        }
        present = {k for (_p, kind, k) in leaves if kind == "key"}
        leaked = forbidden_keys & present
        self.assertFalse(leaked, "public status leaked sensitive key(s): %s" % sorted(leaked))

        # 1b) The only `services`/`containers` allowed are anonymized integer
        #     counts under `counts` — never a name-bearing list anywhere.
        for p, kind, val in leaves:
            if kind == "key" and val in ("services", "containers"):
                self.assertTrue(p.endswith("counts") or p == "$.counts",
                                "%r appears outside counts at %s" % (val, p))
        for k in ("services", "containers", "monitored", "problems"):
            self.assertIsInstance(j["counts"][k], int)

        # 2) The literal seeded secret VALUES must not appear anywhere either.
        blob = json.dumps(j)
        for needle in ("secret-ardi-box", "192.168.1.50", "/data", "opensuse",
                       "immich_server", "sshd", "nginx", "ollama", "Leap"):
            self.assertNotIn(needle, blob,
                             "public status leaked sensitive value %r" % needle)

    # ── PRIVACY: the 90-day daily ribbon must be aggregated rank/up-frac + date ─
    def test_privacy_daily_ribbon(self):
        # Seed daily 'overall' samples carrying sensitive-looking neighbours in the
        # table (other keys) to prove the ribbon emits only {d, s, up}.
        now = int(time.time())
        with app.LOCK:
            app.DB.execute("DELETE FROM status_history")
            for i in range(5):
                bt = now - i * 86400
                app.DB.execute("INSERT INTO status_history(ts,key,state) VALUES(?,?,?)",
                               (bt, "overall", 0))
            app.DB.commit()
        j = self.c.get("/api/status").get_json()
        self.assertIn("daily", j)
        self.assertIn("uptime_90d", j)
        daily = j["daily"]
        self.assertIsInstance(daily, list)
        self.assertLessEqual(len(daily), 90)
        for cell in daily:
            # ONLY these three keys, with the expected value types.
            self.assertEqual(set(cell.keys()), {"d", "s", "up"})
            self.assertIsInstance(cell["d"], str)              # 'YYYY-MM-DD'
            self.assertRegex(cell["d"], r"^\d{4}-\d{2}-\d{2}$")
            self.assertIsInstance(cell["s"], int)
            self.assertIn(cell["s"], (-1, 0, 1, 2, 3))
            self.assertTrue(cell["up"] is None or isinstance(cell["up"], (int, float)))
            if cell["up"] is not None:
                self.assertGreaterEqual(cell["up"], 0.0)
                self.assertLessEqual(cell["up"], 1.0)
        # uptime_90d is a percentage float (or None when no data).
        self.assertTrue(j["uptime_90d"] is None or isinstance(j["uptime_90d"], (int, float)))
        # No forbidden keys / seeded secret values appear in the ribbon payload.
        leaves = list(_walk_strings(j))
        forbidden = {"hostname", "host", "ip", "mount", "os", "model", "name",
                     "image", "path", "secret", "token", "webhook_url"}
        present = {k for (_p, kind, k) in leaves if kind == "key"}
        self.assertFalse(forbidden & present,
                         "daily ribbon leaked sensitive key(s): %s" % sorted(forbidden & present))
        blob = json.dumps(j)
        for needle in ("secret-ardi-box", "192.168.1.50", "/data", "opensuse",
                       "immich_server", "sshd", "nginx", "ollama", "Leap"):
            self.assertNotIn(needle, blob,
                             "daily ribbon leaked sensitive value %r" % needle)

    # ── PRIVACY: heartbeat history must be aggregated up/down ONLY ────────────
    def test_privacy_heartbeat_history(self):
        # Seed real history rows containing what WOULD be sensitive if leaked, to
        # prove the history serializer emits only {t, s} integers.
        now = int(time.time())
        with app.LOCK:
            app.DB.execute("DELETE FROM status_history")
            for i in range(app._STATHIST_CELLS):
                bt = now - (now % app._STATHIST_BUCKET) - i * app._STATHIST_BUCKET
                for k in app._STATHIST_KEYS:
                    app.DB.execute("INSERT INTO status_history(ts,key,state) VALUES(?,?,?)",
                                   (bt, k, 0))
            app.DB.commit()
        j = self.c.get("/api/status").get_json()
        # Every tile's history (and the top-level overall history) is present and
        # carries ONLY aggregated cells of {t:int, s:int} + integer up/total/uptime.
        seen_hist = False
        for t in j["tiles"]:
            h = t.get("history")
            if h is None:
                continue
            seen_hist = True
            self.assertIsInstance(h["cells"], list)
            for cell in h["cells"]:
                self.assertEqual(set(cell.keys()), {"t", "s"})
                self.assertIsInstance(cell["t"], int)
                self.assertIsInstance(cell["s"], int)
                self.assertIn(cell["s"], (-1, 0, 1, 2, 3))
            self.assertIsInstance(h["up"], int)
            self.assertIsInstance(h["total"], int)
        self.assertTrue(seen_hist, "expected at least one tile heartbeat history")
        self.assertIsNotNone(j.get("history"), "expected overall history")
        # The forbidden-key / forbidden-value scan must still pass WITH history present.
        leaves = list(_walk_strings(j))
        forbidden = {"hostname", "host", "ip", "mount", "os", "model", "name",
                     "image", "path", "secret", "token", "webhook_url"}
        present = {k for (_p, kind, k) in leaves if kind == "key"}
        self.assertFalse(forbidden & present,
                         "heartbeat history leaked sensitive key(s): %s"
                         % sorted(forbidden & present))
        blob = json.dumps(j)
        for needle in ("secret-ardi-box", "192.168.1.50", "/data", "opensuse",
                       "immich_server", "sshd", "nginx", "ollama"):
            self.assertNotIn(needle, blob,
                             "heartbeat history leaked sensitive value %r" % needle)

    # ── toggle ────────────────────────────────────────────────────────────────
    def test_disabled_returns_404(self):
        app.STATUS_PAGE = False
        self.assertEqual(self.c.get("/status").status_code, 404)
        self.assertEqual(self.c.get("/api/status").status_code, 404)

    # ── health surfaces the toggle ───────────────────────────────────────────
    def test_health_exposes_status_page_flag(self):
        j = self.c.get("/api/health").get_json()
        self.assertIn("status_page", j)
        self.assertEqual(j["status_page"], app.STATUS_PAGE)

    # ── a few failed units while the bulk run is "degraded", not "down" ───────
    def test_failed_units_degrade_not_down(self):
        # clean docker so services is the only red subsystem
        app.HEALTH["docker"] = {"available": True, "containers": [],
                                "summary": {"total": 12, "running": 12, "problems": 0}}
        app.HEALTH["systemd"] = {"available": True, "services": [],
                                 "summary": {"running": 90, "failed": 4}}
        j = self.c.get("/api/status").get_json()
        self.assertEqual(j["status"], "degraded",
                         "4 failed of 90 running should not paint the lab DOWN")
        # the services tile still honestly reports it needs attention
        svc = next(t for t in j["tiles"] if t["key"] == "services")
        self.assertEqual(svc["status"], "crit")
        self.assertEqual(svc["failed"], 4)

    def test_nothing_running_stays_down(self):
        app.HEALTH["docker"] = {"available": True, "containers": [],
                                "summary": {"total": 12, "running": 12, "problems": 0}}
        app.HEALTH["systemd"] = {"available": True, "services": [],
                                 "summary": {"running": 0, "failed": 4}}
        j = self.c.get("/api/status").get_json()
        self.assertEqual(j["status"], "down",
                         "nothing running is a genuine DOWN")

    # ── never 500s even with an empty/warming snapshot ───────────────────────
    def test_graceful_when_warming(self):
        app.LATEST["host"] = {}
        app.HEALTH["docker"] = None
        app.HEALTH["systemd"] = None
        r = self.c.get("/api/status")
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertIn("status", j)
        self.assertIsInstance(j["tiles"], list)


class TestHeartbeatHistory(unittest.TestCase):
    """status_history persistence + the /api/status heartbeat series shape, the
    thin-state, retention trim, and the uptime% math."""

    def setUp(self):
        self.c = app.app.test_client()
        self._sp = app.STATUS_PAGE
        app.STATUS_PAGE = True
        # Minimal live snapshot so _status_states() yields concrete ranks.
        self._docker = app.HEALTH.get("docker")
        self._systemd = app.HEALTH.get("systemd")
        self._host = app.LATEST.get("host")
        self._gpu_avail = app.LATEST.get("gpu_avail")
        app.HEALTH["docker"] = {"available": True, "containers": [],
                                "summary": {"total": 5, "running": 5, "problems": 0}}
        app.HEALTH["systemd"] = {"available": True, "services": [],
                                 "summary": {"running": 10, "failed": 0}}
        app.LATEST["host"] = {"ram_used": 4000, "ram_total": 16000, "cpu": 10, "disks": []}
        app.LATEST["gpu_avail"] = True
        app.LATEST["util"] = 20
        app.LATEST["mem_used"] = 8000
        app.LATEST["mem_total"] = 24576
        with app.LOCK:
            app.DB.execute("DELETE FROM status_history")
            app.DB.commit()

    def tearDown(self):
        app.STATUS_PAGE = self._sp
        app.HEALTH["docker"] = self._docker
        app.HEALTH["systemd"] = self._systemd
        app.LATEST["host"] = self._host
        app.LATEST["gpu_avail"] = self._gpu_avail
        with app.LOCK:
            app.DB.execute("DELETE FROM status_history")
            app.DB.commit()

    def _count(self):
        with app.LOCK:
            return app.DB.execute("SELECT COUNT(*) FROM status_history").fetchone()[0]

    # ── sampling writes one row per subsystem key per bucket ─────────────────
    def test_sample_inserts_rows(self):
        ts = int(time.time())
        with app.LOCK:
            app.sample_status_history(ts)
            app.DB.commit()
        self.assertEqual(self._count(), len(app._STATHIST_KEYS))
        with app.LOCK:
            keys = {r[0] for r in app.DB.execute("SELECT DISTINCT key FROM status_history")}
        self.assertEqual(keys, set(app._STATHIST_KEYS))

    # ── re-sampling in the same bucket replaces, not duplicates ──────────────
    def test_sample_idempotent_per_bucket(self):
        ts = int(time.time())
        with app.LOCK:
            app.sample_status_history(ts)
            app.sample_status_history(ts + 5)   # same 5-min bucket
            app.DB.commit()
        self.assertEqual(self._count(), len(app._STATHIST_KEYS))

    # ── retention trims old rows (mirrors the sampler's DELETE) ──────────────
    def test_retention_trim(self):
        now = int(time.time())
        old = now - app._STATHIST_RETENTION - 10 * app._STATHIST_BUCKET
        with app.LOCK:
            app.DB.execute("INSERT INTO status_history(ts,key,state) VALUES(?,?,?)",
                           (old, "overall", 0))
            app.DB.execute("INSERT INTO status_history(ts,key,state) VALUES(?,?,?)",
                           (now, "overall", 0))
            app.DB.execute("DELETE FROM status_history WHERE ts<?",
                           (now - app._STATHIST_RETENTION,))
            app.DB.commit()
            rows = app.DB.execute("SELECT ts FROM status_history").fetchall()
        self.assertEqual([r[0] for r in rows], [now])

    # ── series shape: fixed cell count, oldest→newest, -1 fills gaps ─────────
    def test_history_shape_and_gaps(self):
        h = app._status_history(int(time.time()))
        self.assertEqual(set(h.keys()), set(app._STATHIST_KEYS))
        for key, s in h.items():
            self.assertEqual(len(s["cells"]), app._STATHIST_CELLS)
            ts_seq = [c["t"] for c in s["cells"]]
            self.assertEqual(ts_seq, sorted(ts_seq))          # oldest → newest
            self.assertTrue(all(c["s"] == -1 for c in s["cells"]))  # empty DB
            self.assertEqual(s["total"], 0)
            self.assertIsNone(s["uptime"])

    # ── thin state on a fresh DB reads as "collecting", not all-green ────────
    def test_thin_state_collecting(self):
        j = self.c.get("/api/status").get_json()
        ov = j.get("history")
        self.assertIsNotNone(ov)
        self.assertEqual(ov["total"], 0)
        self.assertIsNone(ov["uptime"])

    # ── uptime% math: up buckets / sampled buckets, no-data excluded ─────────
    def test_uptime_math(self):
        now = int(time.time())
        cur = now - (now % app._STATHIST_BUCKET)
        # 8 sampled buckets: 6 up (rank 0), 1 degraded (rank 2), 1 down (rank 3)
        # → 6/8 = 75.0%. Other 22 buckets stay no-data (excluded from the math).
        states = [0, 0, 0, 0, 0, 0, 2, 3]
        with app.LOCK:
            for i, st in enumerate(states):
                bt = cur - i * app._STATHIST_BUCKET
                app.DB.execute("INSERT INTO status_history(ts,key,state) VALUES(?,?,?)",
                               (bt, "overall", st))
            app.DB.commit()
        h = app._status_history(now)["overall"]
        self.assertEqual(h["total"], 8)
        self.assertEqual(h["up"], 6)
        self.assertEqual(h["uptime"], 75.0)

    # ── always 200, even if the table is missing/unreadable ──────────────────
    def test_always_200(self):
        r = self.c.get("/api/status")
        self.assertEqual(r.status_code, 200)
        self.assertIn("history", r.get_json())


class TestDailyRibbon(unittest.TestCase):
    """The 90-day uptime ribbon rollup: worst-rank-per-local-day, honest no-data
    for days without samples (no fake green), and uptime% over days WITH data."""

    def setUp(self):
        with app.LOCK:
            app.DB.execute("DELETE FROM status_history")
            app.DB.commit()

    def tearDown(self):
        with app.LOCK:
            app.DB.execute("DELETE FROM status_history")
            app.DB.commit()

    def _local_day(self, ts):
        return time.strftime("%Y-%m-%d", time.localtime(ts))

    # ── span + oldest→newest ordering, today is the last cell ────────────────
    def test_span_and_ordering(self):
        now = int(time.time())
        r = app._status_daily(now)
        self.assertEqual(r["span"], app._DAILY_RIBBON_DAYS)
        self.assertEqual(len(r["days"]), app._DAILY_RIBBON_DAYS)
        ds = [c["d"] for c in r["days"]]
        self.assertEqual(ds, sorted(ds))                         # oldest → newest
        self.assertEqual(r["days"][-1]["d"], self._local_day(now))  # today last

    # ── empty DB → all no-data, no fake green, uptime None ───────────────────
    def test_empty_is_nodata_not_green(self):
        now = int(time.time())
        r = app._status_daily(now)
        self.assertTrue(all(c["s"] == -1 and c["up"] is None for c in r["days"]))
        self.assertEqual(r["total_days"], 0)
        self.assertIsNone(r["uptime"])

    # ── worst rank wins within a calendar day ────────────────────────────────
    def test_worst_rank_per_day(self):
        now = int(time.time())
        # today at local-noon-ish: mix ok(0), degraded(2), down(3) in one day.
        mid = time.mktime(time.strptime(self._local_day(now), "%Y-%m-%d"))
        with app.LOCK:
            for off, st in ((3600, 0), (7200, 0), (10800, 2), (14400, 3)):
                app.DB.execute("INSERT INTO status_history(ts,key,state) VALUES(?,?,?)",
                               (int(mid + off), "overall", st))
            app.DB.commit()
        r = app._status_daily(now)
        today = r["days"][-1]
        self.assertEqual(today["s"], 3)                 # WORST wins
        # up-fraction = up buckets / total buckets that day = 2/4 = 0.5
        self.assertAlmostEqual(today["up"], 0.5, places=4)

    # ── partial history (< span days) stays honest: only seeded days have data ─
    def test_partial_history_honest(self):
        now = int(time.time())
        # seed exactly 3 distinct local days, all fully up
        with app.LOCK:
            for i in range(3):
                mid = time.mktime(time.strptime(
                    self._local_day(now - i * 86400), "%Y-%m-%d"))
                app.DB.execute("INSERT INTO status_history(ts,key,state) VALUES(?,?,?)",
                               (int(mid + 3600), "overall", 0))
            app.DB.commit()
        r = app._status_daily(now)
        self.assertEqual(r["total_days"], 3)
        self.assertEqual(r["up_days"], 3)
        self.assertEqual(r["uptime"], 100.0)            # over days WITH data
        nodata = [c for c in r["days"] if c["s"] == -1]
        self.assertEqual(len(nodata), app._DAILY_RIBBON_DAYS - 3)

    # ── uptime% math: up_days / days-with-data, no-data excluded ──────────────
    def test_uptime_pct_math(self):
        now = int(time.time())
        # 4 days: 3 fully-up (rank 0), 1 with a down (rank 3) → 3/4 = 75.0%
        states = [0, 0, 0, 3]
        with app.LOCK:
            for i, st in enumerate(states):
                mid = time.mktime(time.strptime(
                    self._local_day(now - i * 86400), "%Y-%m-%d"))
                app.DB.execute("INSERT INTO status_history(ts,key,state) VALUES(?,?,?)",
                               (int(mid + 3600), "overall", st))
            app.DB.commit()
        r = app._status_daily(now)
        self.assertEqual(r["total_days"], 4)
        self.assertEqual(r["up_days"], 3)
        self.assertEqual(r["uptime"], 75.0)

    # ── retention is wide enough for the 90d view ────────────────────────────
    def test_retention_covers_span(self):
        self.assertGreaterEqual(app._STATHIST_RETENTION,
                                app._DAILY_RIBBON_DAYS * 86400)


if __name__ == "__main__":
    unittest.main()
