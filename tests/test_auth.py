"""The optional dashboard login (#327, #354): passwords, sessions, CSRF, scoped
keys, the minting rules, the reset CLI and the throttle.

The headline guarantee — no password means nothing changes — is covered here
and, route by route, in test_auth_gate_sweep.py.
"""
import os
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from auth_helpers import PASSWORD, app, clear_login, login, mint_key
from backend.auth import cli, passwords, sessions, store


class TestPasswords(unittest.TestCase):
    def test_scrypt_round_trip(self):
        h = passwords.hash_password("s3cret-enough")
        self.assertTrue(h.startswith("scrypt$"))
        self.assertTrue(passwords.verify_password("s3cret-enough", h))
        self.assertFalse(passwords.verify_password("wrong", h))

    def test_pbkdf2_hashes_still_verify(self):
        import base64
        import hashlib
        salt = b"0123456789abcdef"
        dk = hashlib.pbkdf2_hmac("sha256", b"pw-for-pbkdf2", salt, 1000)
        stored = "pbkdf2_sha256$1000$%s$%s" % (base64.b64encode(salt).decode(), base64.b64encode(dk).decode())
        self.assertTrue(passwords.verify_password("pw-for-pbkdf2", stored))
        self.assertFalse(passwords.verify_password("nope", stored))

    def test_garbage_hash_never_verifies(self):
        for bad in (None, "", "plain", "scrypt$x$y", "md5$abc"):
            self.assertFalse(passwords.verify_password("x", bad))

    def test_short_password_rejected(self):
        self.assertIsNotNone(passwords.check_new_password("short"))
        self.assertIsNone(passwords.check_new_password("long enough pw"))


class LoginCase(unittest.TestCase):
    def setUp(self):
        clear_login()
        self.c = app.app.test_client()

    def tearDown(self):
        clear_login()


class TestNoPassword(LoginCase):
    def test_status_says_no_password(self):
        j = self.c.get("/api/auth/status").get_json()
        self.assertEqual(j, {"password_set": False, "authenticated": False, "via": None, "agent": False})

    def test_routes_open_as_before(self):
        self.assertEqual(self.c.get("/api/fleet").status_code, 200)
        self.assertEqual(self.c.get("/").status_code, 200)

    def test_key_listing_has_no_scope_fields(self):
        mint_key("push")
        keys = self.c.get("/api/integration/keys").get_json()["keys"]
        self.assertTrue(keys)
        self.assertNotIn("scope", keys[0])
        with app.LOCK:
            app.DB.execute("DELETE FROM api_keys")
            app.DB.commit()

    def test_only_push_keys_before_a_password(self):
        for scope in ("read", "admin"):
            r = self.c.post("/api/integration/keys", json={"name": "x", "scope": scope})
            self.assertEqual(r.status_code, 403, scope)
            self.assertEqual(r.get_json()["error"], "password_required")
        r = self.c.post("/api/integration/keys", json={"name": "x"})
        self.assertEqual(r.status_code, 200)
        r = self.c.post("/api/integration/keys", json={"name": "x", "scope": "root"})
        self.assertEqual(r.status_code, 400)
        with app.LOCK:
            app.DB.execute("DELETE FROM api_keys")
            app.DB.commit()

    def test_login_without_password_is_409(self):
        self.assertEqual(self.c.post("/api/auth/login", json={"password": "x"}).status_code, 409)


