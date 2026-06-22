"""Unit tests for the TLS/SSL certificate-expiry uptime check (type='cert').

Covers: cert-type validation (host / host:port / https:// URL accepted; garbage
rejected; cert_warn_days bounds), the _parse_cert_target parser, probe_cert state
logic against a fully MOCKED ssl handshake (valid → up, expiring-within-window →
up+cert_warn flagged, expired → down, handshake failure / timeout / no-cert →
down with reason), result persistence of days_to_expiry, uptime_down firing on a
down (expired) cert check, and the Prometheus homelab_uptime_cert_days_remaining
family (present when a cert check has probed, absent otherwise).

ALL network is mocked — there is NO real outbound TLS in CI."""
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


def _not_after(days_from_now):
    """An OpenSSL-style notAfter string `days_from_now` days out (UTC)."""
    t = time.gmtime(time.time() + days_from_now * 86400)
    return time.strftime("%b %d %H:%M:%S %Y GMT", t)


def _cert_dict(days_from_now, cn="example.com", issuer="Test CA"):
    return {
        "notAfter": _not_after(days_from_now),
        "subject": ((("commonName", cn),),),
        "issuer": ((("commonName", issuer),),),
    }


class _MockSSLContext:
    """Stand-in for ssl.SSLContext whose wrap_socket returns a TLS socket that
    yields a preset getpeercert() dict (or raises a preset handshake error)."""
    def __init__(self, cert=None, raise_on_wrap=None):
        self._cert = cert
        self._raise = raise_on_wrap
        self.check_hostname = True
        self.verify_mode = None

    def wrap_socket(self, sock, server_hostname=None):
        if self._raise is not None:
            raise self._raise
        ss = MagicMock()
        ss.__enter__.return_value = ss
        ss.__exit__.return_value = False
        ss.getpeercert.return_value = self._cert
        return ss


def _conn_cm():
    """A context-manager socket stand-in for socket.create_connection."""
    sock = MagicMock()
    sock.__enter__.return_value = sock
    sock.__exit__.return_value = False
    return sock


# ── Target parsing ───────────────────────────────────────────────────────────
class TestParseCertTarget(unittest.TestCase):
    def test_bare_host_defaults_443(self):
        self.assertEqual(app._parse_cert_target("example.com"), ("example.com", 443))

    def test_host_port(self):
        self.assertEqual(app._parse_cert_target("db.lan:8443"), ("db.lan", 8443))

    def test_https_url(self):
        self.assertEqual(app._parse_cert_target("https://example.com/health"),
                         ("example.com", 443))

    def test_https_url_with_port(self):
        self.assertEqual(app._parse_cert_target("https://example.com:9443/x"),
                         ("example.com", 9443))

    def test_ipv6_bracketed(self):
        self.assertEqual(app._parse_cert_target("[2001:db8::1]:443"),
                         ("2001:db8::1", 443))

    def test_ipv6_bracketed_default_port(self):
        self.assertEqual(app._parse_cert_target("[2001:db8::1]"),
                         ("2001:db8::1", 443))

    def test_reject_empty(self):
        self.assertEqual(app._parse_cert_target("  "), (None, None))

    def test_reject_bad_port(self):
        self.assertEqual(app._parse_cert_target("h:99999"), (None, None))
        self.assertEqual(app._parse_cert_target("h:abc"), (None, None))

    def test_reject_bare_ipv6_multicolon(self):
        self.assertEqual(app._parse_cert_target("2001:db8::1"), (None, None))

    def test_reject_whitespace_host(self):
        self.assertEqual(app._parse_cert_target("bad host"), (None, None))


