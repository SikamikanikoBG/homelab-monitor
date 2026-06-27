"""Tests for the per-service PUBLIC status surface (opt-in, privacy-first):
the `public` opt-in flag migration + CRUD, `_public_check_detail` shape (windows,
90-day daily, downsampled latency, reconstructed incidents), the `monitors` list
on /api/status, the /api/status/<id> endpoint gating (404 unless public+enabled),
and the central privacy guard: a check whose target carries credentials and whose
results carry a leaky `err` string must expose host-ONLY (no creds, no raw target,
no raw err) — a plant-a-secret scan over the whole public payload. Network-free."""
import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


def _walk_strings(obj, path="$"):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield (path, "key", k)
            yield from _walk_strings(v, path + "." + str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk_strings(v, path + "[%d]" % i)
    else:
        yield (path, "value", obj)


def _clean_db():
    with app.LOCK:
        app.DB.execute("DELETE FROM uptime_checks")
        app.DB.execute("DELETE FROM uptime_results")
        app.DB.commit()
    app._uptime_due.clear()


def _seed_result(cid, ts, up, latency_ms=None, code=None, err=None):
    with app.LOCK:
        app.DB.execute(
            "INSERT INTO uptime_results(check_id,ts,up,latency_ms,code,err) VALUES(?,?,?,?,?,?)",
            (cid, ts, 1 if up else 0, latency_ms, code, err))
        app.DB.commit()


class TestPublicFlagMigration(unittest.TestCase):
    def test_migration_adds_public_idempotent_old_zero(self):
        # The live DB has already been migrated at import — `public` exists.
        cols = [r[1] for r in app.DB.execute("PRAGMA table_info(uptime_checks)").fetchall()]
        self.assertIn("public", cols)
        # Re-running the migration is a no-op (idempotent), never raises.
        app._apply_schema_migrations(app.DB)
        # A row inserted WITHOUT a public value defaults to 0 (= not public).
        _clean_db()
        with app.LOCK:
            app.DB.execute(
                "INSERT INTO uptime_checks(id,label,type,target,interval_sec,timeout_sec,"
                "expected_status,enabled,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                ("old", "legacy", "http", "https://x", 60, 10, None, 1, int(time.time())))
            app.DB.commit()
        row = app.DB.execute("SELECT public FROM uptime_checks WHERE id='old'").fetchone()
        self.assertEqual(int(row[0]), 0)
        checks = app.list_uptime_checks()
        self.assertFalse(checks[0]["public"])


class TestPublicFlagCrud(unittest.TestCase):
    def setUp(self):
        _clean_db()

    def test_create_defaults_private(self):
        cid, err = app.create_uptime_check(
            {"label": "s", "type": "http", "target": "https://example.com"})
        self.assertIsNone(err)
        self.assertFalse(app.list_uptime_checks()[0]["public"])

    def test_create_opt_in_public(self):
        cid, err = app.create_uptime_check(
            {"label": "s", "type": "http", "target": "https://example.com", "public": True})
        self.assertIsNone(err)
        self.assertTrue(app.list_uptime_checks()[0]["public"])

    def test_quick_public_toggle(self):
        cid, _ = app.create_uptime_check(
            {"label": "s", "type": "http", "target": "https://example.com"})
        ok, err = app.update_uptime_check(cid, {"public": True})
        self.assertTrue(ok)
        self.assertTrue(app.list_uptime_checks()[0]["public"])
        ok, _ = app.update_uptime_check(cid, {"public": False})
        self.assertTrue(ok)
        self.assertFalse(app.list_uptime_checks()[0]["public"])

    def test_full_edit_preserves_public_when_absent(self):
        cid, _ = app.create_uptime_check(
            {"label": "s", "type": "http", "target": "https://example.com", "public": True})
        # A full config edit that omits `public` must NOT silently un-publish.
        ok, err = app.update_uptime_check(
            cid, {"label": "s2", "type": "http", "target": "https://example.com",
                  "interval_sec": 90, "timeout_sec": 10})
        self.assertTrue(ok, err)
        c = app.list_uptime_checks()[0]
        self.assertEqual(c["label"], "s2")
        self.assertTrue(c["public"])


class TestPublicHost(unittest.TestCase):
    def test_http_strips_credentials_scheme_path(self):
        h = app._public_host({"type": "http",
                              "target": "https://admin:hunter2@vault.lan:8443/secret?x=1"})
        self.assertEqual(h, "vault.lan:8443")     # host:nondefault-port only
        self.assertNotIn("admin", h)
        self.assertNotIn("hunter2", h)
        self.assertNotIn("/secret", h)

    def test_http_default_port_hidden(self):
        self.assertEqual(app._public_host({"type": "http", "target": "https://x.lan/y"}), "x.lan")
        self.assertEqual(app._public_host({"type": "http", "target": "http://x.lan:80/y"}), "x.lan")

    def test_tcp_and_cert_host(self):
        self.assertEqual(app._public_host({"type": "tcp", "target": "db.lan:5432"}), "db.lan:5432")
        self.assertEqual(app._public_host({"type": "cert", "target": "example.com"}), "example.com")
        self.assertEqual(app._public_host({"type": "cert", "target": "example.com:8443"}), "example.com:8443")

    def test_garbage_target_yields_empty_not_raw(self):
        self.assertEqual(app._public_host({"type": "http", "target": "::::not a url"}), "")


class TestPublicCheckDetail(unittest.TestCase):
    def setUp(self):
        _clean_db()
        self.now = int(time.time())
        self.cid, _ = app.create_uptime_check(
            {"label": "PROD API", "type": "http",
             "target": "https://example.com/health", "public": True})

    def test_non_public_404s(self):
        app.update_uptime_check(self.cid, {"public": False})
        self.assertIsNone(app._public_check_detail(self.cid, self.now))

    def test_disabled_404s(self):
        app.update_uptime_check(self.cid, {"enabled": False})
        self.assertIsNone(app._public_check_detail(self.cid, self.now))

    def test_unknown_id_404s(self):
        self.assertIsNone(app._public_check_detail("nope", self.now))

    def test_shape_windows_daily_latency(self):
        # 200 up samples across ~3 days, every ~20 min, with latency.
        n = 200
        for i in range(n):
            ts = self.now - (n - i) * 1200
            _seed_result(self.cid, ts, True, latency_ms=50 + (i % 30), code=200)
        d = app._public_check_detail(self.cid, self.now)
        self.assertIsNotNone(d)
        self.assertEqual(d["label"], "PROD API")
        self.assertEqual(d["host"], "example.com")     # default 443 hidden
        self.assertEqual(d["state"], "up")
        self.assertEqual(set(d["uptime"].keys()), {"24h", "7d", "30d", "90d"})
        for v in d["uptime"].values():
            self.assertTrue(v is None or 0 <= v <= 100)
        self.assertEqual(d["uptime"]["90d"], 100.0)
        self.assertLessEqual(len(d["daily"]), 90)
        for cell in d["daily"]:
            self.assertEqual(set(cell.keys()), {"d", "up", "s"})
        # latency downsampled to <= 120 points
        self.assertLessEqual(len(d["response_series"]), app._PUBLIC_LATENCY_PTS)
        self.assertTrue(d["response_series"])
        for p in d["response_series"]:
            self.assertEqual(set(p.keys()), {"t", "ms"})

    def test_incident_reconstruction(self):
        # up, up, DOWN, DOWN, up, up  → one closed incident; then trailing DOWN.
        base = self.now - 6000
        seq = [(0, True), (600, True), (1200, False), (1800, False),
               (2400, True), (3000, True), (3600, False), (4200, False)]
        for off, up in seq:
            _seed_result(self.cid, base + off, up, latency_ms=10 if up else None,
                         code=200 if up else 503,
                         err=None if up else "connect to 10.0.0.9:443 failed: secret-path")
        d = app._public_check_detail(self.cid, self.now)
        incs = d["incidents"]
        self.assertGreaterEqual(len(incs), 2)
        # newest first
        self.assertIsNone(incs[0]["end"])             # trailing down = ongoing
        self.assertEqual(incs[0]["reason"], "Down")
        self.assertEqual(incs[0]["code"], 503)
        closed = incs[1]
        self.assertIsNotNone(closed["end"])
        self.assertEqual(closed["duration_sec"], (base + 2400) - (base + 1200))
        # PRIVACY: no incident carries an err / target / raw fields.
        for inc in incs:
            self.assertEqual(set(inc.keys()), {"start", "end", "duration_sec", "code", "reason"})
            self.assertEqual(inc["reason"], "Down")

    def test_up_since(self):
        for i in range(5):
            _seed_result(self.cid, self.now - (5 - i) * 600, True, latency_ms=10, code=200)
        d = app._public_check_detail(self.cid, self.now)
        self.assertEqual(d["up_since"], self.now - 5 * 600)


class TestPublicPrivacyAdversarial(unittest.TestCase):
    """Plant credentials in the target AND a leaky err in the results; the entire
    public surface (index monitors + detail endpoint) must expose host-only —
    NEVER the raw target, NEVER credentials, NEVER the raw err string."""
    SECRETS = ("admin", "hunter2", "s3cr3t", "/internal/secret/path",
               "10.0.0.99", "connection refused by internal-db")

    def setUp(self):
        _clean_db()
        self.c = app.app.test_client()
        self._sp = app.STATUS_PAGE
        app.STATUS_PAGE = True
        self.now = int(time.time())
        self.cid, _ = app.create_uptime_check(
            {"label": "Vault", "type": "http",
             "target": "https://admin:hunter2@vault.lan:8443/internal/secret/path?token=s3cr3t",
             "public": True})
        # Down results with an err that embeds internal host/path/creds.
        for i in range(6):
            up = i < 2
            _seed_result(self.cid, self.now - (6 - i) * 600, up,
                         latency_ms=12 if up else None, code=200 if up else 502,
                         err=None if up else
                         "GET https://admin:hunter2@10.0.0.99/internal/secret/path "
                         "failed: connection refused by internal-db")

    def tearDown(self):
        app.STATUS_PAGE = self._sp

    def _scan(self, blob, where):
        for needle in self.SECRETS:
            self.assertNotIn(needle, blob, "%s leaked secret %r" % (where, needle))

    def test_detail_endpoint_host_only_no_leak(self):
        r = self.c.get("/api/status/" + self.cid)
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertEqual(j["host"], "vault.lan:8443")
        self.assertEqual(j["label"], "Vault")
        # no raw-target / err / internals keys anywhere
        present = {k for (_p, kind, k) in _walk_strings(j) if kind == "key"}
        for forbidden in ("target", "err", "last_err", "url", "password", "token"):
            self.assertNotIn(forbidden, present)
        self._scan(json.dumps(j), "detail endpoint")

    def test_index_monitors_host_only_no_leak(self):
        j = self.c.get("/api/status").get_json()
        self.assertIn("monitors", j)
        self.assertEqual(len(j["monitors"]), 1)
        m = j["monitors"][0]
        self.assertEqual(m["host"], "vault.lan:8443")
        present = {k for (_p, kind, k) in _walk_strings(m) if kind == "key"}
        for forbidden in ("target", "err", "url"):
            self.assertNotIn(forbidden, present)
        self._scan(json.dumps(j), "index monitors")

    def test_detail_404_when_private(self):
        app.update_uptime_check(self.cid, {"public": False})
        self.assertEqual(self.c.get("/api/status/" + self.cid).status_code, 404)
        # and the index no longer lists it
        j = self.c.get("/api/status").get_json()
        self.assertEqual(j["monitors"], [])

    def test_monitors_only_public_and_enabled(self):
        # add a private check + a disabled-public check; neither should appear.
        app.create_uptime_check({"label": "priv", "type": "http", "target": "https://p.lan"})
        cid2, _ = app.create_uptime_check(
            {"label": "disabled-pub", "type": "http", "target": "https://d.lan", "public": True})
        app.update_uptime_check(cid2, {"enabled": False})
        j = self.c.get("/api/status").get_json()
        labels = [m["label"] for m in j["monitors"]]
        self.assertEqual(labels, ["Vault"])


class TestBuildPublicStatusStillClean(unittest.TestCase):
    """Regression: build_public_status's existing aggregate payload still carries
    NO target/detail/err — the only addition is the (default-empty) monitors list."""
    def setUp(self):
        _clean_db()

    def test_no_monitors_when_nothing_opted_in(self):
        app.create_uptime_check({"label": "p", "type": "http", "target": "https://x.lan"})
        out = app.build_public_status()
        self.assertIn("monitors", out)
        self.assertEqual(out["monitors"], [])
        # the rest of the aggregate payload is unchanged + secret-free of targets.
        blob = json.dumps(out)
        self.assertNotIn("x.lan", blob)
        present = {k for (_p, kind, k) in _walk_strings(out) if kind == "key"}
        for forbidden in ("target", "err", "detail", "metric"):
            self.assertNotIn(forbidden, present)


if __name__ == "__main__":
    unittest.main()
