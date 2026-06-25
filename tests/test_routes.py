"""Unit tests for notification routing rules (route a firing alert to a specific
channel by entity glob + min_level, before the default fan-out).

Covers: route CRUD + validation, the always-200/clean-400 HTTP surface, the
selection semantics (glob incl. *, case-insensitivity, min_level gating, ordered
union of matching channels), and the dispatch/engine integration — most importantly
the MUST-NOT-REGRESS invariant that ZERO routes (or no match) behaves byte-identically
to the prior dispatch_alert(..., channel=rule.channel) fan-out, plus the sane fallback
when a matched route's channel is unconfigured (never black-hole), recovery routing
the same way as the fire, and maintenance still suppressing first. Channel send is
mocked (nothing is actually POSTed)."""
import os
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


def _clean():
    with app.LOCK:
        app.DB.execute("DELETE FROM alert_rules")
        app.DB.execute("DELETE FROM alert_history")
        app.DB.execute("DELETE FROM notification_routes")
        app.DB.commit()


SIG_ANOMALY = {"anomalies": {"items": [
    {"key": "gpu_power", "unit": "W", "value": 320.0, "baseline": 200.0,
     "z": 4.1, "direction": "spike"}]}}
SIG_QUIET = {"anomalies": {"items": []}}


# ── CRUD + validation ─────────────────────────────────────────────────────────
class TestRouteCrud(unittest.TestCase):
    def setUp(self):
        _clean()

    def test_create_persists_defaults(self):
        rid, err = app.create_route({"label": "R", "channel": "ntfy"})
        self.assertIsNone(err)
        rows = app.list_routes()
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["id"], rid)
        self.assertEqual(r["match"], "*")
        self.assertEqual(r["min_level"], "info")
        self.assertEqual(r["channel"], "ntfy")
        self.assertTrue(r["enabled"])

    def test_requires_label(self):
        rid, err = app.create_route({"channel": "ntfy"})
        self.assertIsNone(rid)
        self.assertIn("label", err)

    def test_rejects_channel_all(self):
        # 'all' is the default-fan-out behaviour, not a concrete redirect target.
        rid, err = app.create_route({"label": "R", "channel": "all"})
        self.assertIsNone(rid)
        self.assertIn("Channel", err)

    def test_rejects_unknown_channel(self):
        rid, err = app.create_route({"label": "R", "channel": "carrier-pigeon"})
        self.assertIsNone(rid)
        self.assertIn("Channel", err)

    def test_rejects_bad_level(self):
        rid, err = app.create_route({"label": "R", "channel": "ntfy", "min_level": "loud"})
        self.assertIsNone(rid)
        self.assertIn("min_level", err)

    def test_rejects_empty_match(self):
        rid, err = app.create_route({"label": "R", "channel": "ntfy", "match": "   "})
        self.assertIsNone(rid)
        self.assertIn("match", err)

    def test_rejects_bad_priority(self):
        rid, err = app.create_route({"label": "R", "channel": "ntfy", "priority": "soon"})
        self.assertIsNone(rid)
        self.assertIn("priority", err)

    def test_update_toggle_and_full_and_delete(self):
        rid, _ = app.create_route({"label": "R", "channel": "ntfy"})
        ok, err = app.update_route(rid, {"enabled": False})
        self.assertTrue(ok)
        self.assertFalse(app.list_routes()[0]["enabled"])
        ok, err = app.update_route(rid, {"label": "R2", "channel": "webhook",
                                         "match": "*gpu*", "min_level": "warning", "priority": 5})
        self.assertTrue(ok)
        r = app.list_routes()[0]
        self.assertEqual(r["channel"], "webhook")
        self.assertEqual(r["match"], "*gpu*")
        self.assertEqual(r["min_level"], "warning")
        self.assertEqual(r["priority"], 5)
        self.assertTrue(app.delete_route(rid))
        self.assertEqual(app.list_routes(), [])

    def test_update_unknown(self):
        ok, err = app.update_route("nope", {"enabled": True})
        self.assertFalse(ok)
        self.assertEqual(err, "not found")

    def test_empty_update_is_noop(self):
        rid, _ = app.create_route({"label": "R", "channel": "ntfy"})
        ok, err = app.update_route(rid, {})
        self.assertFalse(ok)
        self.assertEqual(err, "empty update")

    def test_ordered_by_priority_then_created(self):
        a, _ = app.create_route({"label": "A", "channel": "ntfy", "priority": 10})
        b, _ = app.create_route({"label": "B", "channel": "webhook", "priority": 1})
        order = [r["id"] for r in app.list_routes()]
        self.assertEqual(order, [b, a])


