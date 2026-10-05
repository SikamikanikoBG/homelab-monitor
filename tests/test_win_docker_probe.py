"""Unit tests for the Windows-host Docker support.

Two layers are covered here:

1. The Docker capability check in probe_host() (the original regression
   coverage below): a Docker Desktop client/engine API-version mismatch is a
   genuine "daemon is unhealthy" condition, but its error message contains the
   word "error" — which used to get misclassified as "Docker CLI not on PATH"
   (status: info, "not installed") instead of "CLI present, daemon not
   responding" (status: warn).

2. The docker *inventory* probe.ps1 now emits (probe_host_metrics). Windows
   hosts used to ship no `docker` key at all, so the per-host Containers tab
   stayed empty on them; probe.ps1 now emits the same JSON shape probe.py does
   for Linux, and these tests pin that shape so the hub's Linux code path keeps
   rendering Windows hosts unchanged.

All SSH calls are mocked — no real network, and no PowerShell, in CI.
"""
import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app
from backend.probes import probe_host_metrics


CID1 = "a" * 64
CID2 = "b" * 64

# The exact key set probe.py's read_docker() emits for a running container that
# got stats + size + restart policy. probe.ps1 must match it field for field, or
# the hub's Containers tab renders a Windows host differently from a Linux one.
LINUX_RUNNING_KEYS = {"id", "name", "image", "state", "status", "ports",
                      "uptime", "restart_policy", "disk_bytes", "mem_bytes",
                      "cpu_pct"}
LINUX_EXITED_KEYS = {"id", "name", "image", "state", "status", "ports",
                     "uptime", "restart_policy", "disk_bytes"}


def _add_host(name="work", ssh_target="user@1.2.3.4"):
    with app.LOCK:
        app.DB.execute("DELETE FROM hosts WHERE name=?", (name,))
        app.DB.execute("INSERT INTO hosts(name, ssh_target, added_at) VALUES(?,?,0)",
                       (name, ssh_target))
        app.DB.commit()


class TestWindowsDockerProbe(unittest.TestCase):
    def setUp(self):
        _add_host()

    def tearDown(self):
        with app.LOCK:
            app.DB.execute("DELETE FROM hosts WHERE name=?", ("work",))
            app.DB.commit()

    def _run_probe(self, docker_stdout):
        """Drive probe_host() down the Windows branch with a canned response
        for the Docker capability check specifically."""
        with patch.object(app, "_tcp_probe", return_value=(True, None)), \
             patch.object(app, "_ensure_ssh_keypair"), \
             patch.object(app, "_detect_os", return_value={"family": "windows", "label": "Windows 11"}), \
             patch.object(app, "_ssh", return_value=(0, "ok", "", 5)), \
             patch.object(app, "_ssh_with_stdin") as m:
            def side_effect(user, host, port, cmd, stdin_bytes, timeout=60):
                # Match on DOCKER_ABSENT rather than "docker": the capability
                # check's script contains that marker, but probe.ps1 now
                # contains the word "docker" too (it probes the inventory), so
                # the looser match would hand probe.ps1's call this canned
                # response as well.
                if b"DOCKER_ABSENT" in stdin_bytes:
                    return (0, docker_stdout, "", 5)
                if b"LastBootUpTime" in stdin_bytes:
                    return (0, "up=123", "", 5)
                if b"nvidia-smi" in stdin_bytes:
                    return (0, "missing", "", 5)
                return (0, "", "", 5)
            m.side_effect = side_effect
            return app.probe_host("work")

    def _docker_check(self, result):
        return next(c for c in result["checks"] if c["id"] == "docker")

    def test_daemon_error_is_warn_not_absent(self):
        """The exact real-world failure: CLI present, daemon returns an API
        version-mismatch error containing the word 'error'. Must NOT be
        reported as 'Docker CLI not on PATH'."""
        dk = self._docker_check(self._run_probe(
            "DOCKER_ERR:request returned 500 Internal Server Error for API route "
            "and version http://%2F%2F.%2Fpipe%2FdockerDesktopLinuxEngine/v1.54/version, "
            "check if the server supports the requested API version"))
        self.assertEqual(dk["status"], "warn")
        self.assertIn("daemon didn't respond cleanly", dk["detail"])
        self.assertIn("remedy", dk)

    def test_cli_absent_is_info(self):
        dk = self._docker_check(self._run_probe("DOCKER_ABSENT"))
        self.assertEqual(dk["status"], "info")
        self.assertIn("not found on PATH", dk["detail"])

    def test_cli_ok_reports_server_version(self):
        dk = self._docker_check(self._run_probe("DOCKER_OK:27.3.1"))
        self.assertEqual(dk["status"], "ok")
        self.assertIn("27.3.1", dk["detail"])