# ── Validation / CRUD ────────────────────────────────────────────────────────
class TestCertValidation(unittest.TestCase):
    def setUp(self):
        _clean_db()

    def test_create_cert_host_only_defaults_warn(self):
        cid, err = app.create_uptime_check(
            {"label": "gh", "type": "cert", "target": "github.com"})
        self.assertIsNone(err)
        c = app.list_uptime_checks()[0]
        self.assertEqual(c["type"], "cert")
        self.assertEqual(c["target"], "github.com")
        self.assertEqual(c["cert_warn_days"], app._UPTIME_CERT_DEFAULT_WARN_DAYS)

    def test_create_cert_host_port(self):
        cid, err = app.create_uptime_check(
            {"label": "n", "type": "cert", "target": "nas.lan:8443", "cert_warn_days": 30})
        self.assertIsNone(err)
        self.assertEqual(app.list_uptime_checks()[0]["cert_warn_days"], 30)

    def test_create_cert_https_url(self):
        _, err = app.create_uptime_check(
            {"label": "x", "type": "cert", "target": "https://example.com"})
        self.assertIsNone(err)

    def test_reject_garbage_target(self):
        _, err = app.create_uptime_check(
            {"label": "x", "type": "cert", "target": "not a host"})
        self.assertIsNotNone(err)
        self.assertIn("cert", err.lower())

    def test_reject_bad_warn_days(self):
        _, err = app.create_uptime_check(
            {"label": "x", "type": "cert", "target": "h", "cert_warn_days": "lots"})
        self.assertIsNotNone(err)
        _, err = app.create_uptime_check(
            {"label": "x", "type": "cert", "target": "h", "cert_warn_days": 0})
        self.assertIsNotNone(err)
        _, err = app.create_uptime_check(
            {"label": "x", "type": "cert", "target": "h", "cert_warn_days": 99999})
        self.assertIsNotNone(err)

    def test_http_tcp_have_null_cert_warn(self):
        cid, _ = app.create_uptime_check({"label": "h", "type": "tcp", "target": "h:1"})
        self.assertIsNone(app.list_uptime_checks()[0]["cert_warn_days"])


# ── probe_cert state logic ───────────────────────────────────────────────────
class TestProbeCert(unittest.TestCase):
    def _run(self, cert=None, raise_on_wrap=None, conn_exc=None, warn_days=21):
        ctx = _MockSSLContext(cert=cert, raise_on_wrap=raise_on_wrap)
        cc = MagicMock(side_effect=conn_exc) if conn_exc else MagicMock(return_value=_conn_cm())
        with patch("ssl.create_default_context", return_value=ctx), \
             patch("socket.create_connection", cc):
            return app.probe_cert("example.com:443", 5, warn_days)

    def test_valid_far_future_is_up(self):
        up, lat, days, err, extra = self._run(cert=_cert_dict(90))
        self.assertTrue(up)
        self.assertIsNone(err)
        self.assertGreaterEqual(days, 89)
        self.assertFalse(extra.get("expiring"))
        self.assertEqual(extra.get("subject_cn"), "example.com")
        self.assertEqual(extra.get("issuer_cn"), "Test CA")

    def test_expiring_within_window_is_up_but_flagged(self):
        up, lat, days, err, extra = self._run(cert=_cert_dict(10), warn_days=21)
        self.assertTrue(up)                 # not a hard failure
        self.assertTrue(extra.get("expiring"))
        self.assertIsNotNone(err)
        self.assertIn("expires", err.lower())
        self.assertLessEqual(days, 21)

    def test_expired_is_down(self):
        up, lat, days, err, extra = self._run(cert=_cert_dict(-3))
        self.assertFalse(up)
        self.assertLess(days, 0)
        self.assertIn("expired", err.lower())

    def test_handshake_failure_is_down(self):
        up, lat, days, err, extra = self._run(
            raise_on_wrap=app.ssl.SSLCertVerificationError("self-signed"))
        self.assertFalse(up)
        self.assertIsNone(days)
        self.assertTrue(err)

    def test_connection_refused_is_down(self):
        up, lat, days, err, extra = self._run(conn_exc=ConnectionRefusedError("no"))
        self.assertFalse(up)
        self.assertIsNone(days)
        self.assertTrue(err)

    def test_timeout_is_down_not_crash(self):
        up, lat, days, err, extra = self._run(conn_exc=socket.timeout("t"))
        self.assertFalse(up)
        self.assertIsNone(days)

    def test_no_cert_presented_is_down(self):
        up, lat, days, err, extra = self._run(cert={})
        self.assertFalse(up)
        self.assertIn("no certificate", err.lower())

    def test_bad_target_is_down(self):
        up, lat, days, err, extra = app.probe_cert("not a host", 5, 21)
        self.assertFalse(up)
        self.assertIn("bad cert", err.lower())


