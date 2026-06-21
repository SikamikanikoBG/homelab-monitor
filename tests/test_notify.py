"""Unit tests for alert dispatch and Telegram notifier (issue #27)."""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


class TestTelegramSettings(unittest.TestCase):
    def test_public_settings_masks_telegram_token(self):
        with patch.object(app, "get_settings", return_value={
            **app.SETTING_DEFAULTS,
            "telegram_token": "123456:SECRET",
            "telegram_chat_id": "-10099",
        }):
            pub = app._public_settings()
        self.assertNotIn("telegram_token", pub)
        self.assertTrue(pub["telegram_token_set"])
        self.assertEqual(pub["telegram_chat_id"], "-10099")

    def test_telegram_defaults_present(self):
        self.assertIn("telegram_token", app.SETTING_DEFAULTS)
        self.assertIn("telegram_chat_id", app.SETTING_DEFAULTS)
        self.assertIn("telegram_token", app.SETTING_SECRETS)


class TestWebhookSecret(unittest.TestCase):
    """Generic webhook URL embeds secrets (Slack/n8n/HA) — must be redacted
    like the Discord webhook, with a *_set boolean and CLEAR/keep round-trip."""

    def setUp(self):
        # Start from a known-clean stored state.
        app.save_settings({"webhook_url": "", "discord_webhook_url": ""})

    def tearDown(self):
        app.save_settings({"webhook_url": ""})

    def test_webhook_url_is_a_secret(self):
        self.assertIn("webhook_url", app.SETTING_SECRETS)

    def test_public_settings_masks_webhook_url(self):
        with patch.object(app, "get_settings", return_value={
            **app.SETTING_DEFAULTS,
            "webhook_url": "https://hooks.slack.com/services/T000/B000/SECRET",
        }):
            pub = app._public_settings()
        self.assertNotIn("webhook_url", pub)
        self.assertTrue(pub["webhook_url_set"])

    def test_api_settings_does_not_leak_raw_url(self):
        c = app.app.test_client()
        url = "https://hooks.slack.com/services/T000/B000/SECRETVALUE"
        c.post("/api/settings", json={"webhook_url": url})
        j = c.get("/api/settings").get_json()
        s = j["settings"]
        self.assertTrue(s["webhook_url_set"])
        self.assertNotIn("webhook_url", s)
        self.assertNotIn("SECRETVALUE", c.get("/api/settings").get_data(as_text=True))
        # …but it is actually stored for dispatch (assembly path intact).
        self.assertEqual(app.get_settings().get("webhook_url"), url)

    def test_other_saves_do_not_wipe_stored_webhook(self):
        c = app.app.test_client()
        url = "https://example.test/hook"
        c.post("/api/settings", json={"webhook_url": url})
        # A save that omits webhook_url (the keep case) must leave it intact.
        c.post("/api/settings", json={"alert_min_level": "critical"})
        self.assertEqual(app.get_settings().get("webhook_url"), url)

    def test_clear_sentinel_clears_webhook(self):
        c = app.app.test_client()
        c.post("/api/settings", json={"webhook_url": "https://example.test/hook"})
        self.assertTrue(app.get_settings().get("webhook_url"))
        # The UI translates the CLEAR sentinel into an empty string.
        c.post("/api/settings", json={"webhook_url": ""})
        self.assertEqual(app.get_settings().get("webhook_url"), "")


class TestTelegramNotifier(unittest.TestCase):
    @patch("app._post_json")
    def test_post_to_telegram_uses_markdown(self, mock_post):
        mock_post.return_value = (200, b"{}")
        app._post_to_telegram("tok", "12345", "warning", "Disk full", "Root at 95%")

        url, payload = mock_post.call_args[0]
        self.assertEqual(url, "https://api.telegram.org/bottok/sendMessage")
        self.assertEqual(payload["chat_id"], "12345")
        self.assertEqual(payload["parse_mode"], "Markdown")
        self.assertIn("Disk full", payload["text"])
        self.assertIn("Root at 95%", payload["text"])
        self.assertIn("warning", payload["text"])

    @patch("app._post_json")
    def test_tg_escape_special_chars(self, mock_post):
        mock_post.return_value = (200, b"{}")
        app._post_to_telegram("tok", "1", "info", "a*b_c", "d`e[f")

        _, payload = mock_post.call_args[0]
        self.assertIn(r"a\*b\_c", payload["text"])
        self.assertIn(r"d\`e\[f", payload["text"])

    @patch("app._post_json")
    def test_dispatch_includes_telegram_when_configured(self, mock_post):
        mock_post.return_value = (200, b"{}")
        s = {
            **app.SETTING_DEFAULTS,
            "telegram_token": "tok",
            "telegram_chat_id": "99",
        }
        results = app.dispatch_alert(s, "info", "Title", "Body")
        channels = [c for c, ok, _ in results]
        self.assertIn("telegram", channels)
        self.assertTrue(all(ok for _, ok, _ in results))

    @patch("app._post_json")
    def test_dispatch_skips_telegram_without_chat_id(self, mock_post):
        s = {**app.SETTING_DEFAULTS, "telegram_token": "tok", "telegram_chat_id": ""}
        results = app.dispatch_alert(s, "info", "Title", "Body")
        channels = [c for c, _, _ in results]
        self.assertNotIn("telegram", channels)
        mock_post.assert_not_called()

    @patch("app._post_json")
    def test_dispatch_reports_telegram_errors(self, mock_post):
        mock_post.side_effect = RuntimeError("network down")
        s = {
            **app.SETTING_DEFAULTS,
            "telegram_token": "tok",
            "telegram_chat_id": "99",
        }
        results = app.dispatch_alert(s, "info", "Title", "Body")
        tg = [r for r in results if r[0] == "telegram"]
        self.assertEqual(len(tg), 1)
        self.assertFalse(tg[0][1])
        self.assertIn("network down", tg[0][2])


class TestOutboundUserAgent(unittest.TestCase):
    """Discord behind Cloudflare 403s requests with no User-Agent; every
    outbound notification helper must carry one (matches the human's fix)."""

    def _capture(self, fn):
        captured = {}

        class _Resp:
            status = 200
            def read(self): return b"{}"
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(req, timeout=None):
            captured["req"] = req
            return _Resp()

        with patch("app.urllib.request.urlopen", side_effect=fake_urlopen):
            fn()
        return captured["req"]

    def test_post_json_sets_user_agent(self):
        req = self._capture(lambda: app._post_json("https://example.test/h", {"a": 1}))
        self.assertEqual(req.get_header("User-agent"), app._NOTIFY_UA)
        self.assertIn(app.VERSION, app._NOTIFY_UA)

    def test_post_text_sets_user_agent_even_with_custom_headers(self):
        # ntfy passes its own header dict (no UA) — the helper must inject one.
        req = self._capture(lambda: app._post_text(
            "https://ntfy.sh/topic", "body",
            {"Content-Type": "text/plain; charset=utf-8", "Title": "x"}))
        self.assertEqual(req.get_header("User-agent"), app._NOTIFY_UA)
        self.assertEqual(req.get_header("Title"), "x")  # caller headers preserved


if __name__ == "__main__":
    unittest.main()
