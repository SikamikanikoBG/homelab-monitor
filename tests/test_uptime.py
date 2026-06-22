"""Unit tests for the external uptime checks (HTTP/TCP endpoint monitors) and the
per-check SMART alerting that `next` layers on top: CRUD + validation, HTTP/TCP
up/down via mocks, timeout -> down, uptime% over a window, retention cap, scheduler
due-logic + hang isolation, the always-200/clean-400 API contract, and the smart
alert deltas — confirm-after threshold (anti-flap), recovery with downtime, latency
warning, per-check opt-out, and credential redaction in the cockpit insight feed.
All network is mocked — NO real outbound in CI."""
import os
import sys
import time
import socket
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


def _clean_db():
    with app.LOCK:
        app.DB.execute("DELETE FROM uptime_checks")
        app.DB.execute("DELETE FROM uptime_results")
        app.DB.commit()
    app._uptime_due.clear()
    app._uptime_down_since.clear()
    app._NOTIFIED.clear()


def _insert_results(cid, rows):
    """rows = list of (ts, up[, latency_ms]). Bulk-insert raw probe results."""
    out = []
    for r in rows:
        lat = r[2] if len(r) > 2 else 5.0
        out.append((cid, r[0], r[1], lat, None, (None if r[1] else "boom")))
    with app.LOCK:
        app.DB.executemany(
            "INSERT INTO uptime_results(check_id,ts,up,latency_ms,code,err) VALUES(?,?,?,?,?,?)", out)
        app.DB.commit()


class TestUptimeCrud(unittest.TestCase):
    def setUp(self):
        _clean_db()

    def test_create_http_validates_and_persists(self):
        cid, err = app.create_uptime_check(
            {"label": "site", "type": "http", "target": "https://example.com", "interval_sec": 60})
        self.assertIsNone(err)
        checks = app.list_uptime_checks()
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0]["id"], cid)
        self.assertTrue(checks[0]["enabled"])
        self.assertTrue(checks[0]["alerts_enabled"])      # alerts default ON
        self.assertEqual(checks[0]["fail_threshold"], 2)  # anti-flap default

    def test_create_tcp(self):
        cid, err = app.create_uptime_check({"label": "db", "type": "tcp", "target": "db.lan:5432"})
        self.assertIsNone(err)
        self.assertEqual(app.list_uptime_checks()[0]["target"], "db.lan:5432")

    def test_reject_missing_label(self):
        _, err = app.create_uptime_check({"type": "http", "target": "https://x"})
        self.assertIn("label", err.lower())

    def test_reject_bad_scheme(self):
        _, err = app.create_uptime_check({"label": "x", "type": "http", "target": "ftp://example.com"})
        self.assertIn("http", err.lower())

    def test_reject_garbage_http_target(self):
        _, err = app.create_uptime_check({"label": "x", "type": "http", "target": "not a url"})
        self.assertIsNotNone(err)

    def test_reject_garbage_tcp_target(self):
        for bad in ("noport", "host:notaport", "host:0", "host:99999", ":5432"):
            _, err = app.create_uptime_check({"label": "x", "type": "tcp", "target": bad})
            self.assertIsNotNone(err, "should reject tcp target %r" % bad)

    def test_reject_bad_type(self):
        _, err = app.create_uptime_check({"label": "x", "type": "icmp", "target": "host:1"})
        self.assertIn("http", err.lower())

    def test_reject_bad_interval(self):
        _, err = app.create_uptime_check(
            {"label": "x", "type": "http", "target": "https://x", "interval_sec": 5})
        self.assertIn("least", err.lower())
        _, err = app.create_uptime_check(
            {"label": "x", "type": "http", "target": "https://x", "interval_sec": "abc"})
        self.assertIsNotNone(err)

    def test_reject_bad_timeout(self):
        _, err = app.create_uptime_check(
            {"label": "x", "type": "http", "target": "https://x", "timeout_sec": 999})
        self.assertIn("most", err.lower())

    def test_expected_status_validation(self):
        _, err = app.create_uptime_check(
            {"label": "x", "type": "http", "target": "https://x", "expected_status": 200})
        self.assertIsNone(err)
        _, err = app.create_uptime_check(
            {"label": "x", "type": "http", "target": "https://x", "expected_status": 9999})
        self.assertIsNotNone(err)

    def test_fail_threshold_clamped(self):
        cid, err = app.create_uptime_check(
            {"label": "x", "type": "http", "target": "https://x", "fail_threshold": 99})
        self.assertIsNone(err)
        self.assertEqual(app.list_uptime_checks()[0]["fail_threshold"], app._UPTIME_FAIL_MAX)

    def test_latency_warn_validation(self):
        cid, err = app.create_uptime_check(
            {"label": "x", "type": "http", "target": "https://x", "latency_warn_ms": 800})
        self.assertIsNone(err)
        self.assertEqual(app.list_uptime_checks()[0]["latency_warn_ms"], 800)
        _, err = app.create_uptime_check(
            {"label": "y", "type": "http", "target": "https://y", "latency_warn_ms": "slow"})
        self.assertIsNotNone(err)

    def test_update_toggle_edit_and_delete(self):
        cid, _ = app.create_uptime_check({"label": "x", "type": "http", "target": "https://x"})
        ok, _ = app.update_uptime_check(cid, {"enabled": False})
        self.assertTrue(ok)
        self.assertFalse(app.list_uptime_checks()[0]["enabled"])
        ok, _ = app.update_uptime_check(
            cid, {"label": "y", "type": "http", "target": "https://y", "alerts_enabled": False})
        self.assertTrue(ok)
        row = app.list_uptime_checks()[0]
        self.assertEqual(row["label"], "y")
        self.assertFalse(row["alerts_enabled"])
        self.assertTrue(app.delete_uptime_check(cid))
        self.assertEqual(app.list_uptime_checks(), [])

    def test_update_missing_is_404(self):
        ok, err = app.update_uptime_check("nope", {"enabled": True})
        self.assertFalse(ok)
        self.assertEqual(err, "not found")

    def test_delete_missing_is_false(self):
        self.assertFalse(app.delete_uptime_check("nope"))


