"""Tests for the public read-only status page (E4) — /status (HTML) + /api/status
(JSON). The page is UNAUTHENTICATED, so the central test is privacy: the public
payload must carry only aggregated, non-sensitive signals and NONE of the known
topology/secret/settings fields the rest of the app exposes."""
import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


def _walk_strings(obj, path="$"):
    """Yield (json_pointer, key_name, value) for every leaf + dict key so a
    privacy assertion can scan the whole tree, not just the top level."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield (path, "key", k)
            yield from _walk_strings(v, path + "." + str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk_strings(v, path + "[%d]" % i)
    else:
        yield (path, "value", obj)


class TestPublicStatus(unittest.TestCase):
    def setUp(self):
        self.c = app.app.test_client()
        self._sp = app.STATUS_PAGE
        app.STATUS_PAGE = True
        # Seed a representative snapshot so the tiles/counts are non-trivial. Use
        # values that *would* leak if passed through naively (hostname, IP, mounts).
        self._host = app.LATEST.get("host")
        self._gpu_avail = app.LATEST.get("gpu_avail")
        self._docker = app.HEALTH.get("docker")
        self._systemd = app.HEALTH.get("systemd")
        self._at = app.HEALTH.get("at")
        app.LATEST["host"] = {
            "hostname": "secret-ardi-box", "ram_used": 8000, "ram_total": 16000,
            "cpu": 12, "ip": "192.168.1.50",
            "disks": [{"mount": "/data", "pct": 71, "used": 100, "total": 200},
                      {"mount": "/", "pct": 40}],
            "os": {"id": "opensuse", "pretty": "openSUSE Leap 16.1"},
        }
        app.LATEST["gpu_avail"] = True
        app.LATEST["util"] = 40
        app.LATEST["mem_used"] = 12000
        app.LATEST["mem_total"] = 24576
        app.HEALTH["docker"] = {"available": True,
                                "containers": [{"name": "immich_server", "status": "ok"}],
                                "summary": {"total": 12, "running": 11, "problems": 1}}
        app.HEALTH["systemd"] = {"available": True,
                                 "services": [{"name": "sshd"}, {"name": "nginx"}, {"name": "ollama"}],
                                 "summary": {"running": 3, "failed": 0}}
        app.HEALTH["at"] = int(time.time())

    def tearDown(self):
        app.STATUS_PAGE = self._sp
        app.LATEST["host"] = self._host
        app.LATEST["gpu_avail"] = self._gpu_avail
        app.HEALTH["docker"] = self._docker
        app.HEALTH["systemd"] = self._systemd
        app.HEALTH["at"] = self._at

    # ── page serves ───────────────────────────────────────────────────────────
    def test_status_html_200(self):
        r = self.c.get("/status")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/html", r.content_type)
        body = r.get_data(as_text=True)
        self.assertIn("/api/status", body)        # data-driven
        self.assertIn("HomeLab Monitor", body)

    # ── JSON shape ────────────────────────────────────────────────────────────
    def test_api_status_shape(self):
        r = self.c.get("/api/status")
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        for k in ("status", "updated", "now", "demo", "tiles", "gpu",
                  "anomaly_active", "counts"):
            self.assertIn(k, j, "missing top-level key %r" % k)
        self.assertIn(j["status"], ("operational", "degraded", "down"))
        self.assertIsInstance(j["tiles"], list)
        self.assertTrue(j["tiles"], "expected subsystem tiles")
        for t in j["tiles"]:
            self.assertIn("key", t)
            self.assertIn("status", t)
            self.assertIn(t["status"], ("ok", "warn", "crit", "info"))
        c = j["counts"]
        for k in ("services", "containers", "monitored", "problems"):
            self.assertIn(k, c)
        # counts reflect the seeded snapshot (3 services + 12 containers = 15)
        self.assertEqual(c["services"], 3)
        self.assertEqual(c["containers"], 12)
        self.assertEqual(c["monitored"], 15)
        self.assertEqual(c["problems"], 1)        # 1 docker problem, 0 failed
        # a docker problem degrades the banner away from full operational
        self.assertIn(j["status"], ("degraded", "down"))
        self.assertTrue(j["gpu"]["available"])
        self.assertTrue(j["gpu"]["busy"])         # util 40 >= 5

    # ── PRIVACY: no sensitive field may appear anywhere in the payload ────────
    def test_privacy_no_sensitive_keys(self):
        j = self.c.get("/api/status").get_json()
        leaves = list(_walk_strings(j))

        # 1) Known-sensitive dict KEYS must be absent at every depth.
        forbidden_keys = {
            "hostname", "host", "ip", "ipv4", "ipv6", "mac", "iface", "net",
            "disks", "mount", "mountpoint", "path", "os", "kernel", "distro",
            "models", "model", "procs", "processes",
            "webhook_url", "webhook", "token", "secret", "password", "api_key",
            "ssh_target", "ssh", "settings", "kwh_price", "currency", "cost",
            "tariff", "callers", "edges", "mcp", "update", "diagnostics",
            "name", "image", "ports", "detail", "metric",
        }
        present = {k for (_p, kind, k) in leaves if kind == "key"}
        leaked = forbidden_keys & present
        self.assertFalse(leaked, "public status leaked sensitive key(s): %s" % sorted(leaked))

        # 1b) The only `services`/`containers` allowed are anonymized integer
        #     counts under `counts` — never a name-bearing list anywhere.
        for p, kind, val in leaves:
            if kind == "key" and val in ("services", "containers"):
                self.assertTrue(p.endswith("counts") or p == "$.counts",
                                "%r appears outside counts at %s" % (val, p))
        for k in ("services", "containers", "monitored", "problems"):
            self.assertIsInstance(j["counts"][k], int)

        # 2) The literal seeded secret VALUES must not appear anywhere either.
        blob = json.dumps(j)
        for needle in ("secret-ardi-box", "192.168.1.50", "/data", "opensuse",
                       "immich_server", "sshd", "nginx", "ollama", "Leap"):
            self.assertNotIn(needle, blob,
                             "public status leaked sensitive value %r" % needle)

    # ── toggle ────────────────────────────────────────────────────────────────
    def test_disabled_returns_404(self):
        app.STATUS_PAGE = False
        self.assertEqual(self.c.get("/status").status_code, 404)
        self.assertEqual(self.c.get("/api/status").status_code, 404)

    # ── health surfaces the toggle ───────────────────────────────────────────
    def test_health_exposes_status_page_flag(self):
        j = self.c.get("/api/health").get_json()
        self.assertIn("status_page", j)
        self.assertEqual(j["status_page"], app.STATUS_PAGE)

    # ── a few failed units while the bulk run is "degraded", not "down" ───────
    def test_failed_units_degrade_not_down(self):
        # clean docker so services is the only red subsystem
        app.HEALTH["docker"] = {"available": True, "containers": [],
                                "summary": {"total": 12, "running": 12, "problems": 0}}
        app.HEALTH["systemd"] = {"available": True, "services": [],
                                 "summary": {"running": 90, "failed": 4}}
        j = self.c.get("/api/status").get_json()
        self.assertEqual(j["status"], "degraded",
                         "4 failed of 90 running should not paint the lab DOWN")
        # the services tile still honestly reports it needs attention
        svc = next(t for t in j["tiles"] if t["key"] == "services")
        self.assertEqual(svc["status"], "crit")
        self.assertEqual(svc["failed"], 4)

    def test_nothing_running_stays_down(self):
        app.HEALTH["docker"] = {"available": True, "containers": [],
                                "summary": {"total": 12, "running": 12, "problems": 0}}
        app.HEALTH["systemd"] = {"available": True, "services": [],
                                 "summary": {"running": 0, "failed": 4}}
        j = self.c.get("/api/status").get_json()
        self.assertEqual(j["status"], "down",
                         "nothing running is a genuine DOWN")

    # ── never 500s even with an empty/warming snapshot ───────────────────────
    def test_graceful_when_warming(self):
        app.LATEST["host"] = {}
        app.HEALTH["docker"] = None
        app.HEALTH["systemd"] = None
        r = self.c.get("/api/status")
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertIn("status", j)
        self.assertIsInstance(j["tiles"], list)


if __name__ == "__main__":
    unittest.main()
