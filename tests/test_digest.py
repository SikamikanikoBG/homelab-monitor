"""Unit tests for the scheduled Lab Copilot digest (E1).

Covers the non-LLM logic (no ollama needed in CI):
  • build_digest degrades to the deterministic facts when the LLM is off, never
    empty, never raises
  • the once-per-day edge-trigger (_digest_due): inert when disabled / no channel,
    fires only at/after the target local time, never twice the same day
  • maybe_send_digest stamps the date latch (no double-send), no-ops when disabled
  • the manual send path (send_digest + the endpoint): clean 200/400, never 500
  • settings round-trip for the schedule config, including secret-keep semantics
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


def _local_min_now():
    lt = time.localtime()
    return lt.tm_hour * 60 + lt.tm_min


class _SettingsBase(unittest.TestCase):
    """Snapshot/restore the digest-related settings + COPILOT_ENABLED so tests
    don't leak into each other or the real DB."""
    KEYS = ("digest_enabled", "digest_time", "digest_channel", "digest_last_sent",
            "digest_cadence", "digest_weekday",
            "discord_webhook_url", "ntfy_topic", "webhook_url",
            "telegram_token", "telegram_chat_id")

    def setUp(self):
        self._saved = {k: app.get_settings().get(k, "") for k in self.KEYS}
        self._en = app.COPILOT_ENABLED
        app.COPILOT_ENABLED = False   # force the deterministic no-LLM path

    def tearDown(self):
        app.COPILOT_ENABLED = self._en
        app.save_settings(self._saved)


class TestBuildDigest(_SettingsBase):
    def test_degrades_to_facts_non_empty(self):
        title, body, status = app.build_digest()
        self.assertEqual(title, app.DIGEST_TITLE)
        self.assertTrue(body.strip())            # never empty
        self.assertEqual(status, "disabled")     # LLM off -> fact fallback

    def test_never_raises(self):
        # even with no metrics the builder must produce something
        title, body, status = app.build_digest()
        self.assertIsInstance(body, str)
        self.assertTrue(body)


class TestDigestDue(_SettingsBase):
    def _cfg(self, **kw):
        base = {"digest_enabled": "1", "digest_channel": "all",
                "digest_time": "00:00", "digest_last_sent": "",
                "ntfy_topic": "lab"}          # one configured channel
        base.update(kw)
        app.save_settings(base)
        return app.get_settings()

    def test_inert_when_disabled(self):
        s = self._cfg(digest_enabled="0")
        self.assertFalse(app._digest_due(s))

    def test_inert_when_no_channel(self):
        s = self._cfg(ntfy_topic="")          # clears the only channel
        self.assertFalse(app._digest_due(s))

    def test_fires_after_target_time(self):
        # target far in the past today -> due
        s = self._cfg(digest_time="00:00")
        self.assertTrue(app._digest_due(s))

    def test_not_due_before_target_time(self):
        # target one minute in the future -> not yet
        future = _local_min_now() + 1
        if future >= 24 * 60:
            self.skipTest("running at 23:59 local — skip the future-minute case")
        hh, mm = divmod(future, 60)
        s = self._cfg(digest_time="%02d:%02d" % (hh, mm))
        self.assertFalse(app._digest_due(s))

    def test_not_due_if_already_sent_today(self):
        today = time.strftime("%Y-%m-%d", time.localtime())
        s = self._cfg(digest_time="00:00", digest_last_sent=today)
        self.assertFalse(app._digest_due(s))

    def test_due_again_after_a_prior_day(self):
        s = self._cfg(digest_time="00:00", digest_last_sent="2000-01-01")
        self.assertTrue(app._digest_due(s))

    def test_bad_time_falls_back(self):
        # garbage time must not raise; defaults to 08:00 internally
        s = self._cfg(digest_time="not-a-time")
        # just assert it returns a bool without raising
        self.assertIn(app._digest_due(s), (True, False))


