"""Route-by-route regression net for the login gate, driven by app.url_map —
no list of paths, so a route added tomorrow is covered the day it lands.

- No password: the gate never answers for any route (the request reaches the
  view exactly as before the login existed).
- Password set, nobody logged in: every route that isn't marked open answers
  401 (or redirects a page load to /login), whatever its method.
- Logged in by cookie: every mutating route refuses a request without the CSRF
  token.
"""
import re
import unittest

from flask import g

from auth_helpers import app, clear_login, login
from backend.auth import gate, marks

_SAFE = {"GET", "HEAD", "OPTIONS"}


def _sample_path(rule):
    """A concrete URL for a rule, filling each <converter:arg> with a dummy."""
    return re.sub(r"<(?:[^:<>]+:)?([^<>]+)>", "x", rule.rule)


def _routes():
    out = []
    for rule in app.app.url_map.iter_rules():
        if rule.endpoint == "static":
            continue
        view = app.app.view_functions[rule.endpoint]
        for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
            out.append((rule, method, view))
    return out


class TestGateSweep(unittest.TestCase):
    def setUp(self):
        clear_login()

    def tearDown(self):
        clear_login()

    def test_there_are_many_routes(self):
        self.assertGreater(len(_routes()), 80)

    def test_no_password_gate_is_a_no_op_for_every_route(self):
        for rule, method, _view in _routes():
            with app.app.test_request_context(_sample_path(rule), method=method):
                self.assertIsNone(gate.gate(), "%s %s" % (method, rule.rule))
                self.assertIsNone(getattr(g, "hlm_principal", None))

    def test_password_set_anonymous_is_refused_everywhere_but_open_routes(self):
        login(app.app.test_client())
        anon = app.app.test_client()
        checked = 0
        for rule, method, view in _routes():
            path = _sample_path(rule)
            r = anon.open(path, method=method, json={} if method not in _SAFE else None)
            if marks.is_open(view):
                body = r.get_json(silent=True) or {}
                self.assertNotEqual(body.get("error"), "auth_required", "%s %s is marked open" % (method, rule.rule))
                continue
            self.assertIn(r.status_code, (401, 302), "%s %s let an anonymous caller in (%s)"
                          % (method, rule.rule, r.status_code))
            checked += 1
        self.assertGreater(checked, 60)

    def test_every_mutation_needs_csrf_with_a_session(self):
        c = app.app.test_client()
        login(c)
        checked = 0
        for rule, method, view in _routes():
            if method in _SAFE or marks.is_open(view):
                continue
            r = c.open(_sample_path(rule), method=method, json={})
            self.assertEqual(r.status_code, 403, "%s %s accepted a cookie request without CSRF"
                             % (method, rule.rule))
            self.assertEqual(r.get_json()["error"], "csrf")
            checked += 1
        self.assertGreater(checked, 25)

    def test_open_routes_are_the_reviewed_set(self):
        # Reviewed list: anything newly marked open shows up here deliberately.
        opened = sorted({r.endpoint for r, _m, v in _routes() if marks.is_open(v)})
        self.assertEqual(opened, sorted([
            "auth_api.login", "auth_api.login_page", "auth_api.setup", "auth_api.status",
            "system.favicon", "system.healthz", "system.locales", "system.metrics",
            "uptime_api.api_public_status", "uptime_api.api_public_status_one", "uptime_api.public_status",
        ]))

    def test_sensitive_routes(self):
        sens = sorted({r.endpoint for r, _m, v in _routes() if marks.is_sensitive(v)})
        for ep in ("system.api_backup_download", "experiments.api_keys_route", "auth_api.list_sessions"):
            self.assertIn(ep, sens)


if __name__ == "__main__":
    unittest.main()
