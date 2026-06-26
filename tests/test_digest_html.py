"""Unit tests for the HTML email body of the scheduled Lab Copilot digest (E1).

Covers the non-LLM logic (no ollama needed in CI):
  • _render_digest_html turns the SAME deterministic sections into email-safe HTML
    (inline styles only, no <style>/<script>/external assets), with the narrative
    when present and the section headers/lines included
  • every dynamic value is HTML-escaped so a name with '<' / '&' can't break markup
  • LLM-down -> the HTML brief still renders the deterministic sections (no narrative),
    never empty
  • send_email with html_body builds a multipart/alternative carrying BOTH a text and
    an html part; without html_body it stays single-part text/plain (unchanged)
  • the digest send routes HTML to the email channel ONLY; chat channels get plain-text
  • no secret leaks into the HTML body
All SMTP / ollama / signals are mocked — nothing leaves the box.
"""
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


# A representative section bundle (mirrors _digest_sections output shape), including a
# dynamic value with HTML-special chars to prove escaping.
_SECTIONS = [
    ("Needs attention", [
        "Recommendations: 2 open (1 critical, 1 warning).",
        "  - [CRIT] Disk /backup nearly full",
    ]),
    ("Capacity", ["Disk /backup is 92% full, fills in ~5 days (~3 GB/day)."]),
    ("Anomalies", ["gpu_power up — 210W now vs ~90W baseline (4.1σ)."]),
]


class TestRenderDigestHtml(unittest.TestCase):
    def test_renders_sections_and_narrative(self):
        html = app._render_digest_html("Last night the GPU burned 1.2 kWh.", _SECTIONS)
        self.assertTrue(html.lstrip().startswith("<!DOCTYPE html>"))
        # header + footer present
        self.assertIn("HomeLab Monitor", html)
        self.assertIn("Lab Copilot brief", html)
        # narrative present
        self.assertIn("Last night the GPU burned 1.2 kWh.", html)
        # each section header + a line
        self.assertIn("Needs attention", html)
        self.assertIn("Capacity", html)
        self.assertIn("Anomalies", html)
        self.assertIn("fills in ~5 days", html)
        # email-client-safe: inline styles only, no style/script/external assets
        self.assertNotIn("<style", html.lower())
        self.assertNotIn("<script", html.lower())
        self.assertNotIn("<link", html.lower())
        self.assertNotIn("javascript:", html.lower())
        self.assertIn("style=", html)   # inline styling is used

    def test_escapes_dynamic_value(self):
        secs = [("Needs attention", [
            "  - [CRIT] model <b>a&b</b> misbehaving",
        ])]
        html = app._render_digest_html(None, secs)
        # the raw tag/amp must NOT appear unescaped; the escaped form must
        self.assertNotIn("<b>a&b</b>", html)
        self.assertIn("&lt;b&gt;a&amp;b&lt;/b&gt;", html)

    def test_escapes_narrative(self):
        html = app._render_digest_html("danger <script>alert(1)</script>", [])
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_llm_down_still_renders_sections(self):
        # narrative None == LLM unreachable: sections still render, never empty
        html = app._render_digest_html(None, _SECTIONS)
        self.assertNotIn(">Summary<", html)        # no narrative block
        self.assertIn("Needs attention", html)
        self.assertIn("Capacity", html)
        self.assertTrue(len(html) > 200)


class TestSendEmailMultipart(unittest.TestCase):
    @patch("app.smtplib.SMTP")
    def test_html_body_makes_multipart_alternative(self, mock_smtp):
        srv = MagicMock()
        mock_smtp.return_value.__enter__.return_value = srv
        ok, err = app.send_email(
            "Subj", "plain text body", "info",
            host="h", sender="f@x", to="t@x", tls=False,
            html_body="<html><body><b>rich</b></body></html>")
        self.assertTrue(ok)
        self.assertIsNone(err)
        msg = srv.send_message.call_args[0][0]
        self.assertTrue(msg.is_multipart())
        types = sorted(p.get_content_type() for p in msg.iter_parts())
        self.assertEqual(types, ["text/html", "text/plain"])
        # text part carries the plain body; html part carries the markup
        text_part = next(p for p in msg.iter_parts()
                         if p.get_content_type() == "text/plain")
        html_part = next(p for p in msg.iter_parts()
                         if p.get_content_type() == "text/html")
        self.assertIn("plain text body", text_part.get_content())
        self.assertIn("<b>rich</b>", html_part.get_content())

    @patch("app.smtplib.SMTP")
    def test_no_html_body_stays_single_part_text(self, mock_smtp):
        srv = MagicMock()
        mock_smtp.return_value.__enter__.return_value = srv
        ok, _ = app.send_email(
            "Subj", "plain only", "info",
            host="h", sender="f@x", to="t@x", tls=False)
        self.assertTrue(ok)
        msg = srv.send_message.call_args[0][0]
        self.assertFalse(msg.is_multipart())
        self.assertEqual(msg.get_content_type(), "text/plain")
        self.assertIn("plain only", msg.get_content())


class TestDigestRoutesHtmlToEmailOnly(unittest.TestCase):
    """dispatch_alert passes html_detail to the email channel ONLY; chat channels get
    plain-text. send_digest builds the HTML and wires it through."""

    def setUp(self):
        self._en = app.COPILOT_ENABLED
        app.COPILOT_ENABLED = False   # deterministic no-LLM path

    def tearDown(self):
        app.COPILOT_ENABLED = self._en

    @patch("app.send_email", return_value=(True, None))
    @patch("app.send_discord")
    def test_email_gets_html_others_do_not(self, mock_discord, mock_email):
        s = {**app.SETTING_DEFAULTS,
             "smtp_host": "h", "smtp_from": "f@x", "smtp_to": "t@x",
             "discord_webhook_url": "https://discord/x"}
        app.dispatch_alert(s, "info", "T", "plain body", channel="all",
                           html_detail="<html>BRIEF</html>")
        # email got the html alternative
        self.assertEqual(mock_email.call_args.kwargs.get("html_body"),
                         "<html>BRIEF</html>")
        # discord got plain text only (no html arg, no raw tags in detail)
        d_args = mock_discord.call_args[0]
        self.assertNotIn("<html>", " ".join(str(a) for a in d_args))

    @patch("app.dispatch_alert", return_value=[("email", True, None)])
    def test_send_digest_builds_and_passes_html(self, mock_dispatch):
        s = {**app.SETTING_DEFAULTS,
             "smtp_host": "h", "smtp_from": "f@x", "smtp_to": "t@x",
             "digest_channel": "email"}
        with patch.object(app, "get_settings", return_value=s):
            app.send_digest(channel="email", s=s, record=False)
        # html_detail kwarg was supplied to dispatch and is a non-empty HTML string
        hd = mock_dispatch.call_args.kwargs.get("html_detail")
        self.assertTrue(hd)
        self.assertIn("<", hd)
        self.assertIn("Lab Copilot brief", hd)


class TestHtmlNoSecretLeak(unittest.TestCase):
    def test_no_secret_in_html(self):
        # The HTML is built from sections/narrative only (telemetry facts). Ensure a
        # configured secret never appears even if settings carry one.
        secs = [("Capacity", ["Disk /backup is 92% full."])]
        html = app._render_digest_html("nightly brief", secs)
        for needle in ("topsecret", "smtp_pass", "password", "webhook"):
            self.assertNotIn(needle, html.lower())


if __name__ == "__main__":
    unittest.main()
