"""ASGI wrapper that keeps the MCP HTTP port behind the dashboard's login (#354).

Without it the MCP port (9810, reachable on the LAN) would let anyone read the
whole fleet after the owner protected the dashboard. While no password is set
it passes everything through, exactly as before. Once one is set, each request
must carry `Authorization: Bearer <key>` with a read or admin key from
Settings → Integrations → API keys; the check is cached for 60 s (so revoking
a key takes up to a minute to bite here — documented in website/mcp.md).

Pure stdlib + asyncio, so it is unit-tested without the `mcp` SDK.
"""
import asyncio
import json
import time
import urllib.error
import urllib.request

_STATUS_TTL = 30
_CHECK_TTL = 60


class LoginGuard:
    def __init__(self, app, base_url, internal_token=None, fetch=None):
        self.app = app
        self.base = base_url.rstrip("/")
        self.internal = internal_token
        self._fetch = fetch or self._http_get
        self._status = (0.0, False)
        self._checks = {}

    def _http_get(self, path, token):
        req = urllib.request.Request(self.base + path, headers={"Authorization": "Bearer " + token} if token else {})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            return e.code, {}
        except (urllib.error.URLError, OSError, ValueError):
            return 0, {}

    async def _password_set(self):
        at, value = self._status
        if time.monotonic() - at < _STATUS_TTL:
            return value
        status, body = await asyncio.to_thread(self._fetch, "/api/auth/status", self.internal)
        # Can't reach the dashboard: fail closed only if we had seen a login before.
        value = bool(body.get("password_set")) if status == 200 else value
        self._status = (time.monotonic(), value)
        return value

    async def _token_ok(self, token):
        hit = self._checks.get(token)
        if hit and time.monotonic() - hit[0] < _CHECK_TTL:
            return hit[1]
        status, body = await asyncio.to_thread(self._fetch, "/api/auth/check", token)
        ok = status == 200 and body.get("scope") in ("read", "admin")
        self._checks[token] = (time.monotonic(), ok)
        return ok

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not await self._password_set():
            return await self.app(scope, receive, send)
        auth = ""
        for k, v in scope.get("headers") or []:
            if k.lower() == b"authorization":
                auth = v.decode("latin-1")
        token = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
        if token and await self._token_ok(token):
            return await self.app(scope, receive, send)
        body = json.dumps({"error": "auth_required",
                           "message": "This dashboard has a login. Set HOMELAB_API_TOKEN (or an "
                                      "Authorization: Bearer header) to a read key from Settings → "
                                      "Integrations → API keys. See website/mcp.md."}).encode()
        await send({"type": "http.response.start", "status": 401,
                    "headers": [(b"content-type", b"application/json"),
                                (b"www-authenticate", b"Bearer")]})
        await send({"type": "http.response.body", "body": body})
