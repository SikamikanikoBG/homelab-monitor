"""Tests for the AI probable-cause line in incident notifications (E1).

Two surfaces, both gated by the OFF-by-default opt-in `notify_ai_cause`:

  1. ENRICHMENT of the primary incident alert (the rule engine's ctype=='incident'
     notification) with a concise "🧠 Probable cause: …" line built from the
     CACHED ai_explanation — a pure read that NEVER calls the LLM on the dispatch
     path (asserted with a monkeypatched call-counter that must stay at zero).

  2. The SUPPLEMENTARY, off-poll cause notification sent by the dedicated
     auto-explain worker (`_maybe_send_incident_cause`) AFTER it persists an
     explanation: at most once per incident (atomic dedup), only while the incident
     is still OPEN + both opt-ins ON + a channel configured, and SUPPRESSED by a
     maintenance/quick-mute window exactly like any other notification.

Also: default-OFF back-compat (notification byte-identical when the opt-in is off),
the additive dedup-flag migration, and the no-leak contract (a planted
credentialed target in the stored explanation is redacted out of the body).
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


def _clean():
    with app.LOCK:
        app.DB.execute("DELETE FROM incidents")
        app.DB.execute("DELETE FROM incident_members")
        app.DB.execute("DELETE FROM alert_rules")
        app.DB.execute("DELETE FROM alert_history")
        app.DB.execute("DELETE FROM settings")
        app.DB.commit()


def _bundle(*series):
    items = []
    for s in series:
        key, z, direction = (s, 4.0, "spike") if isinstance(s, str) else s
        items.append({"key": key, "unit": "W", "value": 300.0, "baseline": 200.0,
                      "z": z, "stddev": 25.0, "direction": direction,
                      "magnitude": 100.0, "samples": 40})
    items.sort(key=lambda a: abs(a["z"]), reverse=True)
    return {"status": "quiet", "checked": 5, "threshold": 3.0, "window_h": 6, "items": items}


def _open_incident(*keys, t=1_000_000):
    if not keys:
        keys = ("gpu_util", "gpu_power", "gpu_temp")
    return app.evaluate_incidents(_bundle(*keys), t)


def _seed_cause(iid, text="GPU utilisation and power spiked together. Suggested next step: check the busiest container.",
                at=1_000_500):
    with app.LOCK:
        app.DB.execute("UPDATE incidents SET ai_explanation=?, ai_explained_at=?, ai_model=? WHERE id=?",
                       (text, at, "gemma3:1b", iid))
        app.DB.commit()


class _CallCounter:
    """A drop-in _ollama_generate replacement that must NEVER be invoked on the
    dispatch/enrichment path. Records call count so tests can assert zero."""
    def __init__(self):
        self.n = 0

    def __call__(self, prompt, timeout=None, capture=None, fmt=None):
        self.n += 1
        return None, "unreachable"


class TestMigration(unittest.TestCase):
    def test_dedup_column_present_and_idempotent(self):
        app._apply_schema_migrations(app.DB)
        app._apply_schema_migrations(app.DB)
        cols = {r[1] for r in app.DB.execute("PRAGMA table_info(incidents)").fetchall()}
        self.assertIn("ai_cause_notified_at", cols)


class TestDefaultOff(unittest.TestCase):
    def setUp(self):
        _clean()

    def test_setting_default_off(self):
        self.assertEqual(app.SETTING_DEFAULTS["notify_ai_cause"], "0")
        self.assertEqual(app.get_settings()["notify_ai_cause"], "0")

    def test_cause_line_empty_when_off(self):
        iid = _open_incident()
        _seed_cause(iid)
        inc = app.get_incident(iid)
        # opt-in OFF (default) → no line even though a cause is cached
        self.assertEqual(app._incident_cause_line(inc), "")

    def test_cause_line_present_when_on(self):
        app.save_settings({"notify_ai_cause": "1"})
        iid = _open_incident()
        _seed_cause(iid)
        inc = app.get_incident(iid)
        line = app._incident_cause_line(inc)
        self.assertTrue(line.startswith("🧠 Probable cause:"))
        self.assertIn("spiked", line)

    def test_cause_line_empty_when_no_cache(self):
        app.save_settings({"notify_ai_cause": "1"})
        iid = _open_incident()   # no explanation seeded
        self.assertEqual(app._incident_cause_line(app.get_incident(iid)), "")


class TestPrimaryEnrichmentDispatchZeroLLM(unittest.TestCase):
    """The rule engine's incident notification gains the cause line when opted in —
    and the whole dispatch makes ZERO ollama calls even while it appends it."""

    def setUp(self):
        _clean()
        app.save_settings({"discord_webhook_url": "", "telegram_token": "",
                           "telegram_chat_id": "", "webhook_url": "", "ntfy_topic": "hlm-test"})
        self._gen = app._ollama_generate
        self.spy = _CallCounter()
        app._ollama_generate = self.spy
        app.create_rule({"name": "inc", "ctype": "incident",
                         "params": {"severity": "warning"}, "enabled": True, "cooldown_min": 0})
        self.iid = _open_incident()

    def tearDown(self):
        app._ollama_generate = self._gen
        _clean()

    def _sig(self):
        return {"anomalies": {"items": []}, "incidents": app.list_incidents()}

    def _fire_and_get_detail(self):
        with patch("app._post_text", return_value=(200, b"")) as pt:
            n = app.evaluate_rules(self._sig())
        self.assertEqual(n, 1)
        sent = [h for h in app.list_alert_history() if h["status"] == "sent"][0]
        # ntfy body = the second positional arg to _post_text
        body = pt.call_args[0][1]
        return sent["detail"], body

    def test_cause_appended_when_on_and_cached_zero_llm(self):
        app.save_settings({"notify_ai_cause": "1"})
        _seed_cause(self.iid)
        detail, body = self._fire_and_get_detail()
        self.assertIn("🧠 Probable cause:", detail)
        self.assertIn("spiked", detail)
        self.assertIn("🧠 Probable cause:", body)
        self.assertEqual(self.spy.n, 0)   # DISPATCH made zero LLM calls

    def test_absent_when_setting_off(self):
        # opt-in OFF (default) but a cause IS cached → notification byte-identical
        _seed_cause(self.iid)
        detail, body = self._fire_and_get_detail()
        self.assertNotIn("Probable cause", detail)
        self.assertNotIn("Probable cause", body)
        self.assertEqual(self.spy.n, 0)

    def test_absent_when_nothing_cached(self):
        app.save_settings({"notify_ai_cause": "1"})
        # no explanation seeded → graceful: normal notification, no AI line
        detail, body = self._fire_and_get_detail()
        self.assertNotIn("Probable cause", detail)
        self.assertEqual(self.spy.n, 0)

    def test_no_leak_credentials_redacted(self):
        app.save_settings({"notify_ai_cause": "1"})
        _seed_cause(self.iid, text="Cause traced to https://admin:hunter2@backup.lan/api spiking.")
        detail, body = self._fire_and_get_detail()
        self.assertNotIn("hunter2", detail)
        self.assertNotIn("admin:hunter2", body)
        self.assertIn("***:***", detail)
        self.assertEqual(self.spy.n, 0)


class TestSupplementarySend(unittest.TestCase):
    """The off-poll supplementary cause notification: opt-in, still-open, dedup'd,
    suppression-respecting, and LLM-free."""

    def setUp(self):
        _clean()
        app.save_settings({"discord_webhook_url": "", "telegram_token": "",
                           "telegram_chat_id": "", "webhook_url": "", "ntfy_topic": "hlm-test",
                           "notify_ai_cause": "1", "incident_auto_explain": "1"})
        self._gen = app._ollama_generate
        self.spy = _CallCounter()
        app._ollama_generate = self.spy
        self.iid = _open_incident()
        _seed_cause(self.iid)

    def tearDown(self):
        app._ollama_generate = self._gen
        _clean()

    def test_sends_once_zero_llm_and_marks_flag(self):
        with patch("app._post_text", return_value=(200, b"")) as pt:
            app._maybe_send_incident_cause(self.iid)
        self.assertEqual(pt.call_count, 1)
        self.assertIn("🧠 Probable cause:", pt.call_args[0][1])
        self.assertEqual(self.spy.n, 0)   # supplementary send made zero LLM calls
        flag = app.DB.execute("SELECT ai_cause_notified_at FROM incidents WHERE id=?",
                              (self.iid,)).fetchone()[0]
        self.assertIsNotNone(flag)

    def test_dedup_at_most_once(self):
        with patch("app._post_text", return_value=(200, b"")) as pt:
            app._maybe_send_incident_cause(self.iid)
            app._maybe_send_incident_cause(self.iid)
            app._maybe_send_incident_cause(self.iid)
        self.assertEqual(pt.call_count, 1)   # AT MOST ONCE

    def test_suppressed_when_opt_in_off(self):
        app.save_settings({"notify_ai_cause": "0"})
        with patch("app._post_text", return_value=(200, b"")) as pt:
            app._maybe_send_incident_cause(self.iid)
        pt.assert_not_called()

    def test_suppressed_when_auto_explain_off(self):
        app.save_settings({"incident_auto_explain": "0"})
        with patch("app._post_text", return_value=(200, b"")) as pt:
            app._maybe_send_incident_cause(self.iid)
        pt.assert_not_called()

    def test_suppressed_when_not_open(self):
        # drive the incident to cleared, then the supplementary must not send
        for k in range(app._INCIDENT_CLEAR_CONFIRM + 1):
            app.evaluate_incidents(_bundle(), 1_000_100 + k * 20)
        self.assertEqual(app.get_incident(self.iid)["state"], "cleared")
        with patch("app._post_text", return_value=(200, b"")) as pt:
            app._maybe_send_incident_cause(self.iid)
        pt.assert_not_called()

    def test_suppressed_by_maintenance_window(self):
        with patch("app._in_maintenance", return_value=(True, 9_999_999)), \
             patch("app._post_text", return_value=(200, b"")) as pt:
            app._maybe_send_incident_cause(self.iid)
        pt.assert_not_called()
        # not claimed → a later (non-maintenance) pass can still send exactly once
        flag = app.DB.execute("SELECT ai_cause_notified_at FROM incidents WHERE id=?",
                              (self.iid,)).fetchone()[0]
        self.assertIsNone(flag)

    def test_suppressed_when_no_channel(self):
        app.save_settings({"ntfy_topic": ""})   # no channel configured
        with patch("app._post_text", return_value=(200, b"")) as pt:
            app._maybe_send_incident_cause(self.iid)
        pt.assert_not_called()

    def test_suppressed_when_no_cache(self):
        with app.LOCK:
            app.DB.execute("UPDATE incidents SET ai_explanation=NULL WHERE id=?", (self.iid,))
            app.DB.commit()
        with patch("app._post_text", return_value=(200, b"")) as pt:
            app._maybe_send_incident_cause(self.iid)
        pt.assert_not_called()

    def test_no_leak_in_supplementary_body(self):
        _seed_cause(self.iid, text="Root cause at https://ops:s3cr3t@nas.lan/vol0.")
        with patch("app._post_text", return_value=(200, b"")) as pt:
            app._maybe_send_incident_cause(self.iid)
        body = pt.call_args[0][1]
        self.assertNotIn("s3cr3t", body)
        self.assertIn("***:***", body)


class TestWorkerWiringOffPoll(unittest.TestCase):
    """The supplementary send is triggered ONLY from the dedicated worker's unit of
    work (generate + supplementary), never from evaluate_incidents/the poll path."""

    def setUp(self):
        _clean()
        app.save_settings({"ntfy_topic": "hlm-test",
                           "notify_ai_cause": "1", "incident_auto_explain": "1"})

    def tearDown(self):
        _clean()

    def test_evaluate_incidents_does_not_send_cause(self):
        # Opening/extending an incident on the poll path must not itself send the
        # supplementary notification — only the worker does.
        with patch("app._maybe_send_incident_cause") as send, \
             patch("app._post_text", return_value=(200, b"")):
            _open_incident()
        send.assert_not_called()
