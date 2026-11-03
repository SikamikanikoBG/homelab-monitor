"""/api/backup ships a copy of the database with every secret blanked (#354):
alert-channel webhooks/tokens/passwords, AI provider keys, the admin password
hash and all sessions. The live database keeps them."""
import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from auth_helpers import app, clear_login, login
from backend.agent import schema
from backend.backup_scrub import scrub

SECRETS = {
    "telegram_token": "tg-SECRET-123456",
    "email_password": "smtp-SECRET-pw",
    "discord_webhook_url": "https://discord.example/api/webhooks/SECRET-hook",
}
PROVIDER_KEY = "sk-live-PROVIDERSECRET-9999"


class TestScrubFunction(unittest.TestCase):
    def test_scrub_blanks_secrets_and_keeps_the_rest(self):
        d = tempfile.mkdtemp()
        try:
            path = os.path.join(d, "x.db")
            c = sqlite3.connect(path)
            c.execute("CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT)")
            c.executemany("INSERT INTO settings VALUES(?,?)",
                          list(SECRETS.items()) + [("currency", "€"), ("ntfy_topic", "lab")])
            c.executescript(schema.SCHEMA)
            c.execute("INSERT INTO agent_providers(id,preset,adapter,label,base_url,api_key,key_last4,created_at,"
                      "updated_at) VALUES('p','openai','openai_compat','x','https://api.example',?, '9999',1,1)",
                      (PROVIDER_KEY,))
            c.commit()
            c.close()
            done = scrub(path, set(SECRETS) | {"webhook_url"})
            self.assertIn("settings", done)
            self.assertIn("agent_providers", done)
            raw = open(path, "rb").read()
            for v in list(SECRETS.values()) + [PROVIDER_KEY]:
                self.assertNotIn(v.encode(), raw, v)
            c = sqlite3.connect(path)
            s = dict(c.execute("SELECT key, value FROM settings"))
            self.assertEqual(s["currency"], "€")
            self.assertEqual(s["ntfy_topic"], "lab")
            self.assertEqual(s["telegram_token"], "")
            self.assertEqual(c.execute("SELECT key_last4 FROM agent_providers").fetchone()[0], "9999")
            c.close()
        finally:
            shutil.rmtree(d)

    def test_scrub_tolerates_a_db_without_agent_or_auth_tables(self):
        d = tempfile.mkdtemp()
        try:
            path = os.path.join(d, "x.db")
            c = sqlite3.connect(path)
            c.execute("CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT)")
            c.commit()
            c.close()
            self.assertEqual(scrub(path, {"telegram_token"}), ["settings"])
        finally:
            shutil.rmtree(d)


class TestBackupEndpoint(unittest.TestCase):
    """The streamed backup bytes contain none of the secrets."""

    def setUp(self):
        clear_login()
        self.dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.dir, "gpu.db")

    def tearDown(self):
        clear_login()
        with app.LOCK:
            for k in SECRETS:
                app.DB.execute("DELETE FROM settings WHERE key=?", (k,))
            app.DB.execute("DELETE FROM agent_providers")
            app.DB.commit()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_backup_bytes_have_no_secrets(self):
        app.save_settings(SECRETS)
        with app.LOCK:
            schema.ensure(app.DB)
            app.DB.execute("INSERT INTO agent_providers(id,preset,adapter,label,base_url,api_key,created_at,"
                           "updated_at) VALUES('p','openai','openai_compat','x','https://a',?,1,1)", (PROVIDER_KEY,))
            app.DB.commit()
        c = app.app.test_client()
        login(c)
        with patch.object(app, "DB_PATH", self.db_path), patch("app._data_dir_writable", return_value=True):
            r = c.get("/api/backup")
        self.assertEqual(r.status_code, 200)
        body = r.get_data()
        self.assertTrue(body.startswith(b"SQLite format 3"))
        for v in list(SECRETS.values()) + [PROVIDER_KEY]:
            self.assertNotIn(v.encode(), body, v)
        with app.LOCK:
            pw = app.DB.execute("SELECT pw_hash FROM auth_admin").fetchone()[0]
        self.assertNotIn(pw.encode(), body)
        # The live database still has them.
        self.assertEqual(app.get_settings()["telegram_token"], SECRETS["telegram_token"])


if __name__ == "__main__":
    unittest.main()