class TestMaybeSend(_SettingsBase):
    def test_noop_when_disabled(self):
        app.save_settings({"digest_enabled": "0", "ntfy_topic": "lab",
                           "digest_last_sent": ""})
        self.assertFalse(app.maybe_send_digest())
        # latch untouched
        self.assertEqual(app.get_settings().get("digest_last_sent"), "")

    def test_stamps_latch_once_per_day(self):
        # configure a webhook that points at a dead port so dispatch fails fast
        # but maybe_send_digest still latches the date (edge-trigger discipline).
        app.save_settings({"digest_enabled": "1", "digest_channel": "webhook",
                           "digest_time": "00:00", "digest_last_sent": "",
                           "webhook_url": "http://127.0.0.1:1/x"})
        today = time.strftime("%Y-%m-%d", time.localtime())
        fired = app.maybe_send_digest()
        self.assertTrue(fired)
        self.assertEqual(app.get_settings().get("digest_last_sent"), today)
        # second pass the same day must NOT fire again (no double-send)
        self.assertFalse(app.maybe_send_digest())


class TestSendDigest(_SettingsBase):
    def test_no_channel_configured(self):
        app.save_settings({"digest_channel": "all", "ntfy_topic": "",
                           "discord_webhook_url": "", "webhook_url": "",
                           "telegram_token": "", "telegram_chat_id": ""})
        out = app.send_digest(channel="all")
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "No channel configured.")
        self.assertEqual(out["results"], [])

    def test_unknown_channel(self):
        out = app.send_digest(channel="carrier-pigeon")
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "Unknown channel.")

    def test_requested_channel_not_configured(self):
        app.save_settings({"discord_webhook_url": ""})
        out = app.send_digest(channel="discord")
        self.assertFalse(out["ok"])
        self.assertIn("not configured", out["reason"].lower())

    def test_send_attempts_configured_channel(self):
        # dead port -> dispatch returns ok=False but we still get a result row
        # and a graceful (non-raising) outcome.
        app.save_settings({"webhook_url": "http://127.0.0.1:1/x"})
        out = app.send_digest(channel="webhook", record=False)
        self.assertEqual(len(out["results"]), 1)
        self.assertEqual(out["results"][0]["channel"], "webhook")
        self.assertFalse(out["results"][0]["ok"])     # nothing listening
        self.assertIn("llm_status", out)


class TestSendEndpoint(_SettingsBase):
    def setUp(self):
        super().setUp()
        self.c = app.app.test_client()

    def test_no_channel_clean_400_not_500(self):
        app.save_settings({"digest_channel": "all", "ntfy_topic": "",
                           "discord_webhook_url": "", "webhook_url": "",
                           "telegram_token": "", "telegram_chat_id": ""})
        r = self.c.post("/api/copilot/digest/send", json={"channel": "all"})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.get_json()["ok"])

    def test_bad_payload_no_500(self):
        app.save_settings({"ntfy_topic": "", "discord_webhook_url": "",
                           "webhook_url": "", "telegram_token": "",
                           "telegram_chat_id": ""})
        r = self.c.post("/api/copilot/digest/send", data="not json",
                        content_type="application/json")
        self.assertIn(r.status_code, (200, 400))   # never 500


class TestSettingsRoundTrip(_SettingsBase):
    def setUp(self):
        super().setUp()
        self.c = app.app.test_client()

    def test_config_round_trip(self):
        r = self.c.post("/api/settings", json={
            "digest_enabled": "1", "digest_time": "07:30", "digest_channel": "ntfy"})
        self.assertEqual(r.status_code, 200)
        s = r.get_json()["settings"]
        self.assertEqual(s["digest_enabled"], "1")
        self.assertEqual(s["digest_time"], "07:30")
        self.assertEqual(s["digest_channel"], "ntfy")

    def test_secret_keep_on_partial_post(self):
        # set a secret, then POST only digest config: the secret must survive
        app.save_settings({"discord_webhook_url": "https://discord.test/webhook/x"})
        self.c.post("/api/settings", json={"digest_time": "09:15"})
        self.assertEqual(app.get_settings().get("discord_webhook_url"),
                         "https://discord.test/webhook/x")
        # and the public view never reveals the secret value
        pub = self.c.post("/api/settings", json={}).get_json()["settings"]
        self.assertNotIn("discord_webhook_url", pub)
        self.assertTrue(pub.get("discord_webhook_url_set"))


