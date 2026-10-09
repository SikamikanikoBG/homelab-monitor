import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


class TestFleetPayloadAssets(unittest.TestCase):
    def test_exposes_local_and_remote_gpu_cards_and_containers(self):
        local_gpus = [{"idx": 0, "name": "local-gpu", "util": 12}]
        local_containers = [{"name": "local-app", "status": "ok"}]
        remote_gpus = [{"idx": 1, "name": "remote-gpu", "util": 88}]
        remote_containers = [{"name": "ollama", "status": "ok"}]
        remote_host = {
            "gpus": remote_gpus,
            "docker": {"available": True, "containers": remote_containers},
        }
        local_docker = {"available": True, "containers": local_containers}
        remote_entry = {"data": {"host": remote_host}, "at": 123, "error": None}
        hosts = [{"name": "remote", "ssh_target": "user@remote", "last_check": None}]

        # The hub's own inventory lives in HEALTH (collect_docker), never in
        # LATEST["host"] — seed it there, as production does.
        with patch.object(app, "LATEST", {
            "gpus": local_gpus,
            "host": {},
        }), patch.dict(app.HEALTH, {"docker": local_docker}), \
             patch.object(app, "list_hosts", return_value=hosts), \
             patch.dict(app.HOST_DATA, {"remote": remote_entry}, clear=True), \
             patch.object(app, "enrich_os_upgrade", side_effect=lambda value: value), \
             patch.object(app, "_host_is_online", return_value=True), \
             patch.object(app, "gpu_telemetry", return_value=None), \
             patch.object(app.socket, "gethostname", return_value="hub"):
            payload = app.fleet_payload()

        self.assertEqual(payload["hosts"][0]["gpus"], local_gpus)
        self.assertEqual(payload["hosts"][0]["containers"], local_containers)
        self.assertEqual(payload["hosts"][1]["gpus"], remote_gpus)
        self.assertEqual(payload["hosts"][1]["containers"], remote_containers)

    def test_missing_assets_degrade_to_empty_arrays(self):
        with patch.object(app, "LATEST", {"gpus": [], "host": {}}), \
             patch.dict(app.HEALTH, {"docker": None}), \
             patch.object(app, "list_hosts", return_value=[{"name": "remote", "ssh_target": "u@r", "last_check": None}]), \
             patch.dict(app.HOST_DATA, {"remote": {"data": {"host": {}}, "at": 1}}, clear=True), \
             patch.object(app, "enrich_os_upgrade", side_effect=lambda value: value), \
             patch.object(app, "_host_is_online", return_value=False), \
             patch.object(app, "gpu_telemetry", return_value=None), \
             patch.object(app.socket, "gethostname", return_value="hub"):
            payload = app.fleet_payload()

        for row in payload["hosts"]:
            self.assertEqual(row["gpus"], [])
            self.assertEqual(row["containers"], [])

    def test_local_docker_unavailable_degrades_to_empty(self):
        unavailable = {"available": False, "reason": "Docker API unreachable",
                       "containers": [], "summary": {"total": 0}}
        with patch.object(app, "LATEST", {"gpus": [], "host": {}}), \
             patch.dict(app.HEALTH, {"docker": unavailable}), \
             patch.object(app, "list_hosts", return_value=[]), \
             patch.object(app, "enrich_os_upgrade", side_effect=lambda value: value), \
             patch.object(app, "gpu_telemetry", return_value=None), \
             patch.object(app.socket, "gethostname", return_value="hub"):
            payload = app.fleet_payload()

        self.assertEqual(payload["hosts"][0]["containers"], [])


if __name__ == "__main__":
    unittest.main()
