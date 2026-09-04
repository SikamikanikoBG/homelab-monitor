"""Deterministic snapshot tests for all 54 HTTP endpoints in app.py.

Each test:
  1. Freezes time.time() to FROZEN_TS = 1735689600 (2025-01-01T00:00:00Z)
  2. Seeds or mocks any data the endpoint needs
  3. Hits the endpoint via the test client
  4. Calls assert_snapshot() — first run writes the baseline; subsequent runs compare

Run UPDATE_SNAPSHOTS=1 pytest tests/test_snapshots.py to regenerate baselines.
"""

import os
import re
import sys
import json
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app

from snapshot_helper import assert_snapshot, frozen_time, FROZEN_TS, SNAP_DIR

WDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# app.VERSION changes every release. Snapshots that would otherwise embed the
# literal replace it with this sentinel before comparing, so a version bump
# alone never breaks a snapshot — the real value is still asserted separately
# against app.VERSION at the call site.
VERSION_SENTINEL = "<VERSION>"


def _clean_db():
    with app.LOCK:
        for tbl in ("samples", "samples_1m", "samples_1h", "net_samples", "net_samples_1m", "net_samples_1h",
                    "proc", "models", "edges", "events", "disk_io_samples",
                    "runs", "run_metrics", "api_keys", "hosts",
                    "uptime_checks", "uptime_results", "maintenance_windows",
                    "notification_rules", "power_proc", "settings"):
            try:
                app.DB.execute(f"DELETE FROM {tbl}")
            except Exception:
                pass
        app.DB.commit()


def _seed_samples(n=3):
    """Insert n deterministic rows into the samples table.

    Seeded AFTER FROZEN_TS (not before) so they fall within the 'today' window
    on UTC CI where midnight == FROZEN_TS exactly.  The 1h-range query uses
    since = now - 3600 = FROZEN_TS - 3600, so these timestamps are still
    well inside every range window.
    """
    with app.LOCK:
        for i in range(n):
            ts = FROZEN_TS + (i + 1) * app.INTERVAL
            app.DB.execute(
                "INSERT INTO samples(ts,util,mem_used,mem_total,power,temp,cpu,ram_used,ram_total,load1,ctemp) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (ts, 40 + i, 8000, 24576, 120 + i * 10, 65, 30 + i, 16000, 32000, 1.5, 55)
            )
        app.DB.commit()
        app.DB.executescript("""
            INSERT OR IGNORE INTO samples_1h(ts,util,mem_used,mem_total,power,temp,
                cpu,ram_used,ram_total,load1,ctemp,cpu_power,dram_power,cnt)
            SELECT (ts/3600)*3600, AVG(util), AVG(mem_used), AVG(mem_total), AVG(power), AVG(temp),
                AVG(cpu), AVG(ram_used), AVG(ram_total), AVG(load1), AVG(ctemp),
                AVG(cpu_power), AVG(dram_power), COUNT(*)
            FROM samples GROUP BY (ts/3600)*3600;
        """)
        app.DB.commit()


def _seed_net_samples(n=2):
    with app.LOCK:
        for i in range(n):
            ts = FROZEN_TS - (n - i) * app.INTERVAL
            app.DB.execute(
                "INSERT INTO net_samples(ts,iface,bytes_in,bytes_out) VALUES(?,?,?,?)",
                (ts, "eth0", i * 1024, i * 512)
            )
        app.DB.commit()


def _mock_latest():
    return {
        "ts": FROZEN_TS, "util": 42, "mem_used": 8192, "mem_total": 24576,
        "power": 130, "temp": 66, "procs": [], "models": [], "callers": [],
        "host": {
            "cpu": 35, "cores": 8, "ram_used": 16000, "ram_total": 32000,
            "ram_kernel": 500, "load1": 1.5, "uptime": 86400,
            "ctemp": 55, "disks": [], "hostname": "testhost",
        },
        "gpu_avail": False, "gpus": [], "gpu_extra": {}, "model_meta": {},
        "serving": [], "training": [], "devtools": [], "model_catalog": [],
        "cpu_power": None, "dram_power": None,
    }


def _mock_health():
    return {
        "docker": {
            "available": True, "containers": [],
            "summary": {"total": 0, "running": 0, "problems": 0}
        },
        "systemd": {
            "available": True, "services": [], "summary": {}
        },
        "update": {"available": False, "current": app.VERSION},
        "processes": None,
        "at": FROZEN_TS,
        "disk_io": {"available": False, "warming_up": True,
                    "summary": {"total_read_mb_s": 0.0, "total_write_mb_s": 0.0},
                    "items": []},
    }


