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


class TestAlertHostLabel(unittest.TestCase):
    """Every alert must name the machine it's about (many-hosts UX)."""

    def test_label_prefers_probe_hostname(self):
        with patch.object(app, "LATEST", {"host": {"hostname": "ardi"}}):
            self.assertEqual(app._alert_host_label(), "ardi")

    def test_label_falls_back_to_socket(self):
        with patch.object(app, "LATEST", {}), \
             patch("socket.gethostname", return_value="hubbox"):
            self.assertEqual(app._alert_host_label(), "hubbox")

    @patch("app._post_json")
    def test_dispatch_prefixes_title_with_host(self, mock_post):
        mock_post.return_value = (200, b"{}")
        s = {**app.SETTING_DEFAULTS, "telegram_token": "tok", "telegram_chat_id": "99"}
        with patch.object(app, "_alert_host_label", return_value="ardi"):
            app.dispatch_alert(s, "warning", "Container immich unhealthy", "down")
        _, payload = mock_post.call_args[0]
        self.assertIn("[ardi]", payload["text"])
        self.assertIn("Container immich unhealthy", payload["text"])

    @patch("app._post_json")
    def test_dispatch_explicit_host_overrides(self, mock_post):
        mock_post.return_value = (200, b"{}")
        s = {**app.SETTING_DEFAULTS, "telegram_token": "tok", "telegram_chat_id": "99"}
        app.dispatch_alert(s, "info", "Title", "Body", host="webserver")
        _, payload = mock_post.call_args[0]
        self.assertIn("[webserver]", payload["text"])

    @patch("app._post_json")
    def test_dispatch_empty_host_opts_out(self, mock_post):
        mock_post.return_value = (200, b"{}")
        s = {**app.SETTING_DEFAULTS, "telegram_token": "tok", "telegram_chat_id": "99"}
        app.dispatch_alert(s, "info", "Title", "Body", host="")
        _, payload = mock_post.call_args[0]
        self.assertNotIn("[", payload["text"].split("\n")[0])


class TestOutboundUserAgent(unittest.TestCase):
    """Discord sits behind Cloudflare, which 403s the default Python-urllib
    agent (error 1010). Every outbound notifier POST must carry a real
    User-Agent header. Regression guard for the webhook-test 403."""

    def _capture(self, fn):
        captured = {}

        class _Resp:
            status = 204
            def read(self): return b""
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def _fake_urlopen(req, timeout=None):
            captured["req"] = req
            return _Resp()

        with patch("urllib.request.urlopen", _fake_urlopen):
            fn()
        return captured["req"]

    def test_post_json_sets_user_agent(self):
        req = self._capture(lambda: app._post_json("https://discord.com/api/webhooks/1/x", {"a": 1}))
        self.assertEqual(req.get_header("User-agent"), app.NOTIFY_USER_AGENT)

    def test_post_text_sets_user_agent(self):
        req = self._capture(lambda: app._post_text("https://ntfy.sh/topic", "hi"))
        self.assertEqual(req.get_header("User-agent"), app.NOTIFY_USER_AGENT)

    def test_post_text_preserves_caller_headers_and_adds_ua(self):
        req = self._capture(lambda: app._post_text(
            "https://ntfy.sh/topic", "hi", headers={"Title": "T", "Priority": "5"}))
        self.assertEqual(req.get_header("Title"), "T")
        self.assertEqual(req.get_header("User-agent"), app.NOTIFY_USER_AGENT)

    def test_user_agent_is_non_default(self):
        self.assertIn("homelab-monitor", app.NOTIFY_USER_AGENT)
        self.assertNotIn("Python-urllib", app.NOTIFY_USER_AGENT)


if __name__ == "__main__":
    unittest.main()
