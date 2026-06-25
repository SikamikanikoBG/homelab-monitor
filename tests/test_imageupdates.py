"""Unit tests for Docker image-update awareness (What's-Up-Docker style).

AWARENESS ONLY: the feature compares each RUNNING container's deployed image
digest (read from the LOCAL docker socket) with the upstream registry's current
tag manifest digest. It NEVER pulls/runs/restarts/deletes anything.

Everything network + the docker socket is MOCKED — no real registry or daemon is
touched in CI. Covers the constraints the reviewer hammers:
  • image-ref parsing: docker.io implicit/explicit, library/, user/repo, ghcr,
    other v2 registries, :tag vs :latest vs digest-pinned, registry-with-port.
  • digest compare → up_to_date / update_available.
  • registry token flow + manifest Docker-Content-Digest read.
  • 429 / 401 / 404 / timeout → 'unknown' with a reason, never a crash.
  • OFF by default → no checks happen, no outbound, empty snapshot.
  • per-image cache prevents re-query within the interval.
  • the recommendation detector fires on N>0 updates available (info, links
    Containers); doesn't fire when disabled.
  • the endpoint is always-200 and graceful; no secret leak.
"""
import os
import sys
import json
import time
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


class ParseImageRef(unittest.TestCase):
    def test_implicit_dockerhub_library(self):
        self.assertEqual(app._parse_image_ref("nginx"),
                         ("docker.io", "library/nginx", "latest", None))

    def test_implicit_with_tag(self):
        self.assertEqual(app._parse_image_ref("nginx:1.27"),
                         ("docker.io", "library/nginx", "1.27", None))

    def test_user_repo(self):
        self.assertEqual(app._parse_image_ref("grafana/grafana:11.0.0"),
                         ("docker.io", "grafana/grafana", "11.0.0", None))

    def test_explicit_docker_io(self):
        self.assertEqual(app._parse_image_ref("docker.io/library/redis:7"),
                         ("docker.io", "library/redis", "7", None))

    def test_ghcr(self):
        self.assertEqual(app._parse_image_ref("ghcr.io/home-assistant/home-assistant:stable"),
                         ("ghcr.io", "home-assistant/home-assistant", "stable", None))

    def test_other_registry_with_port(self):
        # The colon belongs to the registry port, NOT a tag — default tag latest.
        self.assertEqual(app._parse_image_ref("registry.example.com:5000/team/app"),
                         ("registry.example.com:5000", "team/app", "latest", None))

    def test_registry_with_port_and_tag(self):
        self.assertEqual(app._parse_image_ref("registry.example.com:5000/team/app:v2"),
                         ("registry.example.com:5000", "team/app", "v2", None))

    def test_digest_pinned(self):
        reg, repo, tag, pinned = app._parse_image_ref("nginx@sha256:" + "a" * 64)
        self.assertEqual((reg, repo), ("docker.io", "library/nginx"))
        self.assertEqual(pinned, "sha256:" + "a" * 64)

    def test_localhost_registry(self):
        self.assertEqual(app._parse_image_ref("localhost:5000/myimg"),
                         ("localhost:5000", "myimg", "latest", None))

    def test_empty(self):
        self.assertEqual(app._parse_image_ref(""), (None, None, None, None))
        self.assertEqual(app._parse_image_ref(None), (None, None, None, None))


