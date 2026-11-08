"""The MCP side of the login (#354): homelab_client sends a bearer token when one
is configured (and nothing otherwise), supports an in-process transport, and the
HTTP port's LoginGuard passes everything while no password is set and demands a
read/admin key once one is."""
import asyncio
import importlib.util
import os
import sys
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "mcp"))
import homelab_client as hc  # noqa: E402
from login_guard import LoginGuard  # noqa: E402


class _Resp:
    status = 200

    def __init__(self, body=b"{}"):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestClientToken(unittest.TestCase):
    def setUp(self):
        hc.set_transport(None)         # the agent's tests may have installed the in-process one

    def _headers_sent(self, env):
        seen = {}

        def fake_urlopen(req, timeout=None):
            seen.update(dict(req.header_items()))
            return _Resp()
        with patch.dict(os.environ, env, clear=False), patch("urllib.request.urlopen", fake_urlopen):
            for k in ("HOMELAB_API_TOKEN", "HLM_INTERNAL_TOKEN"):
                if k not in env:
                    os.environ.pop(k, None)
            hc._get("/api/fleet")
        return seen

    def test_no_token_no_header(self):
        self.assertNotIn("Authorization", self._headers_sent({}))

    def test_api_token_sent(self):
        self.assertEqual(self._headers_sent({"HOMELAB_API_TOKEN": "hlm_abc"})["Authorization"], "Bearer hlm_abc")

    def test_internal_token_sent(self):
        self.assertEqual(self._headers_sent({"HLM_INTERNAL_TOKEN": "int"})["Authorization"], "Bearer int")

    def test_transport_hook(self):
        try:
            hc.set_transport(lambda path: (200, '{"hosts": []}'))
            self.assertEqual(hc._get("/api/fleet"), {"hosts": []})
            hc.set_transport(lambda path: (403, "{}"))
            with self.assertRaises(hc.MonitorError):
                hc._get("/api/backup")
        finally:
            hc.set_transport(None)


def _run(guard, headers=()):
    out = []

    async def app(scope, receive, send):
        out.append("app")

    async def send(msg):
        out.append(msg)
    guard.app = app
    asyncio.run(guard({"type": "http", "headers": list(headers)}, None, send))
    return out


class TestLoginGuard(unittest.TestCase):
    def test_passes_through_without_password(self):
        g = LoginGuard(None, "http://x", fetch=lambda p, t: (200, {"password_set": False}))
        self.assertEqual(_run(g), ["app"])

    def test_requires_read_key_with_password(self):
        def fetch(path, token):
            if path == "/api/auth/status":
                return 200, {"password_set": True}
            return (200, {"scope": "read"}) if token == "good" else (401, {})
        g = LoginGuard(None, "http://x", fetch=fetch)
        out = _run(g)
        self.assertEqual(out[0]["status"], 401)
        self.assertIn(b"HOMELAB_API_TOKEN", out[1]["body"])
        self.assertEqual(_run(g, [(b"authorization", b"Bearer good")]), ["app"])
        self.assertEqual(_run(g, [(b"authorization", b"Bearer bad")])[0]["status"], 401)

    def test_push_key_not_enough(self):
        def fetch(path, token):
            return (200, {"password_set": True}) if path == "/api/auth/status" else (200, {"scope": "push"})
        g = LoginGuard(None, "http://x", fetch=fetch)
        self.assertEqual(_run(g, [(b"authorization", b"Bearer k")])[0]["status"], 401)

    def test_lifespan_passes(self):
        g = LoginGuard(None, "http://x", fetch=lambda p, t: (200, {"password_set": True}))
        out = []

        async def app(scope, receive, send):
            out.append(scope["type"])
        g.app = app
        asyncio.run(g({"type": "lifespan"}, None, None))
        self.assertEqual(out, ["lifespan"])


if __name__ == "__main__":
    unittest.main()