_FAKE_SIG = {
    "disk": [{"mount": "/backup", "pct": 88, "eta_days": 6.0,
              "gb_per_day": 12.0, "free_gb": 70.0, "status": "filling"}],
    "vram": {"free_gb": 1.2, "total_gb": 24.0, "models_gb": 22.0, "status": "ok"},
    "cost_month": {"enabled": True, "currency": "€", "month_to_date": 4.20,
                   "projected_month": 12.50, "delta_pct": 15, "last_month": 10.0},
    "anomalies": {"status": "flagged", "items": [
        {"key": "gpu_power", "direction": "spike", "value": 310, "unit": "W",
         "baseline": 120, "z": 4.1}]},
    "incidents": {"open": 2, "top": {"severity": "critical", "active_count": 3}},
    "uptime": [
        {"id": 1, "enabled": True, "state": "down", "label": "chroma", "uptime": 40.0,
         "window_total": 10, "last_code": 503, "last_err": "boom"},
        {"id": 2, "enabled": True, "state": "up", "label": "ollama", "uptime": 99.9,
         "window_total": 10}],
    "ooms": [], "image_updates": {},
}


class TestDigestSections(_SettingsBase):
    """The structured deterministic body — sections present, sane, secret-free."""

    def setUp(self):
        super().setUp()
        self._sig = app._reco_signals
        app._reco_signals = lambda now=None: dict(_FAKE_SIG)

    def tearDown(self):
        app._reco_signals = self._sig
        super().tearDown()

    def test_sections_present_and_sane(self):
        secs = dict(app._digest_sections(now=1_700_000_000))
        for h in ("Needs attention", "Capacity", "Cost", "Fleet / Uptime", "Anomalies"):
            self.assertIn(h, secs)
        # Needs attention surfaces the down check + open incidents
        attn = "\n".join(secs["Needs attention"])
        self.assertIn("chroma", attn)
        self.assertIn("Incidents", attn)
        # Capacity shows the disk-fill ETA nudge
        self.assertIn("/backup", "\n".join(secs["Capacity"]))
        # Cost shows MTD + projected + delta
        cost = "\n".join(secs["Cost"])
        self.assertIn("4.2", cost)
        self.assertIn("12.5", cost)
        # Anomalies surfaces the flagged series
        self.assertIn("gpu_power", "\n".join(secs["Anomalies"]))

    def test_llm_down_structured_body_still_produced(self):
        # COPILOT_ENABLED=False in the base => no narrative; body must still carry
        # the deterministic sections and never be empty.
        title, body, status = app.build_digest(now=1_700_000_000)
        self.assertTrue(body.strip())
        self.assertNotIn("Summary", body)        # no narrative line when LLM off
        for h in ("Needs attention", "Capacity", "Cost", "Anomalies"):
            self.assertIn(h, body)
        self.assertIn(status, ("disabled", "facts", "ok"))

    def test_narrative_leads_when_llm_up(self):
        orig = app._ollama_generate
        app._ollama_generate = lambda prompt, timeout=None: ("All quiet on the rig.", None)
        try:
            title, body, status = app.build_digest(now=1_700_000_000)
        finally:
            app._ollama_generate = orig
        self.assertEqual(status, "ok")
        self.assertTrue(body.startswith("Summary"))
        self.assertIn("All quiet on the rig.", body)
        self.assertIn("Capacity", body)          # sections still follow

    def test_body_carries_no_secrets(self):
        # plant secrets in settings; they must never appear in the body.
        app.save_settings({"discord_webhook_url": "https://discord.test/webhook/SECRET123",
                           "webhook_url": "https://hook.test/TOKENXYZ"})
        _t, body, _s = app.build_digest(now=1_700_000_000)
        self.assertNotIn("SECRET123", body)
        self.assertNotIn("TOKENXYZ", body)
        self.assertNotIn("discord.test", body)
        self.assertNotIn("hook.test", body)