class RegistryDigestFlow(unittest.TestCase):
    def _mk_resp(self, digest=None, status=200, body=b"{}", headers=None):
        resp = MagicMock()
        resp.status = status
        resp.read.return_value = body
        h = dict(headers or {})
        if digest is not None:
            h["Docker-Content-Digest"] = digest
        resp.headers = MagicMock()
        resp.headers.get = lambda k, d=None: h.get(k, d)
        resp.__enter__ = lambda s: resp
        resp.__exit__ = lambda s, *a: False
        return resp

    def test_token_then_manifest_digest(self):
        token_resp = self._mk_resp(body=json.dumps({"token": "T0K"}).encode())
        manifest_resp = self._mk_resp(digest="sha256:" + "b" * 64)
        calls = []

        def fake_urlopen(req, timeout=None):
            url = req.full_url if hasattr(req, "full_url") else req.get_full_url()
            calls.append((url, req.headers, getattr(req, "method", None)))
            if "auth.docker.io/token" in url:
                return token_resp
            return manifest_resp

        with patch("app.urllib.request.urlopen", side_effect=fake_urlopen):
            dig, status = app._upstream_manifest_digest("docker.io", "library/nginx", "latest")
        self.assertEqual(status, "ok")
        self.assertEqual(dig, "sha256:" + "b" * 64)
        # token was fetched, then the manifest endpoint hit with a bearer.
        self.assertTrue(any("auth.docker.io/token" in c[0] for c in calls))
        self.assertTrue(any("registry-1.docker.io/v2/library/nginx/manifests/latest" in c[0] for c in calls))
        man = [c for c in calls if "manifests" in c[0]][0]
        self.assertEqual(man[1].get("Authorization"), "Bearer T0K")

    def test_ghcr_token_endpoint(self):
        token_resp = self._mk_resp(body=json.dumps({"token": "GHT"}).encode())
        manifest_resp = self._mk_resp(digest="sha256:" + "c" * 64)
        seen = {"token_url": None}

        def fake_urlopen(req, timeout=None):
            url = req.get_full_url()
            if "token" in url and "ghcr.io" in url:
                seen["token_url"] = url
                return token_resp
            return manifest_resp

        with patch("app.urllib.request.urlopen", side_effect=fake_urlopen):
            dig, status = app._upstream_manifest_digest("ghcr.io", "owner/app", "stable")
        self.assertEqual(status, "ok")
        self.assertIn("ghcr.io/token", seen["token_url"])

    def test_429_rate_limited(self):
        import urllib.error

        def fake_urlopen(req, timeout=None):
            url = req.get_full_url()
            if "token" in url:
                return self._mk_resp(body=b'{"token":"x"}')
            raise urllib.error.HTTPError(url, 429, "Too Many", {}, None)

        with patch("app.urllib.request.urlopen", side_effect=fake_urlopen):
            dig, status = app._upstream_manifest_digest("docker.io", "library/nginx", "latest")
        self.assertEqual(status, "rate_limited")
        self.assertIsNone(dig)

    def test_401_auth(self):
        import urllib.error

        def fake_urlopen(req, timeout=None):
            url = req.get_full_url()
            if "token" in url:
                return self._mk_resp(body=b"{}")
            raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)

        with patch("app.urllib.request.urlopen", side_effect=fake_urlopen):
            dig, status = app._upstream_manifest_digest("docker.io", "private/x", "latest")
        self.assertEqual(status, "auth")

    def test_404_notfound(self):
        import urllib.error

        def fake_urlopen(req, timeout=None):
            url = req.get_full_url()
            if "token" in url:
                return self._mk_resp(body=b"{}")
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

        with patch("app.urllib.request.urlopen", side_effect=fake_urlopen):
            dig, status = app._upstream_manifest_digest("docker.io", "library/nginx", "nope")
        self.assertEqual(status, "notfound")

    def test_timeout_is_error_not_crash(self):
        def fake_urlopen(req, timeout=None):
            raise TimeoutError("timed out")

        with patch("app.urllib.request.urlopen", side_effect=fake_urlopen):
            dig, status = app._upstream_manifest_digest("docker.io", "library/nginx", "latest")
        self.assertEqual(status, "error")
        self.assertIsNone(dig)