class TestRouteApi(unittest.TestCase):
    def setUp(self):
        _clean()
        self.c = app.app.test_client()

    def test_post_201_get_lists_delete_200(self):
        r = self.c.post("/api/alerts/routes", json={"label": "R", "channel": "ntfy"})
        self.assertEqual(r.status_code, 201)
        rid = r.get_json()["id"]
        g = self.c.get("/api/alerts/routes")
        self.assertEqual(g.status_code, 200)
        body = g.get_json()
        self.assertEqual(len(body["routes"]), 1)
        self.assertIn("channels", body)
        d = self.c.delete(f"/api/alerts/routes/{rid}")
        self.assertEqual(d.status_code, 200)
        self.assertTrue(d.get_json()["ok"])

    def test_post_bad_input_clean_400(self):
        r = self.c.post("/api/alerts/routes", json={"label": "", "channel": "ntfy"})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.get_json()["ok"])

    def test_delete_unknown_404(self):
        r = self.c.delete("/api/alerts/routes/nope")
        self.assertEqual(r.status_code, 404)

    def test_patch_unknown_404(self):
        r = self.c.patch("/api/alerts/routes/nope", json={"enabled": True})
        self.assertEqual(r.status_code, 404)

    def test_patch_bad_400(self):
        rid = self.c.post("/api/alerts/routes",
                          json={"label": "R", "channel": "ntfy"}).get_json()["id"]
        r = self.c.patch(f"/api/alerts/routes/{rid}", json={"label": "x", "channel": "nope"})
        self.assertEqual(r.status_code, 400)


# ── Selection semantics ───────────────────────────────────────────────────────
class TestRouteSelection(unittest.TestCase):
    def setUp(self):
        _clean()

    def test_no_routes_selects_nothing(self):
        self.assertEqual(app._route_channels("critical", "anything"), [])

    def test_star_matches_any(self):
        app.create_route({"label": "R", "channel": "ntfy", "match": "*"})
        self.assertEqual(app._route_channels("info", "r anomaly on gpu_power"), ["ntfy"])

    def test_glob_substring(self):
        app.create_route({"label": "R", "channel": "ntfy", "match": "*gpu*"})
        self.assertEqual(app._route_channels("info", "r anomaly on gpu_power"), ["ntfy"])
        self.assertEqual(app._route_channels("info", "disk / fills"), [])

    def test_case_insensitive(self):
        app.create_route({"label": "R", "channel": "ntfy", "match": "*GPU*"})
        self.assertEqual(app._route_channels("info", "anomaly on gpu_power"), ["ntfy"])

    def test_min_level_gates_below(self):
        # A warning route must NOT fire for an info alert.
        app.create_route({"label": "R", "channel": "ntfy", "match": "*", "min_level": "warning"})
        self.assertEqual(app._route_channels("info", "x"), [])

    def test_min_level_matches_at_or_above(self):
        # A critical alert matches a warning route (>= min_level).
        app.create_route({"label": "R", "channel": "ntfy", "match": "*", "min_level": "warning"})
        self.assertEqual(app._route_channels("warning", "x"), ["ntfy"])
        self.assertEqual(app._route_channels("critical", "x"), ["ntfy"])

    def test_disabled_route_skipped(self):
        rid, _ = app.create_route({"label": "R", "channel": "ntfy", "match": "*"})
        app.update_route(rid, {"enabled": False})
        self.assertEqual(app._route_channels("critical", "x"), [])

    def test_union_ordered_deduped(self):
        app.create_route({"label": "A", "channel": "ntfy", "match": "*", "priority": 1})
        app.create_route({"label": "B", "channel": "webhook", "match": "*gpu*", "priority": 2})
        app.create_route({"label": "C", "channel": "ntfy", "match": "*", "priority": 3})  # dup channel
        self.assertEqual(app._route_channels("critical", "anomaly on gpu_power"),
                         ["ntfy", "webhook"])