class TestHostPortParse(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(app._parse_host_port("db.lan:5432"), ("db.lan", 5432))
        self.assertEqual(app._parse_host_port("tcp://h:22"), ("h", 22))
        self.assertEqual(app._parse_host_port("[::1]:80"), ("::1", 80))

    def test_invalid(self):
        for bad in ("nohost", "host:0", "host:70000", ":5432", "host:abc"):
            self.assertEqual(app._parse_host_port(bad)[0], None, bad)


class TestRedact(unittest.TestCase):
    def test_strips_credentials(self):
        out = app._redact_target("https://user:pass@example.com/x")
        self.assertNotIn("pass", out)
        self.assertNotIn("user", out)
        self.assertIn("example.com", out)


class TestHttpProbe(unittest.TestCase):
    def test_up_2xx(self):
        with patch("app._http_probe_once", return_value=200):
            up, lat, code, err = app.probe_http("https://x", 5)
        self.assertTrue(up)
        self.assertEqual(code, 200)
        self.assertIsNone(err)
        self.assertIsNotNone(lat)

    def test_down_5xx(self):
        with patch("app._http_probe_once", return_value=503):
            up, lat, code, err = app.probe_http("https://x", 5)
        self.assertFalse(up)
        self.assertEqual(code, 503)

    def test_expected_status_match(self):
        with patch("app._http_probe_once", return_value=204):
            up, _, _, _ = app.probe_http("https://x", 5, expected=204)
        self.assertTrue(up)
        with patch("app._http_probe_once", return_value=200):
            up, _, _, _ = app.probe_http("https://x", 5, expected=204)
        self.assertFalse(up)

    def test_connection_error_is_down_not_crash(self):
        with patch("app._http_probe_once", side_effect=OSError("refused")):
            up, lat, code, err = app.probe_http("https://x", 5)
        self.assertFalse(up)
        self.assertIsNone(code)
        self.assertIsNotNone(err)

    def test_timeout_is_down(self):
        with patch("app._http_probe_once", side_effect=socket.timeout("timed out")):
            up, lat, code, err = app.probe_http("https://x", 1)
        self.assertFalse(up)

    def test_error_string_redacts_credentials(self):
        with patch("app._http_probe_once",
                   side_effect=OSError("failed for https://user:secretpw@h/x")):
            up, _, _, err = app.probe_http("https://user:secretpw@h/x", 5)
        self.assertFalse(up)
        self.assertNotIn("secretpw", err)


class TestTcpProbe(unittest.TestCase):
    def test_up(self):
        m = MagicMock()
        m.__enter__ = MagicMock(return_value=m)
        m.__exit__ = MagicMock(return_value=False)
        with patch("socket.create_connection", return_value=m):
            up, lat, code, err = app.probe_tcp("h:5432", 5)
        self.assertTrue(up)
        self.assertIsNone(err)

    def test_down(self):
        with patch("socket.create_connection", side_effect=ConnectionRefusedError("no")):
            up, lat, code, err = app.probe_tcp("h:5432", 5)
        self.assertFalse(up)
        self.assertIsNotNone(err)

    def test_timeout_is_down(self):
        with patch("socket.create_connection", side_effect=socket.timeout("t")):
            up, _, _, _ = app.probe_tcp("h:5432", 1)
        self.assertFalse(up)

    def test_bad_target_is_down(self):
        up, lat, code, err = app.probe_tcp("garbage", 5)
        self.assertFalse(up)


class TestRunAndState(unittest.TestCase):
    def setUp(self):
        _clean_db()

    def test_run_persists_result(self):
        cid, _ = app.create_uptime_check({"label": "x", "type": "http", "target": "https://x"})
        check = app.list_uptime_checks()[0]
        with patch("app._http_probe_once", return_value=200):
            res = app.run_uptime_check(check)
        self.assertTrue(res["up"])
        st = app._uptime_state(cid, int(time.time()))
        self.assertEqual(st["state"], "up")
        self.assertEqual(st["uptime"], 100.0)

    def test_uptime_pct_over_window(self):
        cid, _ = app.create_uptime_check({"label": "x", "type": "tcp", "target": "h:1"})
        now = int(time.time())
        _insert_results(cid, [(now - 40, 1), (now - 30, 0), (now - 20, 1), (now - 10, 1),
                              (now - 100000, 1)])  # last row OUTSIDE 24h window
        st = app._uptime_state(cid, now, window=86400)
        self.assertEqual(st["window_total"], 4)
        self.assertEqual(st["uptime"], 75.0)
        self.assertEqual(st["state"], "up")   # most-recent row is up

    def test_uptime7_window(self):
        cid, _ = app.create_uptime_check({"label": "x", "type": "tcp", "target": "h:1"})
        now = int(time.time())
        # 1 up inside 24h, 1 down inside 7d-but-outside-24h
        _insert_results(cid, [(now - 10, 1), (now - 2 * 86400, 0)])
        st = app._uptime_state(cid, now)
        self.assertEqual(st["uptime"], 100.0)   # 24h sees only the up
        self.assertEqual(st["uptime7"], 50.0)   # 7d sees both

    def test_unknown_when_no_results(self):
        cid, _ = app.create_uptime_check({"label": "x", "type": "http", "target": "https://x"})
        st = app._uptime_state(cid, int(time.time()))
        self.assertEqual(st["state"], "unknown")
        self.assertIsNone(st["uptime"])

    def test_results_retention_cap(self):
        cid, _ = app.create_uptime_check({"label": "x", "type": "tcp", "target": "h:1"})
        check = app.list_uptime_checks()[0]
        old_cap = app._UPTIME_RESULT_CAP
        app._UPTIME_RESULT_CAP = 5
        try:
            with patch("socket.create_connection", side_effect=ConnectionRefusedError("no")):
                for _ in range(12):
                    app.run_uptime_check(check)
                    time.sleep(0.001)
            with app.LOCK:
                n = app.DB.execute(
                    "SELECT COUNT(*) FROM uptime_results WHERE check_id=?", (cid,)).fetchone()[0]
            self.assertLessEqual(n, 5)
        finally:
            app._UPTIME_RESULT_CAP = old_cap


class TestScheduler(unittest.TestCase):
    def setUp(self):
        _clean_db()

    def test_off_empty_no_probes(self):
        with patch("app.run_uptime_check") as rc:
            probed = app._uptime_tick(now=1000.0)
        self.assertEqual(probed, [])
        rc.assert_not_called()

    def test_disabled_not_probed(self):
        app.create_uptime_check(
            {"label": "x", "type": "http", "target": "https://x", "enabled": False})
        with patch("app.run_uptime_check") as rc:
            probed = app._uptime_tick(now=1000.0)
        self.assertEqual(probed, [])
        rc.assert_not_called()

    def test_due_logic_respects_interval(self):
        cid, _ = app.create_uptime_check(
            {"label": "x", "type": "http", "target": "https://x", "interval_sec": 60})
        with patch("app.run_uptime_check") as rc:
            self.assertEqual(app._uptime_tick(now=1000.0), [cid])
            self.assertEqual(app._uptime_tick(now=1030.0), [])
            self.assertEqual(app._uptime_tick(now=1061.0), [cid])
        self.assertEqual(rc.call_count, 2)

    def test_hanging_probe_does_not_block_others(self):
        slow, _ = app.create_uptime_check(
            {"label": "slow", "type": "tcp", "target": "h:1", "interval_sec": 60})
        fast, _ = app.create_uptime_check(
            {"label": "fast", "type": "tcp", "target": "h:2", "interval_sec": 60})
        seen = []

        def fake_run(check):
            if check["label"] == "slow":
                time.sleep(0.05)
            seen.append(check["id"])
            return {"up": True}

        with patch("app.run_uptime_check", side_effect=fake_run):
            probed = app._uptime_tick(now=2000.0)
        self.assertEqual(set(probed), {slow, fast})
        self.assertEqual(set(seen), {slow, fast})

    def test_probe_exception_does_not_break_pass(self):
        a, _ = app.create_uptime_check({"label": "a", "type": "tcp", "target": "h:1"})
        b, _ = app.create_uptime_check({"label": "b", "type": "tcp", "target": "h:2"})

        def boom(check):
            if check["label"] == "a":
                raise RuntimeError("kaboom")
            return {"up": True}

        with patch("app.run_uptime_check", side_effect=boom):
            probed = app._uptime_tick(now=3000.0)
        self.assertEqual(set(probed), {a, b})


class TestSmartAlerting(unittest.TestCase):
    """The 'smarter' deltas over a plain port: anti-flap confirm, recovery with
    downtime, latency warning, and per-check opt-out. notify_uptime() is driven
    directly with a captured dispatch_alert so no real outbound happens."""
    def setUp(self):
        _clean_db()
        self.sent = []
        self.s = {"alerts_enabled": "1", "discord_webhook_url": "https://hook",
                  "alert_min_level": "warning"}

    def _capture(self):
        def fake(s, level, title, detail, host=None):
            self.sent.append((level, title, detail))
            return [("discord", True, None)]
        return patch("app.dispatch_alert", side_effect=fake)

    def test_confirmed_down_needs_threshold(self):
        cid, _ = app.create_uptime_check(
            {"label": "x", "type": "tcp", "target": "h:1", "fail_threshold": 2})
        now = int(time.time())
        _insert_results(cid, [(now - 5, 0)])
        self.assertFalse(app._uptime_confirmed_down(cid, 2))   # 1 fail, threshold 2
        _insert_results(cid, [(now - 1, 0)])
        self.assertTrue(app._uptime_confirmed_down(cid, 2))    # 2 fails

    def test_down_fires_once_then_recovery_with_downtime(self):
        cid, _ = app.create_uptime_check(
            {"label": "NAS", "type": "tcp", "target": "h:1", "fail_threshold": 2})
        now = int(time.time())
        _insert_results(cid, [(now - 60, 0), (now - 30, 0)])  # two consecutive fails
        with self._capture():
            app.notify_uptime(self.s)
            app.notify_uptime(self.s)   # second pass: edge-triggered, no re-fire
        downs = [m for m in self.sent if "DOWN" in m[1]]
        self.assertEqual(len(downs), 1)
        self.assertEqual(downs[0][0], "critical")
        # Now it recovers — a single up result.
        _insert_results(cid, [(now - 1, 1)])
        with self._capture():
            app.notify_uptime(self.s)
        recs = [m for m in self.sent if "recovered" in m[1]]
        self.assertEqual(len(recs), 1)
        self.assertIn("down", recs[0][2].lower())   # mentions the downtime

    def test_latency_warning(self):
        cid, _ = app.create_uptime_check(
            {"label": "slowapp", "type": "http", "target": "https://x",
             "latency_warn_ms": 100})
        now = int(time.time())
        _insert_results(cid, [(now - 1, 1, 450.0)])   # up but slow (450 ms > 100)
        with self._capture():
            app.notify_uptime(self.s)
        slow = [m for m in self.sent if "slow" in m[1]]
        self.assertEqual(len(slow), 1)
        self.assertEqual(slow[0][0], "warning")

    def test_no_alert_when_check_opts_out(self):
        cid, _ = app.create_uptime_check(
            {"label": "x", "type": "tcp", "target": "h:1", "alerts_enabled": False,
             "fail_threshold": 1})
        now = int(time.time())
        _insert_results(cid, [(now - 1, 0)])
        with self._capture():
            app.notify_uptime(self.s)
        self.assertEqual(self.sent, [])

    def test_insights_surface_down_and_redact(self):
        app.create_uptime_check(
            {"label": "Immich", "type": "http", "target": "https://user:pw@immich.lan"})
        cid = app.list_uptime_checks()[0]["id"]
        now = int(time.time())
        _insert_results(cid, [(now - 1, 0)])
        rows = app.uptime_insights()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["level"], "critical")
        blob = rows[0]["title"] + rows[0]["detail"]
        self.assertIn("Immich", blob)
        self.assertNotIn("pw@", blob)       # credentials redacted out of the feed


