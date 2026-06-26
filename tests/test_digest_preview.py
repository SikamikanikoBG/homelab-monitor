"""Unit tests for the in-browser digest preview endpoint (E1).

GET /api/copilot/digest/preview renders the CURRENT scheduled digest as a
standalone HTML page using the SAME builders the email channel uses
(_digest_sections + _render_digest_html), so the preview is byte-faithful to
what would be sent. Covers (no live ollama needed — LLM is forced off):
  • 200 text/html with the digest section content
  • theme=light|dark both accepted, no error; dark recolours the email-safe palette
  • narrative=0 short-circuits ollama; the deterministic sections still render
  • LLM-down -> still a valid page with deterministic sections (no 500, no hang)
  • no secret leaks into the HTML even when settings carry one
  • the existing /api/copilot/digest JSON endpoint is unchanged
  • i18n parity for the new button label (en <-> zh-CN)
"""
import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


# A representative section bundle (mirrors _digest_sections output shape).
_SECTIONS = [
    ("Needs attention", [
        "Recommendations: 2 open (1 critical, 1 warning).",
        "  - [CRIT] Disk /backup nearly full",
    ]),
    ("Capacity", ["Disk /backup is 92% full, fills in ~5 days (~3 GB/day)."]),
]


class _Base(unittest.TestCase):
    def setUp(self):
        self.c = app.app.test_client()
        self._en = app.COPILOT_ENABLED
        app.COPILOT_ENABLED = False   # deterministic no-LLM path (narrative dropped)

    def tearDown(self):
        app.COPILOT_ENABLED = self._en


class TestPreviewRoute(_Base):
    def test_returns_html_with_sections(self):
        with patch.object(app, "_digest_sections", return_value=_SECTIONS):
            r = self.c.get("/api/copilot/digest/preview")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.mimetype.startswith("text/html"))
        body = r.get_data(as_text=True)
        self.assertTrue(body.lstrip().startswith("<!DOCTYPE html>"))
        self.assertIn("Lab Copilot brief", body)
        self.assertIn("Needs attention", body)
        self.assertIn("Capacity", body)
        self.assertIn("fills in ~5 days", body)

    def test_theme_light_and_dark_accepted(self):
        with patch.object(app, "_digest_sections", return_value=_SECTIONS):
            light = self.c.get("/api/copilot/digest/preview?theme=light")
            dark = self.c.get("/api/copilot/digest/preview?theme=dark")
            bogus = self.c.get("/api/copilot/digest/preview?theme=neon")
        for r in (light, dark, bogus):
            self.assertEqual(r.status_code, 200)
            self.assertIn("Lab Copilot brief", r.get_data(as_text=True))
        # dark variant recolours the email-safe background; light keeps it white
        lbody = light.get_data(as_text=True)
        dbody = dark.get_data(as_text=True)
        self.assertIn("background:#ffffff", lbody)
        self.assertNotIn("background:#ffffff", dbody)
        self.assertIn("background:#0b0f17", dbody)
        # bogus theme falls back to light styling
        self.assertIn("background:#ffffff", bogus.get_data(as_text=True))

    def test_no_data_still_valid_page(self):
        with patch.object(app, "_digest_sections", return_value=[]):
            r = self.c.get("/api/copilot/digest/preview")
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        self.assertIn("Lab Copilot brief", body)   # header always renders

    def test_llm_down_never_500_renders_sections(self):
        # ollama call raises -> narrative dropped, deterministic sections stand alone
        app.COPILOT_ENABLED = True
        with patch.object(app, "_digest_sections", return_value=_SECTIONS), \
             patch.object(app, "_ollama_generate", side_effect=OSError("down")):
            r = self.c.get("/api/copilot/digest/preview")
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        self.assertIn("Capacity", body)
        self.assertNotIn(">Summary<", body)   # no narrative block

    def test_narrative_zero_skips_ollama(self):
        app.COPILOT_ENABLED = True
        with patch.object(app, "_digest_sections", return_value=_SECTIONS), \
             patch.object(app, "_ollama_generate") as gen:
            r = self.c.get("/api/copilot/digest/preview?narrative=0")
        self.assertEqual(r.status_code, 200)
        gen.assert_not_called()
        self.assertIn("Capacity", r.get_data(as_text=True))

    def test_narrative_included_when_llm_ok(self):
        app.COPILOT_ENABLED = True
        with patch.object(app, "_digest_sections", return_value=_SECTIONS), \
             patch.object(app, "_copilot_context", return_value={}), \
             patch.object(app, "_copilot_facts", return_value=["f"]), \
             patch.object(app, "_ollama_generate",
                          return_value=("Nightly the GPU burned 1.2 kWh.", None)):
            r = self.c.get("/api/copilot/digest/preview")
        body = r.get_data(as_text=True)
        self.assertIn("Nightly the GPU burned 1.2 kWh.", body)


class TestNoSecretLeak(_Base):
    def test_no_secret_in_preview_html(self):
        # Plant a secret in settings; the preview is telemetry-only and must not echo it.
        saved = {k: app.get_settings().get(k, "")
                 for k in ("smtp_pass", "discord_webhook_url", "telegram_token")}
        try:
            app.save_settings({"smtp_pass": "topsecret-xyz",
                               "discord_webhook_url": "https://discord/SECRETHOOK",
                               "telegram_token": "BOTSECRET123"})
            with patch.object(app, "_digest_sections", return_value=_SECTIONS):
                body = self.c.get("/api/copilot/digest/preview").get_data(as_text=True)
            low = body.lower()
            for needle in ("topsecret", "secrethook", "botsecret", "smtp_pass", "password"):
                self.assertNotIn(needle.lower(), low)
        finally:
            app.save_settings(saved)


class TestExistingDigestUnchanged(_Base):
    def test_digest_json_still_json(self):
        r = self.c.get("/api/copilot/digest")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.mimetype.startswith("application/json"))
        data = r.get_json()
        for k in ("now", "facts", "digest", "source", "llm_status"):
            self.assertIn(k, data)


class TestLocaleParity(unittest.TestCase):
    def _load(self, name):
        p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "locales", name)
        with open(p, encoding="utf-8") as f:
            return json.load(f)

    def test_preview_keys_in_both_locales(self):
        en = self._load("en.json")
        zh = self._load("zh-CN.json")
        for key in ("digest.preview", "digest.preview_hint"):
            self.assertIn(key, en)
            self.assertIn(key, zh)
            self.assertTrue(en[key].strip())
            self.assertTrue(zh[key].strip())


if __name__ == "__main__":
    unittest.main()