# ── run_uptime_check records days_to_expiry + overview surfaces it ────────────
class TestCertRunAndOverview(unittest.TestCase):
    def setUp(self):
        _clean_db()

    def _check(self, target="example.com:443", warn=21):
        cid, err = app.create_uptime_check(
            {"label": "cert", "type": "cert", "target": target, "cert_warn_days": warn})
        self.assertIsNone(err)
        return app.list_uptime_checks()[0]

    def test_run_records_days_to_expiry(self):
        check = self._check()
        with patch("app.probe_cert", return_value=(True, 12.0, 47, None, {})):
            res = app.run_uptime_check(check)
        self.assertEqual(res["days_to_expiry"], 47)
        ov = app.uptime_overview()["checks"][0]
        self.assertEqual(ov["days_to_expiry"], 47)
        self.assertEqual(ov["state"], "up")

    def test_overview_cert_warn_flag(self):
        check = self._check(warn=21)
        with patch("app.probe_cert",
                   return_value=(True, 12.0, 5, "certificate expires in 5d", {})):
            app.run_uptime_check(check)
        ov = app.uptime_overview()["checks"][0]
        self.assertEqual(ov["state"], "up")
        self.assertTrue(ov["cert_warn"])

    def test_overview_expired_is_down(self):
        check = self._check()
        with patch("app.probe_cert",
                   return_value=(False, 12.0, -2, "certificate expired 2d ago", {})):
            app.run_uptime_check(check)
        ov = app.uptime_overview()["checks"][0]
        self.assertEqual(ov["state"], "down")
        self.assertFalse(ov["cert_warn"])
        self.assertIn("expired", (ov["last_err"] or "").lower())


# ── uptime_down inherits cert failures ───────────────────────────────────────
class TestCertAlertInheritance(unittest.TestCase):
    def setUp(self):
        _clean_db()

    def test_uptime_down_fires_on_expired_cert(self):
        rule = {"id": "r1", "name": "C", "enabled": True, "ctype": "uptime_down",
                "params": {"check_id": "any"}}
        sig = {"uptime": [{"id": "c1", "label": "MyCert", "enabled": True,
                           "state": "down", "type": "cert",
                           "last_err": "certificate expired 2d ago"}]}
        active, title, detail = app._eval_rule(rule, sig)
        self.assertTrue(active)
        self.assertIn("expired", detail.lower())

    def test_uptime_down_silent_when_only_expiring(self):
        # An up-but-expiring cert (state stays 'up') must NOT fire uptime_down.
        rule = {"id": "r1", "name": "C", "enabled": True, "ctype": "uptime_down",
                "params": {"check_id": "any"}}
        sig = {"uptime": [{"id": "c1", "label": "MyCert", "enabled": True,
                           "state": "up", "type": "cert", "cert_warn": True}]}
        active, _t, _d = app._eval_rule(rule, sig)
        self.assertFalse(active)


# ── Prometheus export ────────────────────────────────────────────────────────
class TestCertMetric(unittest.TestCase):
    def setUp(self):
        self.c = app.app.test_client()
        _clean_db()

    def tearDown(self):
        _clean_db()

    def _result(self, cid, up, days):
        with app.LOCK:
            app.DB.execute(
                "INSERT INTO uptime_results(check_id,ts,up,latency_ms,code,err,days_to_expiry) "
                "VALUES(?,?,?,?,?,?,?)",
                (cid, int(time.time()), 1 if up else 0, 9.0, None, None, days))
            app.DB.commit()

    def test_cert_days_metric_present_when_cert_check_probed(self):
        cid, _ = app.create_uptime_check(
            {"label": "ghcert", "type": "cert", "target": "github.com:443"})
        self._result(cid, up=True, days=58)
        body = self.c.get("/metrics").get_data(as_text=True)
        self.assertIn("homelab_uptime_cert_days_remaining", body)
        self.assertIn("58", body)

    def test_cert_metric_absent_without_cert_check(self):
        cid, _ = app.create_uptime_check({"label": "tcp", "type": "tcp", "target": "h:1"})
        self._result(cid, up=True, days=None)
        body = self.c.get("/metrics").get_data(as_text=True)
        self.assertNotIn("homelab_uptime_cert_days_remaining", body)

    def test_cert_metric_negative_when_expired(self):
        cid, _ = app.create_uptime_check(
            {"label": "old", "type": "cert", "target": "expired.example.com:443"})
        self._result(cid, up=False, days=-7)
        body = self.c.get("/metrics").get_data(as_text=True)
        self.assertIn("homelab_uptime_cert_days_remaining", body)
        self.assertIn("-7", body)


# ── OFF/empty = no probes ────────────────────────────────────────────────────
class TestCertInertWhenEmpty(unittest.TestCase):
    def setUp(self):
        _clean_db()

    def test_no_cert_checks_no_probe(self):
        with patch("app.probe_cert") as pc, patch("app.run_uptime_check") as rc:
            probed = app._uptime_tick(now=1000.0)
        self.assertEqual(probed, [])
        pc.assert_not_called()
        rc.assert_not_called()


if __name__ == "__main__":
    unittest.main()