class CheckOneImage(unittest.TestCase):
    def setUp(self):
        app._IMG_DIGEST_CACHE.clear()

    def test_up_to_date(self):
        dig = "sha256:" + "d" * 64
        with patch.object(app, "_container_deployed_digest", return_value=(dig, "nginx:latest")), \
             patch.object(app, "_upstream_manifest_digest", return_value=(dig, "ok")):
            res = app._check_one_image("c1", "nginx:latest", 3600)
        self.assertEqual(res["status"], "up_to_date")

    def test_update_available(self):
        with patch.object(app, "_container_deployed_digest", return_value=("sha256:old", "nginx:latest")), \
             patch.object(app, "_upstream_manifest_digest", return_value=("sha256:new", "ok")):
            res = app._check_one_image("c1", "nginx:latest", 3600)
        self.assertEqual(res["status"], "update_available")
        self.assertEqual(res["current_digest"], "sha256:old")
        self.assertEqual(res["latest_digest"], "sha256:new")

    def test_digest_pinned_is_unknown(self):
        res = app._check_one_image("c1", "nginx@sha256:" + "a" * 64, 3600)
        self.assertEqual(res["status"], "unknown")
        self.assertEqual(res["reason"], "digest-pinned")

    def test_no_local_digest_is_unknown(self):
        with patch.object(app, "_container_deployed_digest", return_value=(None, "localbuilt:dev")):
            res = app._check_one_image("c1", "localbuilt:dev", 3600)
        self.assertEqual(res["status"], "unknown")
        self.assertEqual(res["reason"], "no-local-digest")

    def test_rate_limited_propagates(self):
        with patch.object(app, "_container_deployed_digest", return_value=("sha256:x", "nginx:latest")), \
             patch.object(app, "_upstream_manifest_digest", return_value=(None, "rate_limited")):
            res = app._check_one_image("c1", "nginx:latest", 3600)
        self.assertEqual(res["status"], "unknown")
        self.assertEqual(res["reason"], "rate_limited")

    def test_cache_prevents_requery(self):
        calls = {"n": 0}

        def fake_upstream(reg, repo, tag, timeout=6):
            calls["n"] += 1
            return ("sha256:same", "ok")

        with patch.object(app, "_container_deployed_digest", return_value=("sha256:same", "nginx:latest")), \
             patch.object(app, "_upstream_manifest_digest", side_effect=fake_upstream):
            app._check_one_image("c1", "nginx:latest", 3600)
            app._check_one_image("c2", "nginx:latest", 3600)   # same image → cache hit
        self.assertEqual(calls["n"], 1)


