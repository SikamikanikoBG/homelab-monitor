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


if __name__ == "__main__":
    unittest.main()