class TestApi(unittest.TestCase):
    def setUp(self):
        _clean_db()
        self.c = app.app.test_client()

    def test_list_empty_200(self):
        r = self.c.get("/api/uptime")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["checks"], [])

    def test_create_201_then_listed(self):
        r = self.c.post("/api/uptime", json={"label": "s", "type": "http", "target": "https://x"})
        self.assertEqual(r.status_code, 201)
        cid = r.get_json()["id"]
        r = self.c.get("/api/uptime")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["checks"][0]["id"], cid)

    def test_create_bad_400_clean(self):
        r = self.c.post("/api/uptime", json={"label": "", "type": "http", "target": "x"})
        self.assertEqual(r.status_code, 400)
        body = r.get_json()
        self.assertFalse(body["ok"])
        self.assertIn("error", body)

    def test_create_empty_body_400(self):
        r = self.c.post("/api/uptime", json={})
        self.assertEqual(r.status_code, 400)

    def test_patch_404(self):
        r = self.c.patch("/api/uptime/nope", json={"enabled": True})
        self.assertEqual(r.status_code, 404)

    def test_delete_roundtrip(self):
        cid = self.c.post("/api/uptime",
                          json={"label": "s", "type": "tcp", "target": "h:1"}).get_json()["id"]
        r = self.c.delete("/api/uptime/" + cid)
        self.assertEqual(r.status_code, 200)
        r = self.c.delete("/api/uptime/" + cid)
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