class RunCycleAndSnapshot(unittest.TestCase):
    def setUp(self):
        app._IMG_DIGEST_CACHE.clear()
        with app._IMG_LOCK:
            app._IMG_STATE.update(results={}, checked_at=0, count=0,
                                  rate_limited_until=0, last_error=None, enabled=False)

    def test_off_by_default_no_checks_no_outbound(self):
        # image_update_check defaults to "0".
        with patch.object(app, "get_settings", return_value=dict(app.SETTING_DEFAULTS)), \
             patch.object(app, "_docker") as mock_docker, \
             patch("app.urllib.request.urlopen") as mock_net:
            app._image_update_run_cycle()
        mock_docker.assert_not_called()
        mock_net.assert_not_called()
        snap = None
        with patch.object(app, "get_settings", return_value=dict(app.SETTING_DEFAULTS)):
            snap = app.image_updates_snapshot()
        self.assertFalse(snap["enabled"])
        self.assertEqual(snap["results"], [])
        self.assertEqual(snap["count"], 0)

    def test_disabled_snapshot_is_fully_inert(self):
        # A backoff left over from a previous enabled run must NOT leak into the
        # disabled snapshot — off => clean (rate_limited False, no stale results).
        with app._IMG_LOCK:
            app._IMG_STATE.update(rate_limited_until=time.time() + 9999,
                                  results={"x": {"id": "x", "status": "update_available"}},
                                  count=1, checked_at=int(time.time()))
        with patch.object(app, "get_settings", return_value=dict(app.SETTING_DEFAULTS)):
            snap = app.image_updates_snapshot()
        self.assertFalse(snap["enabled"])
        self.assertFalse(snap["rate_limited"])
        self.assertEqual(snap["results"], [])
        self.assertEqual(snap["count"], 0)
        self.assertEqual(snap["checked_at"], 0)

    def test_on_runs_and_counts_updates(self):
        s = dict(app.SETTING_DEFAULTS); s["image_update_check"] = "1"
        running = [{"Id": "aaaaaaaaaaaa1111", "Image": "nginx:latest"},
                   {"Id": "bbbbbbbbbbbb2222", "Image": "grafana/grafana:11"}]

        def fake_check(cid, image_ref, interval):
            if "nginx" in image_ref:
                return {"id": cid, "image": image_ref, "status": "update_available"}
            return {"id": cid, "image": image_ref, "status": "up_to_date"}

        with patch.object(app, "get_settings", return_value=s), \
             patch.object(app, "_docker", return_value=json.dumps(running).encode()), \
             patch.object(app, "_check_one_image", side_effect=fake_check):
            app._image_update_run_cycle()
            snap = app.image_updates_snapshot()
        self.assertTrue(snap["enabled"])
        self.assertEqual(snap["count"], 1)
        self.assertEqual(snap["by_status"]["update_available"], 1)
        self.assertEqual(snap["by_status"]["up_to_date"], 1)

    def test_cycle_skips_when_results_fresh(self):
        s = dict(app.SETTING_DEFAULTS); s["image_update_check"] = "1"
        calls = {"n": 0}

        def fake_docker(path):
            calls["n"] += 1
            return json.dumps([]).encode()

        with patch.object(app, "get_settings", return_value=s), \
             patch.object(app, "_docker", side_effect=fake_docker), \
             patch.object(app, "_check_one_image", return_value={"id": "x", "status": "unknown"}):
            app._image_update_run_cycle()
            app._image_update_run_cycle()   # within interval → no second enumerate
        self.assertEqual(calls["n"], 1)

    def test_cycle_never_crashes_on_docker_error(self):
        s = dict(app.SETTING_DEFAULTS); s["image_update_check"] = "1"
        with patch.object(app, "get_settings", return_value=s), \
             patch.object(app, "_docker", side_effect=RuntimeError("socket gone")):
            app._image_update_run_cycle()   # must not raise
        snap = None
        with patch.object(app, "get_settings", return_value=s):
            snap = app.image_updates_snapshot()
        self.assertIsNotNone(snap["last_error"])


class RecommendationDetector(unittest.TestCase):
    def test_fires_on_updates_available(self):
        sig = {"image_updates": {"enabled": True, "count": 2,
               "results": [{"image": "nginx:latest", "status": "update_available"},
                           {"image": "grafana/grafana:11", "status": "update_available"}]}}
        items = app._reco_detect(sig)
        imgs = [it for it in items if it.get("source") == "images"]
        self.assertEqual(len(imgs), 1)
        self.assertEqual(imgs[0]["severity"], "info")
        self.assertEqual(imgs[0]["link"], "containers")
        self.assertIn("2", imgs[0]["title"])

    def test_silent_when_disabled(self):
        sig = {"image_updates": {"enabled": False, "count": 0, "results": []}}
        items = app._reco_detect(sig)
        self.assertFalse([it for it in items if it.get("source") == "images"])

    def test_silent_when_zero(self):
        sig = {"image_updates": {"enabled": True, "count": 0, "results": []}}
        items = app._reco_detect(sig)
        self.assertFalse([it for it in items if it.get("source") == "images"])


class Endpoint(unittest.TestCase):
    def setUp(self):
        self.c = app.app.test_client()

    def test_endpoint_always_200_and_shape(self):
        with patch.object(app, "image_updates_snapshot",
                          return_value={"enabled": True, "count": 1, "results": [],
                                        "by_status": {"up_to_date": 0, "update_available": 1, "unknown": 0}}):
            r = self.c.get("/api/images/updates")
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertIn("count", j)
        self.assertIn("by_status", j)

    def test_endpoint_graceful_on_error(self):
        with patch.object(app, "image_updates_snapshot", side_effect=RuntimeError("boom")):
            r = self.c.get("/api/images/updates")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.get_json()["enabled"])