# ── dispatch_routed: fallback + redirect + no-black-hole ──────────────────────
class TestDispatchRouted(unittest.TestCase):
    def setUp(self):
        _clean()

    def test_no_routes_identical_to_default_fanout(self):
        # THE invariant: zero routes => dispatch_routed == dispatch_alert(default).
        s = {**app.SETTING_DEFAULTS, "webhook_url": "https://e.test/h", "ntfy_topic": ""}
        with patch("app._post_json", return_value=(200, b"{}")):
            base = app.dispatch_alert(s, "info", "T", "D", channel="all")
            routed = app.dispatch_routed(s, "info", "T", "D",
                                         entity="anything", default_channel="all")
        self.assertEqual(base, routed)

    def test_no_routes_respects_rule_channel(self):
        s = {**app.SETTING_DEFAULTS, "webhook_url": "https://e.test/h", "ntfy_topic": "t"}
        with patch("app._post_json", return_value=(200, b"{}")), \
             patch("app._post_text", return_value=(200, b"")):
            base = app.dispatch_alert(s, "info", "T", "D", channel="webhook")
            routed = app.dispatch_routed(s, "info", "T", "D",
                                         entity="x", default_channel="webhook")
        self.assertEqual(base, routed)
        self.assertEqual({c for c, _, _ in routed}, {"webhook"})

    def test_matching_route_redirects(self):
        # Rule's default channel is ntfy, but a route redirects gpu alerts to webhook.
        app.create_route({"label": "R", "channel": "webhook", "match": "*gpu*"})
        s = {**app.SETTING_DEFAULTS, "webhook_url": "https://e.test/h", "ntfy_topic": "t"}
        with patch("app._post_json", return_value=(200, b"{}")) as pj, \
             patch("app._post_text", return_value=(200, b"")) as pt:
            res = app.dispatch_routed(s, "warning", "T", "D",
                                      entity="anomaly on gpu_power", default_channel="ntfy")
        self.assertEqual({c for c, _, _ in res}, {"webhook"})
        pj.assert_called()        # webhook went out
        pt.assert_not_called()    # ntfy did NOT

    def test_matched_unconfigured_channel_falls_back_not_blackhole(self):
        # Route points at slack which isn't configured; must fall back to the rule's
        # default channel (ntfy) — the alert still goes out, nothing is dropped.
        app.create_route({"label": "R", "channel": "slack", "match": "*"})
        s = {**app.SETTING_DEFAULTS, "slack_webhook_url": "", "ntfy_topic": "t"}
        with patch("app._post_text", return_value=(200, b"")) as pt:
            res = app.dispatch_routed(s, "warning", "T", "D",
                                      entity="x", default_channel="ntfy")
        chans = {c for c, _, _ in res}
        self.assertIn("ntfy", chans)
        self.assertTrue(any(ok for _, ok, _ in res))  # not black-holed
        pt.assert_called()