class TestSnapshots(unittest.TestCase):

    def setUp(self):
        self.client = app.app.test_client()
        _clean_db()

    # ─── /api/data ───────────────────────────────────────────────────────────

    def test_api_data(self):
        _seed_samples()
        with frozen_time(), \
             patch.object(app, "LATEST", _mock_latest()), \
             patch.object(app, "HEALTH", _mock_health()), \
             patch("app.uptime_summary", return_value={"total": 0, "up": 0, "down": 0, "unknown": 0, "worst_down": None}), \
             patch("app.uptime_insights", return_value=[]):
            r = self.client.get("/api/data?range=1h")
            data = r.get_json()
        self.assertEqual(data["version"], app.VERSION)
        data["version"] = VERSION_SENTINEL
        assert_snapshot(self, "api_data", data)

    # ─── /api/cost ───────────────────────────────────────────────────────────

    def test_api_cost(self):
        _seed_samples()
        with frozen_time(), \
             patch.object(app, "LATEST", _mock_latest()):
            r = self.client.get("/api/cost?range=1h")
            data = r.get_json()
        assert_snapshot(self, "api_cost", data)

    # ─── /api/costs ──────────────────────────────────────────────────────────

    def test_api_costs(self):
        _seed_samples()
        with frozen_time(), \
             patch.object(app, "LATEST", _mock_latest()):
            r = self.client.get("/api/costs?range=1h")
            data = r.get_json()
        assert_snapshot(self, "api_costs", data)

    # ─── /api/cost/heatmap ───────────────────────────────────────────────────

    def test_api_cost_heatmap(self):
        _seed_samples()
        with frozen_time(), \
             patch.object(app, "LATEST", _mock_latest()):
            r = self.client.get("/api/cost/heatmap?days=1")
            data = r.get_json()
        assert_snapshot(self, "api_cost_heatmap", data)

    # ─── /api/costs/entity ───────────────────────────────────────────────────

    def test_api_costs_entity(self):
        with frozen_time(), \
             patch.object(app, "LATEST", _mock_latest()):
            r = self.client.get("/api/costs/entity?name=test&kind=gpu&range=1h")
            data = r.get_json()
        assert_snapshot(self, "api_costs_entity", data)

    # ─── /api/sessions ───────────────────────────────────────────────────────

    def test_api_sessions(self):
        _seed_samples()
        with frozen_time(), \
             patch.object(app, "LATEST", _mock_latest()):
            r = self.client.get("/api/sessions?range=1h")
            data = r.get_json()
        assert_snapshot(self, "api_sessions", data)

    # ─── /api/models ─────────────────────────────────────────────────────────

    def test_api_models(self):
        with frozen_time(), \
             patch("app._model_registry", return_value=([], False)), \
             patch.object(app, "LATEST", _mock_latest()):
            r = self.client.get("/api/models")
            data = r.get_json()
        assert_snapshot(self, "api_models", data)

    # ─── /api/integration/keys GET ───────────────────────────────────────────

    def test_api_integration_keys_get(self):
        with frozen_time():
            r = self.client.get("/api/integration/keys")
            data = r.get_json()
        assert_snapshot(self, "api_integration_keys_get", data)

    # ─── /api/integration/keys POST ──────────────────────────────────────────

    def test_api_integration_keys_post(self):
        with frozen_time(), \
             patch("app.uuid.uuid4", return_value=MagicMock(hex="aaaa0000bbbb1111cccc2222dddd3333")), \
             patch("app.secrets.token_urlsafe", return_value="DETERMINISTIC_TOKEN_FOR_SNAPSHOT_TEST"):
            r = self.client.post("/api/integration/keys",
                                 json={"name": "snap-test-key"},
                                 content_type="application/json")
            data = r.get_json()
        # Strip the key value (it contains the mocked token, deterministic)
        assert_snapshot(self, "api_integration_keys_post", data)

    # ─── DELETE /api/integration/keys/<kid> ──────────────────────────────────

    def test_api_integration_keys_delete(self):
        # Create a key first, then delete it
        with frozen_time(), \
             patch("app.uuid.uuid4", return_value=MagicMock(hex="deadbeefdeadbeefdeadbeefdeadbeef")), \
             patch("app.secrets.token_urlsafe", return_value="SNAP_TOK"):
            self.client.post("/api/integration/keys",
                             json={"name": "to-delete"},
                             content_type="application/json")
        with frozen_time():
            r = self.client.delete("/api/integration/keys/deadbeefdeadbeefdeadbeefdeadbeef")
            data = r.get_json()
        assert_snapshot(self, "api_integration_keys_delete", data)

    # ─── POST /api/runs ───────────────────────────────────────────────────────

    def _make_api_key(self):
        """Create a key and return the plaintext."""
        with frozen_time(), \
             patch("app.uuid.uuid4", return_value=MagicMock(hex="keykeykeykeykeykeykeykeykeykeyke")), \
             patch("app.secrets.token_urlsafe", return_value="SNAP_APIKEY"):
            r = self.client.post("/api/integration/keys",
                                 json={"name": "snap"},
                                 content_type="application/json")
        return r.get_json()["key"]

    def test_api_runs_create(self):
        key = self._make_api_key()
        with frozen_time(), \
             patch("app.uuid.uuid4", return_value=MagicMock(hex="run1111111111111111111111111111")):
            r = self.client.post("/api/runs",
                                 json={"name": "snap-run", "source": "api",
                                       "started_at": FROZEN_TS},
                                 headers={"X-API-Key": key},
                                 content_type="application/json")
            data = r.get_json()
        assert_snapshot(self, "api_runs_create", data)

    # ─── PATCH /api/runs/<rid> ────────────────────────────────────────────────

    def test_api_runs_update(self):
        key = self._make_api_key()
        rid = "run2222222222222222222222222222"
        with app.LOCK:
            app.DB.execute(
                "INSERT INTO runs(id,name,source,status,started_at,created_at) VALUES(?,?,?,?,?,?)",
                (rid, "patch-run", "api", "running", FROZEN_TS, FROZEN_TS)
            )
            app.DB.commit()
        with frozen_time():
            r = self.client.patch(f"/api/runs/{rid}",
                                  json={"status": "finished", "ended_at": FROZEN_TS},
                                  headers={"X-API-Key": key},
                                  content_type="application/json")
            data = r.get_json()
        assert_snapshot(self, "api_runs_update", data)

    # ─── POST /api/runs/<rid>/metrics ────────────────────────────────────────

    def test_api_runs_metrics(self):
        key = self._make_api_key()
        rid = "run3333333333333333333333333333"
        with app.LOCK:
            app.DB.execute(
                "INSERT INTO runs(id,name,source,status,started_at,created_at) VALUES(?,?,?,?,?,?)",
                (rid, "metric-run", "api", "running", FROZEN_TS, FROZEN_TS)
            )
            app.DB.commit()
        with frozen_time():
            r = self.client.post(f"/api/runs/{rid}/metrics",
                                 json={"metrics": [{"key": "loss", "value": 0.42, "step": 1, "ts": FROZEN_TS}]},
                                 headers={"X-API-Key": key},
                                 content_type="application/json")
            data = r.get_json()
        assert_snapshot(self, "api_runs_metrics", data)

    # ─── POST /api/runs/<rid>/finish ─────────────────────────────────────────

    def test_api_runs_finish(self):
        key = self._make_api_key()
        rid = "run4444444444444444444444444444"
        with app.LOCK:
            app.DB.execute(
                "INSERT INTO runs(id,name,source,status,started_at,created_at) VALUES(?,?,?,?,?,?)",
                (rid, "finish-run", "api", "running", FROZEN_TS, FROZEN_TS)
            )
            app.DB.commit()
        with frozen_time():
            r = self.client.post(f"/api/runs/{rid}/finish",
                                 json={"status": "finished", "ended_at": FROZEN_TS},
                                 headers={"X-API-Key": key},
                                 content_type="application/json")
            data = r.get_json()
        assert_snapshot(self, "api_runs_finish", data)

    # ─── DELETE /api/runs/<rid> ───────────────────────────────────────────────

    def test_api_runs_delete(self):
        rid = "run5555555555555555555555555555"
        with app.LOCK:
            app.DB.execute(
                "INSERT INTO runs(id,name,source,status,started_at,created_at) VALUES(?,?,?,?,?,?)",
                (rid, "del-run", "api", "finished", FROZEN_TS, FROZEN_TS)
            )
            app.DB.commit()
        with frozen_time():
            r = self.client.delete(f"/api/runs/{rid}")
            data = r.get_json()
        assert_snapshot(self, "api_runs_delete", data)

    # ─── GET /api/runs ────────────────────────────────────────────────────────

    def test_api_runs_list(self):
        rid = "run6666666666666666666666666666"
        with app.LOCK:
            app.DB.execute(
                "INSERT INTO runs(id,name,source,status,started_at,ended_at,created_at) VALUES(?,?,?,?,?,?,?)",
                (rid, "list-run", "api", "finished", FROZEN_TS - 3600, FROZEN_TS, FROZEN_TS)
            )
            app.DB.commit()
        with frozen_time():
            r = self.client.get("/api/runs?range=1h")
            data = r.get_json()
        assert_snapshot(self, "api_runs_list", data)

    # ─── GET /api/runs/<rid> ─────────────────────────────────────────────────

    def test_api_runs_get(self):
        rid = "run7777777777777777777777777777"
        with app.LOCK:
            app.DB.execute(
                "INSERT INTO runs(id,name,source,status,started_at,ended_at,created_at) VALUES(?,?,?,?,?,?,?)",
                (rid, "get-run", "api", "finished", FROZEN_TS - 1800, FROZEN_TS, FROZEN_TS)
            )
            app.DB.commit()
        with frozen_time():
            r = self.client.get(f"/api/runs/{rid}")
            data = r.get_json()
        assert_snapshot(self, "api_runs_get", data)

    # ─── GET/POST /api/integration/mlflow/sync ───────────────────────────────

    def test_api_mlflow_sync_get(self):
        with frozen_time():
            r = self.client.get("/api/integration/mlflow/sync")
            data = r.get_json()
        assert_snapshot(self, "api_integration_mlflow_sync_get", data)

    # ─── GET /api/containers/<name>/logs ─────────────────────────────────────
    # This endpoint returns SSE stream; we just snapshot status + content-type.

    def test_api_containers_logs(self):
        with patch("app._docker_log_stream", return_value=iter([])):
            r = self.client.get("/api/containers/myapp/logs")
        data = {"status": r.status_code, "content_type": r.content_type.split(";")[0].strip()}
        assert_snapshot(self, "api_containers_logs", data)

    # ─── GET /api/network ────────────────────────────────────────────────────

    def test_api_network(self):
        _seed_net_samples()
        with frozen_time():
            r = self.client.get("/api/network?range=1h")
            data = r.get_json()
        assert_snapshot(self, "api_network", data)

    # ─── POST /api/integration/mlflow/sync ───────────────────────────────────

    def test_api_mlflow_sync_post(self):
        with app.LOCK:
            app.DB.execute(
                "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
                ("mlflow_uri", "http://mlflow.local:5000")
            )
            app.DB.commit()
        with frozen_time(), \
             patch("app.sync_mlflow", return_value=5):
            r = self.client.post("/api/integration/mlflow/sync")
            data = r.get_json()
        # Cleanup
        with app.LOCK:
            app.DB.execute("DELETE FROM settings WHERE key='mlflow_uri'")
            app.DB.commit()
        assert_snapshot(self, "api_integration_mlflow_sync_post", data)

    # ─── GET /healthz ─────────────────────────────────────────────────────────

    def test_healthz(self):
        r = self.client.get("/healthz")
        data = r.get_json()
        self.assertEqual(data["version"], app.VERSION)
        data["version"] = VERSION_SENTINEL
        assert_snapshot(self, "healthz", data)

    # ─── GET /api/changelog ──────────────────────────────────────────────────

    def test_api_changelog(self):
        r = self.client.get("/api/changelog")
        raw = r.get_json()
        # Snapshot only stable properties — full markdown text changes every release
        data = {
            "status": r.status_code,
            "has_content": bool(raw and raw.get("markdown")),
            "has_sections": bool(raw and raw.get("sections")),
            "markdown_prefix": (raw.get("markdown") or "")[:100],
        }

        # Real contract: the newest release heading the endpoint serves must
        # match the newest release heading in CHANGELOG.md, both parsed fresh
        # (not hardcoded on either side) so this survives every release.
        heading_re = re.compile(r"##\s*\[([\d.]+)\]")
        api_heading = heading_re.match((raw.get("markdown") or "").splitlines()[0])
        self.assertIsNotNone(api_heading, "changelog endpoint did not return a version heading")
        changelog_path = os.path.join(WDIR, "CHANGELOG.md")
        with open(changelog_path, encoding="utf-8") as f:
            changelog_text = f.read()
        first_release_line = next(
            line for line in changelog_text.splitlines() if heading_re.match(line)
        )
        changelog_heading = heading_re.match(first_release_line)
        self.assertEqual(api_heading.group(1), changelog_heading.group(1))

        # Sentinel out the version literal (appears in both the heading and its
        # release-tag URL) and the release date, so the snapshot no longer
        # breaks on a version bump or a same-day-next-year re-release.
        prefix = re.sub(r"\d+\.\d+\.\d+", VERSION_SENTINEL, data["markdown_prefix"])
        prefix = re.sub(r"\d{4}-\d{2}-\d{2}", "<DATE>", prefix)
        data["markdown_prefix"] = prefix
        assert_snapshot(self, "api_changelog", data)

    # ─── GET /favicon.ico ────────────────────────────────────────────────────

    def test_favicon(self):
        r = self.client.get("/favicon.ico")
        data = {"status": r.status_code}
        assert_snapshot(self, "favicon", data)

    # ─── GET /locales/<path:fn> ───────────────────────────────────────────────

    def test_locales(self):
        r = self.client.get("/locales/nonexistent.json")
        data = {"status": r.status_code}
        assert_snapshot(self, "locales_notfound", data)

    # ─── GET /api/mcp-status ─────────────────────────────────────────────────

    def test_api_mcp_status(self):
        with patch("app._mcp_probe", return_value=False), \
             patch("app._mcp_enabled", return_value=False):
            r = self.client.get("/api/mcp-status")
            data = r.get_json()
        assert_snapshot(self, "api_mcp_status", data)

    # ─── GET /api/health ─────────────────────────────────────────────────────

    def test_api_health(self):
        ml = _mock_latest()
        mh = _mock_health()
        with frozen_time(), \
             patch.object(app, "LATEST", ml), \
             patch.object(app, "HEALTH", mh), \
             patch("app.os_updates_summary", return_value={"available": 0}), \
             patch("app.local_diagnostics", return_value=[]), \
             patch("app.enrich_os_upgrade", side_effect=lambda x: x):
            r = self.client.get("/api/health")
            data = r.get_json()
        self.assertEqual(data["version"], app.VERSION)
        self.assertEqual(data["update"]["current"], app.VERSION)
        data["version"] = VERSION_SENTINEL
        data["update"]["current"] = VERSION_SENTINEL
        assert_snapshot(self, "api_health", data)

    # ─── GET /metrics ─────────────────────────────────────────────────────────

    def test_metrics(self):
        ml = _mock_latest()
        mh = _mock_health()
        with patch.object(app, "LATEST", ml), \
             patch.object(app, "HEALTH", mh):
            r = self.client.get("/metrics")
        if r.status_code == 503:
            data = {"status": 503, "note": "prometheus_client not installed"}
        else:
            # Prometheus text format — snapshot just status and content-type
            data = {"status": r.status_code, "content_type": r.content_type.split(";")[0].strip()}
        assert_snapshot(self, "metrics", data)

    # ─── GET /api/hub/pubkey ─────────────────────────────────────────────────

    def test_api_hub_pubkey(self):
        with patch("app.get_hub_pubkey", return_value="ssh-ed25519 AAAATESTKEY testhost"):
            r = self.client.get("/api/hub/pubkey")
            data = r.get_json()
        assert_snapshot(self, "api_hub_pubkey", data)

    # ─── GET /api/hosts ───────────────────────────────────────────────────────

    def test_api_hosts_get(self):
        r = self.client.get("/api/hosts")
        data = r.get_json()
        assert_snapshot(self, "api_hosts_get", data)

    # ─── POST /api/hosts ─────────────────────────────────────────────────────

    def test_api_hosts_post(self):
        with patch("app.uuid.uuid4", return_value=MagicMock(hex="hostaaaabbbbccccddddeeeeffffaaaa")):
            r = self.client.post("/api/hosts",
                                 json={"name": "snap-host", "ssh_target": "user@192.168.1.100"},
                                 content_type="application/json")
            data = r.get_json()
        assert_snapshot(self, "api_hosts_post", data)

    # ─── DELETE /api/hosts/<name> ─────────────────────────────────────────────

    def test_api_hosts_delete(self):
        with patch("app.uuid.uuid4", return_value=MagicMock(hex="hostbbbbccccddddeeeeffffaaaabbbb")):
            self.client.post("/api/hosts",
                             json={"name": "del-host", "ssh_target": "user@10.0.0.1"},
                             content_type="application/json")
        r = self.client.delete("/api/hosts/del-host")
        data = r.get_json()
        assert_snapshot(self, "api_hosts_delete", data)

    # ─── PATCH /api/hosts/<name> ─────────────────────────────────────────────

    def test_api_hosts_patch(self):
        with patch("app.uuid.uuid4", return_value=MagicMock(hex="hostccccddddeeeeffffaaaabbbbcccc")):
            self.client.post("/api/hosts",
                             json={"name": "patch-host", "ssh_target": "user@10.0.0.2"},
                             content_type="application/json")
        r = self.client.patch("/api/hosts/patch-host",
                              json={"ssh_target": "user@10.0.0.99"},
                              content_type="application/json")
        data = r.get_json()
        assert_snapshot(self, "api_hosts_patch", data)

    # ─── GET /api/lan/scan ────────────────────────────────────────────────────

    def test_api_lan_scan(self):
        with patch("app.discover_lan", return_value={"hosts": []}):
            r = self.client.get("/api/lan/scan")
            data = r.get_json()
        assert_snapshot(self, "api_lan_scan", data)

    # ─── GET /api/host_data/<name> ───────────────────────────────────────────

    def test_api_host_data_local(self):
        with frozen_time(), \
             patch("app._local_now_snapshot", return_value={"cpu": 30, "hostname": "testhost"}), \
             patch("app.enrich_os_upgrade", side_effect=lambda x: x):
            r = self.client.get("/api/host_data/local")
            data = r.get_json()
        assert_snapshot(self, "api_host_data_local", data)

    def test_api_host_data_missing(self):
        with frozen_time():
            r = self.client.get("/api/host_data/nonexistent")
            data = r.get_json()
        assert_snapshot(self, "api_host_data_missing", data)

    # ─── GET /api/disk_scan ───────────────────────────────────────────────────

    def test_api_disk_scan_bad_path(self):
        with patch("app._safe_host_dir", return_value=None):
            r = self.client.get("/api/disk_scan?path=/no/such/dir")
            data = r.get_json()
        assert_snapshot(self, "api_disk_scan_bad_path", data)

    def test_api_disk_scan_scanning(self):
        with patch("app._safe_host_dir", return_value="/tmp"), \
             patch.dict(app._DISK_SCAN, {app._disk_scan_key("local", "/tmp"):
                                         {"state": "scanning", "at": FROZEN_TS, "path": "/tmp"}}):
            r = self.client.get("/api/disk_scan?path=/tmp")
            data = r.get_json()
        assert_snapshot(self, "api_disk_scan_scanning", data)

    def test_api_disk_scan_done(self):
        done_entry = {
            "state": "done",
            "at": FROZEN_TS,
            "total": 1024,
            "free": 512,
            "error": None,
            "entries": [{"name": "foo", "size": 512, "type": "file"}],
        }
        with frozen_time(), \
             patch("app._safe_host_dir", return_value="/tmp"), \
             patch.dict(app._DISK_SCAN, {app._disk_scan_key("local", "/tmp"): done_entry}):
            r = self.client.get("/api/disk_scan?path=/tmp")
            data = r.get_json()
        assert_snapshot(self, "api_disk_scan_done", data)

    # ─── GET /api/fleet ───────────────────────────────────────────────────────

    def test_api_fleet(self):
        with frozen_time(), \
             patch("app._local_now_snapshot", return_value={"cpu": 30, "hostname": "testhost"}), \
             patch("app.enrich_os_upgrade", side_effect=lambda x: x), \
             patch("app.socket.gethostname", return_value="testhost"):
            r = self.client.get("/api/fleet")
            data = r.get_json()
        assert_snapshot(self, "api_fleet", data)

    # ─── POST /api/hosts/<name>/test ─────────────────────────────────────────

    def test_api_hosts_test(self):
        with patch("app.probe_host", return_value=None):
            r = self.client.post("/api/hosts/nonexistent/test")
            data = r.get_json()
        assert_snapshot(self, "api_hosts_test", data)

    def test_api_hosts_test_success(self):
        with patch("app.uuid.uuid4", return_value=MagicMock(hex="hosttestsuccessaabbccddeeff0011")):
            self.client.post("/api/hosts",
                             json={"name": "testhost-snap", "ssh_target": "user@10.0.0.5"},
                             content_type="application/json")
        probe_result = {"ok": True, "latency_ms": 12, "ssh": True}
        with patch("app.probe_host", return_value=probe_result):
            r = self.client.post("/api/hosts/testhost-snap/test")
            data = r.get_json()
        assert_snapshot(self, "api_hosts_test_success", data)

    # ─── POST /api/hosts/<name>/run ──────────────────────────────────────────

    def test_api_hosts_run(self):
        with patch("app.run_on_host", return_value=None):
            r = self.client.post("/api/hosts/nonexistent/run",
                                 json={"cmd": "hostname"},
                                 content_type="application/json")
            data = r.get_json()
        assert_snapshot(self, "api_hosts_run", data)

    def test_api_hosts_run_success(self):
        with patch("app.uuid.uuid4", return_value=MagicMock(hex="hostrunsuccessaabbccddeeff0022")):
            self.client.post("/api/hosts",
                             json={"name": "testhost-run", "ssh_target": "user@10.0.0.6"},
                             content_type="application/json")
        run_result = {"ok": True, "stdout": "testhost-run\n", "stderr": "", "rc": 0}
        with patch("app.run_on_host", return_value=run_result):
            r = self.client.post("/api/hosts/testhost-run/run",
                                 json={"cmd": "hostname"},
                                 content_type="application/json")
            data = r.get_json()
        assert_snapshot(self, "api_hosts_run_success", data)

    # ─── GET /api/backup ─────────────────────────────────────────────────────

    def test_api_backup(self):
        with patch("app._data_dir_writable", return_value=False):
            r = self.client.get("/api/backup")
            data = r.get_json()
        assert_snapshot(self, "api_backup", data)

    # ─── POST /api/backup/restore ────────────────────────────────────────────

    def test_api_backup_restore(self):
        with patch("app._data_dir_writable", return_value=False):
            r = self.client.post("/api/backup/restore")
            data = r.get_json()
        assert_snapshot(self, "api_backup_restore", data)

    # ─── GET /api/settings ───────────────────────────────────────────────────

    def test_api_settings_get(self):
        r = self.client.get("/api/settings")
        data = r.get_json()
        self.assertEqual(data["version"], app.VERSION)
        data["version"] = VERSION_SENTINEL
        assert_snapshot(self, "api_settings_get", data)

    # ─── POST /api/settings ──────────────────────────────────────────────────

    def test_api_settings_post(self):
        r = self.client.post("/api/settings",
                             json={"currency": "€"},
                             content_type="application/json")
        data = r.get_json()
        # Reset so other tests aren't affected
        self.client.post("/api/settings", json={"currency": "$"},
                         content_type="application/json")
        self.assertEqual(data["version"], app.VERSION)
        data["version"] = VERSION_SENTINEL
        assert_snapshot(self, "api_settings_post", data)

    # ─── POST /api/notify/test ────────────────────────────────────────────────

    def test_api_notify_test(self):
        # No channels configured → expect 400
        r = self.client.post("/api/notify/test")
        data = r.get_json()
        assert_snapshot(self, "api_notify_test", data)

    # ─── GET /api/uptime ─────────────────────────────────────────────────────

    def test_api_uptime_get(self):
        with frozen_time():
            r = self.client.get("/api/uptime")
            data = r.get_json()
        assert_snapshot(self, "api_uptime_get", data)

    # ─── POST /api/uptime ────────────────────────────────────────────────────

    def test_api_uptime_post(self):
        with frozen_time(), \
             patch("app.uuid.uuid4", return_value=MagicMock(hex="uptimeaabbccddeeff0011223344aabb")):
            r = self.client.post("/api/uptime",
                                 json={"label": "snap-check", "type": "http",
                                       "target": "https://example.com",
                                       "interval_sec": 60},
                                 content_type="application/json")
            data = r.get_json()
        assert_snapshot(self, "api_uptime_post", data)

    # ─── PATCH /api/uptime/<cid> ─────────────────────────────────────────────

    def test_api_uptime_patch(self):
        with frozen_time(), \
             patch("app.uuid.uuid4", return_value=MagicMock(hex="uptimepatchaabbccddeeff00112233")):
            cr = self.client.post("/api/uptime",
                                  json={"label": "patch-check", "type": "http",
                                        "target": "https://example.com"},
                                  content_type="application/json")
            cid = cr.get_json()["id"]
        with frozen_time():
            r = self.client.patch(f"/api/uptime/{cid}",
                                  json={"enabled": False},
                                  content_type="application/json")
            data = r.get_json()
        assert_snapshot(self, "api_uptime_patch", data)

    # ─── DELETE /api/uptime/<cid> ────────────────────────────────────────────

    def test_api_uptime_delete(self):
        with frozen_time(), \
             patch("app.uuid.uuid4", return_value=MagicMock(hex="uptimedelaabbccddeeff001122334")):
            cr = self.client.post("/api/uptime",
                                  json={"label": "del-check", "type": "http",
                                        "target": "https://example.com"},
                                  content_type="application/json")
            cid = cr.get_json()["id"]
        with frozen_time():
            r = self.client.delete(f"/api/uptime/{cid}")
            data = r.get_json()
        assert_snapshot(self, "api_uptime_delete", data)

    # ─── GET /api/maintenance ─────────────────────────────────────────────────

    def test_api_maintenance_get(self):
        r = self.client.get("/api/maintenance")
        data = r.get_json()
        assert_snapshot(self, "api_maintenance_get", data)

    # ─── POST /api/maintenance ────────────────────────────────────────────────

    def test_api_maintenance_post(self):
        with frozen_time(), \
             patch("app.uuid.uuid4", return_value=MagicMock(hex="maintaabbccddeeff001122334455aa")):
            r = self.client.post("/api/maintenance",
                                 json={"label": "snap-window",
                                       "start_ts": FROZEN_TS,
                                       "end_ts": FROZEN_TS + 3600},
                                 content_type="application/json")
            data = r.get_json()
        assert_snapshot(self, "api_maintenance_post", data)

    # ─── DELETE /api/maintenance/<wid> ────────────────────────────────────────

    def test_api_maintenance_delete(self):
        with frozen_time(), \
             patch("app.uuid.uuid4", return_value=MagicMock(hex="maintdelaabbccddeeff0011223344")):
            cr = self.client.post("/api/maintenance",
                                  json={"label": "del-window",
                                        "start_ts": FROZEN_TS,
                                        "end_ts": FROZEN_TS + 3600},
                                  content_type="application/json")
            wid = cr.get_json()["id"]
        r = self.client.delete(f"/api/maintenance/{wid}")
        data = r.get_json()
        assert_snapshot(self, "api_maintenance_delete", data)

    # ─── POST /api/update/app ────────────────────────────────────────────────

    def test_api_update_app(self):
        with patch("app.start_self_update", return_value=(400, {"ok": False, "error": "disabled"})):
            r = self.client.post("/api/update/app")
            data = r.get_json()
        assert_snapshot(self, "api_update_app", data)

    # ─── GET /api/update/app/status ──────────────────────────────────────────

    def test_api_update_app_status(self):
        with patch("app._read_update_state", return_value=None):
            r = self.client.get("/api/update/app/status")
            data = r.get_json()
        assert_snapshot(self, "api_update_app_status", data)

    # ─── GET /api/notify/rules ───────────────────────────────────────────────

    def test_api_notify_rules_get(self):
        r = self.client.get("/api/notify/rules")
        data = r.get_json()
        assert_snapshot(self, "api_notify_rules_get", data)

    # ─── POST /api/notify/rules ──────────────────────────────────────────────

    def test_api_notify_rules_post(self):
        r = self.client.post("/api/notify/rules",
                             json={"action": "add", "match_kind": "container",
                                   "match_pattern": "myapp", "channel": "all",
                                   "min_level": "warning", "enabled": True},
                             content_type="application/json")
        data = r.get_json()
        assert_snapshot(self, "api_notify_rules_post", data)

    # ─── POST /api/notify/rules/test ─────────────────────────────────────────

    def test_api_notify_rules_test(self):
        # No channels configured → 400
        r = self.client.post("/api/notify/rules/test",
                             json={"match_kind": "container", "match_pattern": "*"},
                             content_type="application/json")
        data = r.get_json()
        assert_snapshot(self, "api_notify_rules_test", data)

    # ─── GET / ────────────────────────────────────────────────────────────────

    def test_index(self):
        r = self.client.get("/")
        data = {"status": r.status_code, "content_type": r.content_type.split(";")[0].strip()}
        assert_snapshot(self, "index", data)

    # ─── GET /api/brief/preview ──────────────────────────────────────────────

    def test_api_brief_preview(self):
        with frozen_time(), \
             patch.object(app, "LATEST", _mock_latest()), \
             patch.object(app, "HEALTH", _mock_health()), \
             patch("app.uptime_overview", return_value={"checks": [], "now": FROZEN_TS,
                                                         "window": 86400,
                                                         "min_interval": 30,
                                                         "max_timeout": 30}), \
             patch("app.socket.gethostname", return_value="testhost"), \
             patch("app.enrich_os_upgrade", side_effect=lambda x: x):
            r = self.client.get("/api/brief/preview?theme=dark")
        data = {"status": r.status_code, "content_type": r.content_type.split(";")[0].strip()}
        assert_snapshot(self, "api_brief_preview", data)

    # ─── POST /api/brief/test ────────────────────────────────────────────────

    def test_api_brief_test(self):
        # No channel configured → 400
        r = self.client.post("/api/brief/test",
                             json={"channel": "discord"},
                             content_type="application/json")
        data = r.get_json()
        assert_snapshot(self, "api_brief_test", data)

    # ─── GET /api/public-status ───────────────────────────────────────────────
    # Requires PUBLIC_STATUS env var to be set

    def test_api_public_status(self):
        with frozen_time(), \
             patch.dict(os.environ, {"PUBLIC_STATUS": "1"}), \
             patch.object(app, "LATEST", _mock_latest()), \
             patch.object(app, "HEALTH", _mock_health()), \
             patch("app.enrich_os_upgrade", side_effect=lambda x: x):
            r = self.client.get("/api/public-status")
            data = r.get_json()
        assert_snapshot(self, "api_public_status", data)

    def test_api_public_status_disabled(self):
        # Without PUBLIC_STATUS set → 404
        env = {k: v for k, v in os.environ.items() if k != "PUBLIC_STATUS"}
        with patch.dict(os.environ, env, clear=True):
            r = self.client.get("/api/public-status")
        data = {"status": r.status_code}
        assert_snapshot(self, "api_public_status_disabled", data)

    # ─── GET /api/public-status/<cid> ────────────────────────────────────────

    def test_api_public_status_one(self):
        with frozen_time(), \
             patch.dict(os.environ, {"PUBLIC_STATUS": "1"}), \
             patch("app._public_status_detail", return_value=None):
            r = self.client.get("/api/public-status/nonexistent")
        data = {"status": r.status_code}
        assert_snapshot(self, "api_public_status_one_notfound", data)

    def test_api_public_status_one_found(self):
        detail = {
            "id": "check-snap-001",
            "label": "Snap Service",
            "type": "http",
            "host": "example.com",
            "state": "up",
            "last_latency_ms": 42,
            "last_checked": FROZEN_TS,
            "interval_sec": 60,
            "up_since": FROZEN_TS - 3600,
            "uptime": {"24h": 100.0, "7d": 99.9, "30d": 99.8, "90d": 99.7},
            "daily": [],
            "response_series": [],
            "incidents": [],
            "cert_days_remaining": None,
            "cert_expires_at": None,
            "cert_status": None,
        }
        with frozen_time(), \
             patch.dict(os.environ, {"PUBLIC_STATUS": "1"}), \
             patch("app._public_status_detail", return_value=detail):
            r = self.client.get("/api/public-status/check-snap-001")
            data = r.get_json()
        assert_snapshot(self, "api_public_status_one_found", data)

    # ─── GET /public ─────────────────────────────────────────────────────────

    def test_public(self):
        with patch.dict(os.environ, {"PUBLIC_STATUS": "1"}):
            r = self.client.get("/public")
        data = {"status": r.status_code, "content_type": r.content_type.split(";")[0].strip()}
        assert_snapshot(self, "public", data)

    def test_public_disabled(self):
        env = {k: v for k, v in os.environ.items() if k != "PUBLIC_STATUS"}
        with patch.dict(os.environ, env, clear=True):
            r = self.client.get("/public")
        data = {"status": r.status_code}
        assert_snapshot(self, "public_disabled", data)

    # ─── GET /public/<cid> ────────────────────────────────────────────────────

    def test_public_cid(self):
        with patch.dict(os.environ, {"PUBLIC_STATUS": "1"}):
            r = self.client.get("/public/some-check-id")
        data = {"status": r.status_code, "content_type": r.content_type.split(";")[0].strip()}
        assert_snapshot(self, "public_cid", data)


if __name__ == "__main__":
    unittest.main()