class CheckNowEndpoint(unittest.TestCase):
    """On-demand 'Check now' (POST /api/images/updates/check). Explicit user
    action: runs ONE bounded re-check even when the background poll is OFF,
    awareness-only, container validated against the live set, never 500/hang."""

    def setUp(self):
        self.c = app.app.test_client()
        app._IMG_DIGEST_CACHE.clear()
        with app._IMG_LOCK:
            app._IMG_STATE.update(results={}, checked_at=0, count=0,
                                  rate_limited_until=0, last_error=None, enabled=False)

    _LIVE = [{"id": "aaaaaaaaaaaa", "name": "web", "image": "nginx:latest",
              "state": "running", "ports": []}]

    def test_missing_body_is_clean_400(self):
        r = self.c.post("/api/images/updates/check", json={})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.get_json()["ok"])

    def test_unknown_container_is_404(self):
        with patch.object(app, "containers", return_value=self._LIVE):
            r = self.c.post("/api/images/updates/check", json={"container": "ghost"})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.get_json()["error"], "no_such_container")

    def test_injectiony_name_is_404_no_shellout(self):
        # An injection-y value never matches _CT_NAME_RE / the live set → 404,
        # and no probe/network/socket call is ever made for it.
        with patch.object(app, "containers", return_value=self._LIVE), \
             patch.object(app, "_check_one_image") as probe, \
             patch.object(app, "_docker_req") as dreq, \
             patch("app.urllib.request.urlopen") as net:
            for bad in ["web; rm -rf /", "$(id)", "../etc", "web && curl evil",
                        "a" * 200]:
                r = self.c.post("/api/images/updates/check", json={"container": bad})
                self.assertEqual(r.status_code, 404, bad)
        probe.assert_not_called()
        dreq.assert_not_called()
        net.assert_not_called()

    def test_runs_one_probe_updates_cache_returns_fresh(self):
        dig_old, dig_new = "sha256:" + "1" * 64, "sha256:" + "2" * 64
        with patch.object(app, "containers", return_value=self._LIVE), \
             patch.object(app, "get_settings", return_value=dict(app.SETTING_DEFAULTS)), \
             patch.object(app, "_container_deployed_digest",
                          return_value=(dig_old, "nginx:latest")) as dep, \
             patch.object(app, "_upstream_manifest_digest",
                          return_value=(dig_new, "ok")) as up:
            r = self.c.post("/api/images/updates/check", json={"container": "web"})
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertEqual(j["status"], "update_available")
        self.assertEqual(j["deployed_digest"], dig_old)
        self.assertEqual(j["upstream_digest"], dig_new)
        self.assertEqual(j["container"], "web")
        self.assertTrue(j["checked_at"] > 0)
        # exactly one upstream + one deployed probe (single image per call).
        self.assertEqual(up.call_count, 1)
        self.assertEqual(dep.call_count, 1)
        # cache warmed for the shared view.
        with app._IMG_LOCK:
            self.assertIn("aaaaaaaaaaaa", app._IMG_STATE["results"])
            self.assertEqual(app._IMG_STATE["count"], 1)

    def test_works_when_background_poll_off(self):
        # SETTING_DEFAULTS has image_update_check == "0" (background OFF). The
        # explicit check must still run and return a real status.
        self.assertEqual(dict(app.SETTING_DEFAULTS).get("image_update_check"), "0")
        dig = "sha256:" + "3" * 64
        with patch.object(app, "containers", return_value=self._LIVE), \
             patch.object(app, "get_settings", return_value=dict(app.SETTING_DEFAULTS)), \
             patch.object(app, "_container_deployed_digest", return_value=(dig, "nginx:latest")), \
             patch.object(app, "_upstream_manifest_digest", return_value=(dig, "ok")):
            r = self.c.post("/api/images/updates/check", json={"container": "web"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["status"], "up_to_date")

    def test_429_returns_rate_limited_not_crash(self):
        with patch.object(app, "containers", return_value=self._LIVE), \
             patch.object(app, "get_settings", return_value=dict(app.SETTING_DEFAULTS)), \
             patch.object(app, "_container_deployed_digest",
                          return_value=("sha256:x", "nginx:latest")), \
             patch.object(app, "_upstream_manifest_digest",
                          return_value=(None, "rate_limited")):
            r = self.c.post("/api/images/updates/check", json={"container": "web"})
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertTrue(j["rate_limited"])
        self.assertEqual(j["status"], "unknown")
        # backoff is now armed in shared state.
        with app._IMG_LOCK:
            self.assertGreater(app._IMG_STATE["rate_limited_until"], time.time())

    def test_existing_backoff_short_circuits_without_probe(self):
        # If already rate-limited, the explicit check returns rate_limited WITHOUT
        # hitting the registry again (no hammering Docker Hub).
        with app._IMG_LOCK:
            app._IMG_STATE["rate_limited_until"] = time.time() + 999
        with patch.object(app, "containers", return_value=self._LIVE), \
             patch.object(app, "get_settings", return_value=dict(app.SETTING_DEFAULTS)), \
             patch.object(app, "_upstream_manifest_digest") as up:
            r = self.c.post("/api/images/updates/check", json={"container": "web"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["rate_limited"])
        up.assert_not_called()

    def test_force_bypasses_interval_cache(self):
        # A warm per-image cache must NOT short-circuit the explicit check — the
        # user asked for a fresh answer (force=True re-queries upstream).
        ck = ("docker.io", "library/nginx", "latest")
        app._IMG_DIGEST_CACHE[ck] = {"digest": "sha256:stale", "status": "ok",
                                     "at": time.time()}
        with patch.object(app, "containers", return_value=self._LIVE), \
             patch.object(app, "get_settings", return_value=dict(app.SETTING_DEFAULTS)), \
             patch.object(app, "_container_deployed_digest",
                          return_value=("sha256:dep", "nginx:latest")), \
             patch.object(app, "_upstream_manifest_digest",
                          return_value=("sha256:fresh", "ok")) as up:
            r = self.c.post("/api/images/updates/check", json={"container": "web"})
        up.assert_called_once()
        self.assertEqual(r.get_json()["upstream_digest"], "sha256:fresh")

    def test_awareness_only_no_mutation_calls(self):
        # The check must use ONLY read verbs on the docker socket (GET) — never a
        # POST/DELETE (pull/run/restart/exec/delete) for the on-demand path.
        seen = {"methods": set()}

        def fake_docker_req(method, path, *a, **k):
            seen["methods"].add(method.upper())
            if "/json" in path and "/images/" not in path:
                return 200, json.dumps({"Image": "imgid",
                                        "Config": {"Image": "nginx:latest"}}).encode()
            if "/images/" in path:
                return 200, json.dumps({"RepoDigests": ["library/nginx@sha256:dep"]}).encode()
            return 404, b"{}"

        with patch.object(app, "containers", return_value=self._LIVE), \
             patch.object(app, "get_settings", return_value=dict(app.SETTING_DEFAULTS)), \
             patch.object(app, "_docker_req", side_effect=fake_docker_req), \
             patch.object(app, "_upstream_manifest_digest", return_value=("sha256:up", "ok")):
            r = self.c.post("/api/images/updates/check", json={"container": "web"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(seen["methods"], {"GET"})  # read-only — no POST/DELETE/PUT

    def test_never_500_when_probe_raises(self):
        with patch.object(app, "containers", return_value=self._LIVE), \
             patch.object(app, "get_settings", return_value=dict(app.SETTING_DEFAULTS)), \
             patch.object(app, "_check_one_image", side_effect=RuntimeError("boom")):
            r = self.c.post("/api/images/updates/check", json={"container": "web"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["status"], "unknown")


if __name__ == "__main__":
    unittest.main()
