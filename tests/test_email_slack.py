"""Unit tests for the Email (SMTP) and Slack alert channels.

Both flow through the existing dispatch_alert / _configured_channels engine so
they inherit rules, recovery, maintenance suppression and the per-channel test
path automatically. All network/SMTP is mocked — nothing leaves the box.
"""
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


# ── Slack ────────────────────────────────────────────────────────────────────
class TestSlackSender(unittest.TestCase):
    @patch("app._post_json")
    def test_send_slack_posts_text_and_coloured_attachment(self, mock_post):
        mock_post.return_value = (200, b"ok")
        app.send_slack("https://hooks.slack.com/services/X", "critical",
                       "Disk full", "Root at 95%")
        url, payload = mock_post.call_args[0]
        self.assertEqual(url, "https://hooks.slack.com/services/X")
        self.assertIn("Disk full", payload["text"])
        self.assertIn("Root at 95%", payload["text"])
        att = payload["attachments"][0]
        self.assertEqual(att["color"], app._SLACK_COLORS["critical"])
        self.assertEqual(att["title"], "Disk full")
        self.assertEqual(att["text"], "Root at 95%")

    @patch("app._post_json")
    def test_dispatch_includes_slack_when_configured(self, mock_post):
        mock_post.return_value = (200, b"ok")
        s = {**app.SETTING_DEFAULTS, "slack_webhook_url": "https://hooks.slack.com/x"}
        results = app.dispatch_alert(s, "info", "T", "B")
        self.assertIn("slack", [c for c, _, _ in results])
        self.assertTrue(all(ok for _, ok, _ in results))

    @patch("app._post_json")
    def test_dispatch_skips_slack_when_unset(self, mock_post):
        s = {**app.SETTING_DEFAULTS, "slack_webhook_url": ""}
        results = app.dispatch_alert(s, "info", "T", "B")
        self.assertNotIn("slack", [c for c, _, _ in results])

    @patch("app._post_json", side_effect=RuntimeError("network down"))
    def test_slack_error_returns_not_raise(self, _mock):
        s = {**app.SETTING_DEFAULTS, "slack_webhook_url": "https://hooks.slack.com/x"}
        results = app.dispatch_alert(s, "info", "T", "B", channel="slack")
        self.assertEqual(len(results), 1)
        ch, ok, err = results[0]
        self.assertEqual(ch, "slack")
        self.assertFalse(ok)
        self.assertIn("network down", err)

    def test_slack_url_is_a_secret_and_masked(self):
        self.assertIn("slack_webhook_url", app.SETTING_SECRETS)
        with patch.object(app, "get_settings", return_value={
            **app.SETTING_DEFAULTS,
            "slack_webhook_url": "https://hooks.slack.com/services/T/B/SECRET",
        }):
            pub = app._public_settings()
        self.assertNotIn("slack_webhook_url", pub)
        self.assertTrue(pub["slack_webhook_url_set"])


# ── Email (SMTP) ───────────────────────────────────────────────────────────────
class TestEmailSender(unittest.TestCase):
    @patch("app.smtplib.SMTP")
    def test_send_email_builds_message_and_uses_starttls_and_login(self, mock_smtp):
        srv = MagicMock()
        mock_smtp.return_value.__enter__.return_value = srv
        ok, err = app.send_email(
            "Subject here", "Body line", "warning",
            host="smtp.example.com", port="587", user="u", password="p",
            sender="from@example.com", to="a@example.com, b@example.com", tls=True)
        self.assertTrue(ok)
        self.assertIsNone(err)
        mock_smtp.assert_called_once()
        self.assertEqual(mock_smtp.call_args[0][0], "smtp.example.com")
        self.assertEqual(mock_smtp.call_args[0][1], 587)
        srv.starttls.assert_called_once()
        srv.login.assert_called_once_with("u", "p")
        # send_message called with the right envelope.
        msg = srv.send_message.call_args[0][0]
        self.assertEqual(msg["Subject"], "Subject here")
        self.assertEqual(msg["From"], "from@example.com")
        self.assertEqual(msg["To"], "a@example.com, b@example.com")
        self.assertIn("Body line", msg.get_content())
        kw = srv.send_message.call_args
        self.assertEqual(kw.kwargs["from_addr"], "from@example.com")
        self.assertEqual(kw.kwargs["to_addrs"], ["a@example.com", "b@example.com"])

    @patch("app.smtplib.SMTP")
    def test_send_email_no_tls_no_login_when_not_configured(self, mock_smtp):
        srv = MagicMock()
        mock_smtp.return_value.__enter__.return_value = srv
        ok, _ = app.send_email(
            "S", "B", "info", host="h", port="25",
            sender="f@x", to="t@x", tls=False)
        self.assertTrue(ok)
        srv.starttls.assert_not_called()
        srv.login.assert_not_called()

    def test_send_email_not_configured_returns_false(self):
        ok, err = app.send_email("S", "B", "info", host="", sender="", to="")
        self.assertFalse(ok)
        self.assertEqual(err, "not configured")

    @patch("app.smtplib.SMTP", side_effect=RuntimeError("connection refused"))
    def test_send_email_error_returns_not_raise(self, _mock):
        ok, err = app.send_email(
            "S", "B", "info", host="h", sender="f@x", to="t@x")
        self.assertFalse(ok)
        self.assertIn("connection refused", err)

    @patch("app.smtplib.SMTP", side_effect=RuntimeError("auth failed for secretpw"))
    def test_send_email_error_scrubs_password(self, _mock):
        ok, err = app.send_email(
            "S", "B", "info", host="h", user="u", password="secretpw",
            sender="f@x", to="t@x")
        self.assertFalse(ok)
        self.assertNotIn("secretpw", err)
        self.assertIn("***", err)