class TestSetupAndSessions(LoginCase):
    def test_setup_logs_in_and_refuses_twice(self):
        r = self.c.post("/api/auth/setup", json={"password": PASSWORD})
        self.assertEqual(r.status_code, 200)
        cookie = r.headers.get("Set-Cookie")
        self.assertIn("hlm_session=", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        st = self.c.get("/api/auth/status").get_json()
        self.assertTrue(st["authenticated"])
        self.assertEqual(st["csrf"], r.get_json()["csrf"])
        r2 = app.app.test_client().post("/api/auth/setup", json={"password": "another password"})
        self.assertEqual(r2.status_code, 409)

    def test_weak_password_refused(self):
        r = self.c.post("/api/auth/setup", json={"password": "short"})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(sessions.password_set())

    def test_setup_revokes_read_admin_keys_unless_kept(self):
        _, _ = mint_key("push")
        keep_id, _ = mint_key("read")
        drop_id, _ = mint_key("admin")
        r = self.c.post("/api/auth/setup", json={"password": PASSWORD, "keep_keys": [keep_id]})
        self.assertEqual(r.get_json()["revoked_keys"], 1)
        with app.LOCK:
            ids = {row[0] for row in app.DB.execute("SELECT id FROM api_keys")}
            app.DB.execute("DELETE FROM api_keys")
            app.DB.commit()
        self.assertIn(keep_id, ids)
        self.assertNotIn(drop_id, ids)

    def test_wrong_password_and_throttle(self):
        login(self.c)
        other = app.app.test_client()
        for _ in range(5):
            self.assertEqual(other.post("/api/auth/login", json={"password": "nope"}).status_code, 401)
        r = other.post("/api/auth/login", json={"password": PASSWORD})
        self.assertEqual(r.status_code, 429)
        self.assertGreaterEqual(r.get_json()["retry_after"], 1)

    def test_logout_ends_the_session(self):
        csrf = login(self.c)
        self.assertEqual(self.c.post("/api/auth/logout", headers={"X-CSRF-Token": csrf}).status_code, 200)
        self.assertEqual(self.c.get("/api/fleet").status_code, 401)

    def test_password_change_revokes_other_sessions(self):
        csrf = login(self.c)
        phone = app.app.test_client()
        login(phone)
        r = self.c.post("/api/auth/password", json={"current": PASSWORD, "new": "a brand new password"},
                        headers={"X-CSRF-Token": csrf})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.c.get("/api/fleet").status_code, 200)
        self.assertEqual(phone.get("/api/fleet").status_code, 401)

    def test_change_needs_current_password(self):
        csrf = login(self.c)
        r = self.c.post("/api/auth/password", json={"current": "wrong", "new": "a brand new password"},
                        headers={"X-CSRF-Token": csrf})
        self.assertEqual(r.status_code, 403)

    def test_remove_password_refused_while_agent_on(self):
        csrf = login(self.c)
        with patch("backend.api.auth_api._agent_enabled", return_value=True):
            r = self.c.delete("/api/auth/password", json={"current": PASSWORD}, headers={"X-CSRF-Token": csrf})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["error"], "agent_enabled")
        r = self.c.delete("/api/auth/password", json={"current": PASSWORD}, headers={"X-CSRF-Token": csrf})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(sessions.password_set())
        self.assertEqual(app.app.test_client().get("/api/fleet").status_code, 200)

    def test_sessions_list_and_revoke(self):
        csrf = login(self.c)
        phone = app.app.test_client()
        login(phone)
        items = self.c.get("/api/auth/sessions").get_json()["sessions"]
        self.assertEqual(len(items), 2)
        self.assertEqual(sum(1 for s in items if s["current"]), 1)
        other = next(s for s in items if not s["current"])
        r = self.c.delete("/api/auth/sessions/" + other["id"], headers={"X-CSRF-Token": csrf})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(phone.get("/api/fleet").status_code, 401)

    def test_session_expiry(self):
        login(self.c)
        with app.LOCK:
            app.DB.execute("UPDATE auth_sessions SET expires_at=?", (int(time.time()) - 1,))
            app.DB.commit()
        sessions.invalidate()
        self.assertEqual(self.c.get("/api/fleet").status_code, 401)

    def test_cookie_value_is_not_stored(self):
        r = self.c.post("/api/auth/setup", json={"password": PASSWORD})
        token = r.headers["Set-Cookie"].split("hlm_session=")[1].split(";")[0]
        with app.LOCK:
            rows = [x[0] for x in app.DB.execute("SELECT id_hash FROM auth_sessions")]
        self.assertNotIn(token, rows)