class TestWeeklyDue(_SettingsBase):
    def _cfg(self, **kw):
        base = {"digest_enabled": "1", "digest_channel": "all",
                "digest_time": "00:00", "digest_last_sent": "",
                "digest_cadence": "weekly", "ntfy_topic": "lab"}
        base.update(kw)
        app.save_settings(base)
        return app.get_settings()

    def _now_for_wday(self, wday):
        """A unix ts whose LOCAL weekday is `wday`, at 12:00 local (well past 00:00)."""
        base = int(time.time())
        lt = time.localtime(base)
        # step day-by-day until local weekday matches
        for d in range(8):
            cand = base + d * 86400
            if time.localtime(cand).tm_wday == wday:
                clt = time.localtime(cand)
                noon = time.mktime((clt.tm_year, clt.tm_mon, clt.tm_mday,
                                    12, 0, 0, 0, 0, -1))
                return int(noon)
        return base

    def test_fires_only_on_configured_weekday(self):
        s = self._cfg(digest_weekday="2")          # Wednesday
        wed = self._now_for_wday(2)
        thu = self._now_for_wday(3)
        self.assertTrue(app._digest_due(s, now=wed))
        self.assertFalse(app._digest_due(s, now=thu))

    def test_weekly_latch_no_double_send(self):
        s = self._cfg(digest_weekday="2")
        wed = self._now_for_wday(2)
        self.assertTrue(app._digest_due(s, now=wed))
        today = time.strftime("%Y-%m-%d", time.localtime(wed))
        s2 = self._cfg(digest_weekday="2", digest_last_sent=today)
        self.assertFalse(app._digest_due(s2, now=wed))   # already sent that day

    def test_weekly_not_due_before_time(self):
        # target far in the future today, on the right weekday -> not yet
        wday = time.localtime().tm_wday
        s = self._cfg(digest_weekday=str(wday), digest_time="23:59")
        # use a now early in that day
        clt = time.localtime()
        early = int(time.mktime((clt.tm_year, clt.tm_mon, clt.tm_mday, 0, 1, 0, 0, 0, -1)))
        self.assertFalse(app._digest_due(s, now=early))

    def test_daily_cadence_unchanged(self):
        s = self._cfg(digest_cadence="daily", digest_time="00:00")
        self.assertTrue(app._digest_due(s))          # daily fires regardless of weekday

    def test_cadence_weekday_round_trip(self):
        c = app.app.test_client()
        r = c.post("/api/settings", json={"digest_cadence": "weekly", "digest_weekday": "5"})
        self.assertEqual(r.status_code, 200)
        s = r.get_json()["settings"]
        self.assertEqual(s["digest_cadence"], "weekly")
        self.assertEqual(s["digest_weekday"], "5")


class TestSendNowRicher(_SettingsBase):
    """Manual send-now produces the new richer body (sections present)."""

    def setUp(self):
        super().setUp()
        self._sig = app._reco_signals
        app._reco_signals = lambda now=None: dict(_FAKE_SIG)
        self._disp = app.dispatch_alert
        self.captured = {}

        def _cap(s, level, title, body, channel=None):
            self.captured["title"] = title
            self.captured["body"] = body
            return [(channel or "ntfy", True, None)]
        app.dispatch_alert = _cap

    def tearDown(self):
        app._reco_signals = self._sig
        app.dispatch_alert = self._disp
        super().tearDown()

    def test_send_now_richer_body(self):
        app.save_settings({"ntfy_topic": "lab"})
        out = app.send_digest(channel="ntfy", record=False)
        self.assertTrue(out["ok"])
        body = self.captured["body"]
        for h in ("Needs attention", "Capacity", "Cost"):
            self.assertIn(h, body)


if __name__ == "__main__":
    unittest.main()