# ── End-to-end through evaluate_rules ─────────────────────────────────────────
class TestRoutingInEngine(unittest.TestCase):
    def setUp(self):
        _clean()
        # ntfy + webhook both configured.
        app.save_settings({"discord_webhook_url": "", "telegram_token": "",
                           "telegram_chat_id": "", "ntfy_topic": "hlm-test",
                           "webhook_url": "https://e.test/h"})

    def tearDown(self):
        app.save_settings({"ntfy_topic": "", "webhook_url": ""})
        _clean()

    def test_zero_routes_unchanged_behaviour(self):
        # Rule fires to its own channel (ntfy) exactly as before; webhook untouched.
        app.create_rule({"name": "gpu", "ctype": "anomaly", "params": {"series": "any"},
                         "enabled": True, "channel": "ntfy", "cooldown_min": 60})
        with patch("app._post_text", return_value=(200, b"")) as pt, \
             patch("app._post_json", return_value=(200, b"{}")) as pj:
            self.assertEqual(app.evaluate_rules(SIG_ANOMALY), 1)
        pt.assert_called()         # ntfy
        pj.assert_not_called()     # webhook NOT used
        hist = app.list_alert_history()
        self.assertEqual(hist[0]["channel"], "ntfy")
        self.assertEqual(hist[0]["status"], "sent")

    def test_route_redirects_fired_alert(self):
        # Same rule (channel ntfy) but a route sends gpu alerts to webhook instead.
        app.create_rule({"name": "gpu", "ctype": "anomaly", "params": {"series": "any"},
                         "enabled": True, "channel": "ntfy", "cooldown_min": 60})
        app.create_route({"label": "gpu->webhook", "channel": "webhook", "match": "*gpu*"})
        with patch("app._post_text", return_value=(200, b"")) as pt, \
             patch("app._post_json", return_value=(200, b"{}")) as pj:
            self.assertEqual(app.evaluate_rules(SIG_ANOMALY), 1)
        pj.assert_called()         # webhook
        pt.assert_not_called()     # ntfy bypassed
        self.assertEqual(app.list_alert_history()[0]["channel"], "webhook")

    def test_info_route_does_not_fire_for_warning_only_when_below(self):
        # A route with min_level=critical must not capture a warning alert: it falls
        # through to the default (ntfy) fan-out.
        app.create_rule({"name": "gpu", "ctype": "anomaly", "params": {"series": "any"},
                         "enabled": True, "channel": "ntfy", "level": "warning",
                         "cooldown_min": 60})
        app.create_route({"label": "crit->webhook", "channel": "webhook",
                          "match": "*gpu*", "min_level": "critical"})
        with patch("app._post_text", return_value=(200, b"")) as pt, \
             patch("app._post_json", return_value=(200, b"{}")) as pj:
            self.assertEqual(app.evaluate_rules(SIG_ANOMALY), 1)
        pt.assert_called()         # fell back to ntfy
        pj.assert_not_called()

    def test_recovery_routes_like_the_fire(self):
        # The fire is redirected to webhook; the recovery (info) carries the same
        # rule-name+ctype entity. With a min_level=info route it routes to webhook too.
        app.create_rule({"name": "gpu", "ctype": "anomaly", "params": {"series": "any"},
                         "enabled": True, "channel": "ntfy", "cooldown_min": 0})
        app.create_route({"label": "gpu->webhook", "channel": "webhook",
                          "match": "*gpu*", "min_level": "info"})
        with patch("app._post_text", return_value=(200, b"")) as pt, \
             patch("app._post_json", return_value=(200, b"{}")) as pj:
            app.evaluate_rules(SIG_ANOMALY)   # fire -> webhook
            app.evaluate_rules(SIG_QUIET)     # recovery -> webhook (same route)
        pt.assert_not_called()                # ntfy never used
        statuses = {h["status"]: h["channel"] for h in app.list_alert_history()}
        self.assertEqual(statuses.get("sent"), "webhook")
        self.assertEqual(statuses.get("recovered"), "webhook")

    def test_maintenance_suppresses_before_routing(self):
        # Maintenance still wins: nothing is sent and nothing is routed.
        app.create_rule({"name": "gpu", "ctype": "anomaly", "params": {"series": "any"},
                         "enabled": True, "channel": "ntfy", "cooldown_min": 60})
        app.create_route({"label": "gpu->webhook", "channel": "webhook", "match": "*gpu*"})
        app._MAINT_SUPPRESS_LOGGED.clear()
        with patch("app._in_maintenance", return_value=(True, None)), \
             patch("app._post_text", return_value=(200, b"")) as pt, \
             patch("app._post_json", return_value=(200, b"{}")) as pj:
            self.assertEqual(app.evaluate_rules(SIG_ANOMALY), 0)
        pt.assert_not_called()
        pj.assert_not_called()
        self.assertEqual(app.list_alert_history()[0]["status"], "suppressed")


if __name__ == "__main__":
    unittest.main()