class TestWindowsDockerInventory(unittest.TestCase):
    """probe_host_metrics() must hand back the docker section probe.ps1 emits,
    unchanged — Windows hosts render through the same code path as Linux ones."""

    def _metrics(self, probe_stdout, rc=0):
        """Drive probe_host_metrics() down the Windows branch with a canned
        probe.ps1 response."""
        with patch.object(app, "_ssh_with_stdin", return_value=(rc, probe_stdout, "", 5)):
            return probe_host_metrics("user", "1.2.3.4", 22, family="windows")

    @staticmethod
    def _probe_payload(containers, summary=None, version="win-0.3"):
        return json.dumps({
            "host": {
                "hostname": "WORKSTATION",
                "cpu": 12.5, "cores": 16,
                "ram_total": 32768, "ram_used": 8192, "uptime": 123456,
                "docker": {
                    "available": True,
                    "containers": containers,
                    "summary": summary or {"total": len(containers),
                                           "running": sum(1 for c in containers
                                                          if c["state"] == "running"),
                                           "problems": 0},
                },
            },
            "model_catalog": [],
            "at": 1700000000,
            "probe_version": version,
        })

    def test_running_and_exited_containers_match_the_linux_shape(self):
        """The whole point of the change: a Windows host now yields a docker
        inventory whose keys are the ones the hub already reads for Linux."""
        payload = self._probe_payload([
            {"id": CID1, "name": "ollama", "image": "ollama/ollama:latest",
             "state": "running", "status": "Up 2 days",
             "ports": "0.0.0.0:11434->11434/tcp", "uptime": "2 days",
             "restart_policy": "always", "disk_bytes": 2500000,
             "mem_bytes": 2 * 1024**3, "cpu_pct": 12.5},
            {"id": CID2, "name": "dead", "image": "old:1",
             "state": "exited", "status": "Exited (137) 3 hours ago",
             "ports": "", "uptime": "",
             "restart_policy": "no", "disk_bytes": 0},
        ], summary={"total": 2, "running": 1, "problems": 1})
        data, err, _ms, _to = self._metrics(payload)
        self.assertIsNone(err)
        dk = data["host"]["docker"]
        self.assertTrue(dk["available"])
        self.assertEqual(dk["summary"], {"total": 2, "running": 1, "problems": 1})
        byname = {c["name"]: c for c in dk["containers"]}
        self.assertEqual(set(byname["ollama"]), LINUX_RUNNING_KEYS)
        self.assertEqual(set(byname["dead"]), LINUX_EXITED_KEYS)
        # " ago" stripped, and an exited container carries no live stats.
        self.assertEqual(byname["ollama"]["uptime"], "2 days")
        self.assertEqual(byname["dead"]["uptime"], "")
        self.assertEqual(byname["ollama"]["mem_bytes"], 2 * 1024**3)
        self.assertEqual(byname["ollama"]["cpu_pct"], 12.5)

    def test_host_without_docker_omits_the_key(self):
        """A host with no CLI (or a wedged daemon) emits no docker key at all —
        the Containers panel stays hidden, exactly as on a Linux host without
        docker. The hub must not choke on the missing section."""
        payload = json.dumps({
            "host": {"hostname": "WORKSTATION", "cpu": 1.0, "cores": 4},
            "model_catalog": [], "at": 1700000000, "probe_version": "win-0.3",
        })
        data, err, _ms, _to = self._metrics(payload)
        self.assertIsNone(err)
        self.assertNotIn("docker", data["host"])

    def test_empty_inventory_still_reports_available(self):
        """Docker running with zero containers is available, not absent — the
        panel should render empty rather than disappear."""
        payload = self._probe_payload([], summary={"total": 0, "running": 0, "problems": 0})
        data, _err, _ms, _to = self._metrics(payload)
        dk = data["host"]["docker"]
        self.assertTrue(dk["available"])
        self.assertEqual(dk["containers"], [])

    def test_bad_json_is_reported_not_raised(self):
        """A probe.ps1 that dies mid-write must surface as an error, not blow
        up the poll cycle."""
        data, err, _ms, _to = self._metrics("{\"host\": {truncated")
        self.assertIsNone(data)
        self.assertIn("bad JSON", err)

    def test_ps1_source_wires_the_section_in(self):
        """CI has no PowerShell, so the script itself can't be executed here —
        this pins the two edits that make the section reach the payload."""
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "probe.ps1")
        with open(path, "r", encoding="utf-8") as f:
            src = f.read()
        self.assertIn("function Read-Docker", src)
        self.assertIn("Merge (Read-Docker)", src)
        # The bytes helper mirrors probe.py's _MEM_UNITS; a 1024-for-kB slip
        # would silently inflate every reported size by 2.4%.
        self.assertIn("'kb' = 1000", src)
        self.assertIn("'kib' = 1024", src)


if __name__ == "__main__":
    unittest.main()