class TestGateWithPassword(LoginCase):
    def setUp(self):
        super().setUp()
        self.csrf = login(self.c)
        self.anon = app.app.test_client()

    def tearDown(self):
        with app.LOCK:
            app.DB.execute("DELETE FROM api_keys")
            app.DB.commit()
        super().tearDown()

    def test_anonymous_api_is_401_and_page_redirects(self):
        r = self.anon.get("/api/fleet")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.get_json()["error"], "auth_required")
        r = self.anon.get("/", headers={"Accept": "text/html"})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].startswith("/login"))

    def test_open_routes_stay_open(self):
        for path in ("/healthz", "/metrics", "/login", "/api/auth/status", "/favicon.ico",
                     "/static/logo.svg", "/api/public-status"):
            self.assertNotIn(self.anon.get(path).status_code, (401, 302), path)

    def test_metrics_can_require_auth(self):
        with patch.dict(os.environ, {"METRICS_REQUIRE_AUTH": "1"}):
            self.assertEqual(self.anon.get("/metrics").status_code, 401)
            _, key = mint_key("read")
            self.assertEqual(self.anon.get("/metrics", headers={"Authorization": "Bearer " + key}).status_code, 200)

    def test_mutation_needs_csrf(self):
        r = self.c.post("/api/settings", json={"currency": "€"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.get_json()["error"], "csrf")
        r = self.c.post("/api/settings", json={"currency": "€"}, headers={"X-CSRF-Token": "wrong"})
        self.assertEqual(r.status_code, 403)
        r = self.c.post("/api/settings", json={"currency": "€"}, headers={"X-CSRF-Token": self.csrf})
        self.assertEqual(r.status_code, 200)

    def test_cross_origin_mutation_refused(self):
        r = self.c.post("/api/settings", json={"currency": "€"},
                        headers={"X-CSRF-Token": self.csrf, "Origin": "http://evil.example"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.get_json()["error"], "bad_origin")
        r = self.c.post("/api/settings", json={"currency": "€"},
                        headers={"X-CSRF-Token": self.csrf, "Origin": "http://localhost"})
        self.assertEqual(r.status_code, 200)
        with patch.dict(os.environ, {"TRUSTED_ORIGINS": "https://proxy.example"}):
            r = self.c.post("/api/settings", json={"currency": "€"},
                            headers={"X-CSRF-Token": self.csrf, "Origin": "https://proxy.example"})
            self.assertEqual(r.status_code, 200)

    def test_push_key_only_reaches_push_routes(self):
        _, key = mint_key("push")
        h = {"Authorization": "Bearer " + key}
        self.assertEqual(self.anon.get("/api/fleet", headers=h).status_code, 403)
        r = self.anon.post("/api/runs", json={"name": "t"}, headers=h)
        self.assertEqual(r.status_code, 200)
        with app.LOCK:
            app.DB.execute("DELETE FROM runs")
            app.DB.commit()

    def test_read_key(self):
        _, key = mint_key("read")
        h = {"Authorization": "Bearer " + key}
        self.assertEqual(self.anon.get("/api/fleet", headers=h).status_code, 200)
        self.assertEqual(self.anon.post("/api/settings", json={}, headers=h).status_code, 403)
        self.assertEqual(self.anon.get("/api/backup", headers=h).status_code, 403)
        self.assertEqual(self.anon.get("/api/integration/keys", headers=h).status_code, 403)
        self.assertEqual(self.anon.post("/api/runs", json={"name": "t"}, headers=h).status_code, 403)

    def test_admin_key_reaches_everything_without_csrf(self):
        _, key = mint_key("admin")
        h = {"Authorization": "Bearer " + key}
        self.assertEqual(self.anon.post("/api/settings", json={"currency": "€"}, headers=h).status_code, 200)
        self.assertEqual(self.anon.get("/api/integration/keys", headers=h).status_code, 200)

    def test_expired_key_refused(self):
        kid, key = mint_key("admin", days=1)
        with app.LOCK:
            app.DB.execute("UPDATE api_keys SET expires_at=? WHERE id=?", (int(time.time()) - 5, kid))
            app.DB.commit()
        self.assertEqual(self.anon.get("/api/fleet", headers={"Authorization": "Bearer " + key}).status_code, 401)

    def test_admin_key_minted_with_90_day_default(self):
        r = self.c.post("/api/integration/keys", json={"name": "ops", "scope": "admin"},
                        headers={"X-CSRF-Token": self.csrf})
        self.assertEqual(r.status_code, 200)
        keys = self.c.get("/api/integration/keys").get_json()["keys"]
        k = next(k for k in keys if k["id"] == r.get_json()["id"])
        self.assertEqual(k["scope"], "admin")
        self.assertAlmostEqual(k["expires_at"] - k["created_at"], 90 * 86400, delta=5)

    def test_key_use_records_ip(self):
        kid, key = mint_key("read")
        self.anon.get("/api/fleet", headers={"Authorization": "Bearer " + key},
                      environ_base={"REMOTE_ADDR": "10.1.2.3"})
        keys = self.c.get("/api/integration/keys").get_json()["keys"]
        self.assertEqual(next(k for k in keys if k["id"] == kid)["last_ip"], "10.1.2.3")

    def test_internal_environ_is_read_only(self):
        from backend.auth.gate import INTERNAL_ENV_KEY
        env = {INTERNAL_ENV_KEY: True}
        self.assertEqual(self.anon.get("/api/fleet", environ_base=env).status_code, 200)
        self.assertEqual(self.anon.post("/api/settings", json={}, environ_base=env).status_code, 403)
        self.assertEqual(self.anon.get("/api/backup", environ_base=env).status_code, 403)

    def test_internal_token_only_from_loopback(self):
        with patch.dict(os.environ, {"HLM_INTERNAL_TOKEN": "tok-123"}):
            h = {"Authorization": "Bearer tok-123"}
            r = self.anon.get("/api/fleet", headers=h, environ_base={"REMOTE_ADDR": "127.0.0.1"})
            self.assertEqual(r.status_code, 200)
            r = self.anon.get("/api/fleet", headers=h, environ_base={"REMOTE_ADDR": "192.168.1.5"})
            self.assertEqual(r.status_code, 401)
            r = self.anon.post("/api/settings", json={}, headers=h, environ_base={"REMOTE_ADDR": "127.0.0.1"})
            self.assertEqual(r.status_code, 403)
            j = self.anon.get("/api/auth/check", headers=h, environ_base={"REMOTE_ADDR": "127.0.0.1"}).get_json()
            self.assertEqual(j["scope"], "internal")


class TestResetCli(unittest.TestCase):
    def test_reset_clears_password_and_turns_agent_off(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            conn = sqlite3.connect(path)
            from backend.agent import schema
            conn.executescript(store.AUTH_SCHEMA)
            conn.executescript(schema.SCHEMA)
            conn.execute("INSERT INTO auth_admin VALUES(1,'scrypt$x',1,1)")
            conn.execute("INSERT INTO auth_sessions VALUES('h','c',1,1,9999999999,'','')")
            conn.execute("INSERT INTO agent_settings VALUES('enabled','1')")
            conn.commit()
            conn.close()
            done = cli.reset_password(path)
            self.assertEqual(len(done), 3)
            conn = sqlite3.connect(path)
            self.assertIsNone(conn.execute("SELECT * FROM auth_admin").fetchone())
            self.assertIsNone(conn.execute("SELECT * FROM auth_sessions").fetchone())
            self.assertEqual(dict(conn.execute("SELECT key,value FROM agent_settings")),
                             {"enabled": "0", "paused": "1"})
            conn.close()
        finally:
            os.unlink(path)

    def test_reset_on_a_db_without_login_is_harmless(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            self.assertEqual(cli.reset_password(path), [])
            self.assertEqual(cli.main(["cli"]), 2)
        finally:
            os.unlink(path)


class TestEnvSeed(LoginCase):
    def test_admin_password_env_seeds_once(self):
        with patch.dict(os.environ, {"ADMIN_PASSWORD": "from the environment"}):
            self.assertTrue(sessions.seed_from_env())
            self.assertTrue(sessions.verify("from the environment"))
        with patch.dict(os.environ, {"ADMIN_PASSWORD": "a different one!!"}):
            self.assertFalse(sessions.seed_from_env())
            self.assertTrue(sessions.verify("from the environment"))

    def test_short_env_password_ignored(self):
        with patch.dict(os.environ, {"ADMIN_PASSWORD": "short"}):
            self.assertFalse(sessions.seed_from_env())
        self.assertFalse(sessions.password_set())


if __name__ == "__main__":
    unittest.main()