class TestEmailWiring(unittest.TestCase):
    @patch("app.send_email", return_value=(True, None))
    def test_dispatch_includes_email_when_host_from_to_set(self, mock_send):
        s = {**app.SETTING_DEFAULTS, "smtp_host": "h",
             "smtp_from": "f@x", "smtp_to": "t@x"}
        results = app.dispatch_alert(s, "info", "T", "B")
        self.assertIn("email", [c for c, _, _ in results])
        mock_send.assert_called_once()

    @patch("app.send_email", return_value=(True, None))
    def test_dispatch_skips_email_when_to_missing(self, mock_send):
        s = {**app.SETTING_DEFAULTS, "smtp_host": "h", "smtp_from": "f@x", "smtp_to": ""}
        results = app.dispatch_alert(s, "info", "T", "B")
        self.assertNotIn("email", [c for c, _, _ in results])
        mock_send.assert_not_called()

    def test_configured_channels_detects_email_and_slack(self):
        none = app._configured_channels(dict(app.SETTING_DEFAULTS))
        self.assertNotIn("email", none)
        self.assertNotIn("slack", none)
        s = {**app.SETTING_DEFAULTS, "smtp_host": "h", "smtp_from": "f@x",
             "smtp_to": "t@x", "slack_webhook_url": "https://hooks.slack.com/x"}
        chans = app._configured_channels(s)
        self.assertIn("email", chans)
        self.assertIn("slack", chans)

    def test_smtp_pass_is_a_secret_and_masked(self):
        self.assertIn("smtp_pass", app.SETTING_SECRETS)
        with patch.object(app, "get_settings", return_value={
            **app.SETTING_DEFAULTS, "smtp_pass": "topsecret"}):
            pub = app._public_settings()
        self.assertNotIn("smtp_pass", pub)
        self.assertTrue(pub["smtp_pass_set"])
        # non-secret SMTP fields stay visible
        self.assertIn("smtp_host", pub)


class TestChannelTestEndpoint(unittest.TestCase):
    """The per-channel test path must accept email/slack and return clean
    (not 500) results when unconfigured."""

    def test_email_test_clean_when_unset(self):
        c = app.app.test_client()
        with patch.object(app, "get_settings", return_value=dict(app.SETTING_DEFAULTS)):
            r = c.post("/api/alerts/channels/test", json={"channel": "email"})
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertFalse(j["ok"])
        self.assertEqual(j["results"][0]["error"], "not configured")

    def test_slack_test_clean_when_unset(self):
        c = app.app.test_client()
        with patch.object(app, "get_settings", return_value=dict(app.SETTING_DEFAULTS)):
            r = c.post("/api/alerts/channels/test", json={"channel": "slack"})
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertFalse(j["ok"])
        self.assertEqual(j["results"][0]["error"], "not configured")

    def test_rule_accepts_email_and_slack_channel(self):
        # create_rule should accept the new channel names.
        for ch in ("email", "slack"):
            rid, err = app.create_rule({"name": f"r-{ch}", "ctype": "uptime_down",
                                        "channel": ch, "level": "warning"})
            self.assertIsNone(err, f"channel {ch} rejected: {err}")
            if rid:
                app.delete_rule(rid)


class TestSettingsRoundTrip(unittest.TestCase):
    """keep / CLEAR round-trip for the two new secrets via the real /api/settings."""

    def setUp(self):
        app.save_settings({"slack_webhook_url": "", "smtp_pass": ""})

    def tearDown(self):
        app.save_settings({"slack_webhook_url": "", "smtp_pass": ""})

    def test_slack_keep_and_clear(self):
        c = app.app.test_client()
        c.post("/api/settings", json={"slack_webhook_url": "https://hooks.slack.com/SECRET"})
        self.assertNotIn("SECRET", c.get("/api/settings").get_data(as_text=True))
        # keep: a save omitting the key leaves it intact
        c.post("/api/settings", json={"alert_min_level": "critical"})
        self.assertEqual(app.get_settings().get("slack_webhook_url"),
                         "https://hooks.slack.com/SECRET")
        # clear
        c.post("/api/settings", json={"slack_webhook_url": ""})
        self.assertEqual(app.get_settings().get("slack_webhook_url"), "")

    def test_smtp_pass_keep_and_clear(self):
        c = app.app.test_client()
        c.post("/api/settings", json={"smtp_pass": "topsecret"})
        self.assertNotIn("topsecret", c.get("/api/settings").get_data(as_text=True))
        c.post("/api/settings", json={"smtp_host": "smtp.example.com"})
        self.assertEqual(app.get_settings().get("smtp_pass"), "topsecret")
        c.post("/api/settings", json={"smtp_pass": ""})
        self.assertEqual(app.get_settings().get("smtp_pass"), "")


if __name__ == "__main__":
    unittest.main()
