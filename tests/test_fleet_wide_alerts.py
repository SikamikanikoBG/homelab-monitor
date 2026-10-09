"""Fleet-wide container/systemd/disk/VRAM alerting — bug #3
(refactor-prep/03.alerts-are-hub-only.md). A remote host's crashed
container, failed systemd unit, full disk, or VRAM pressure must page the
same as the hub's own would, using a new host-prefixed key so nothing
about the hub's existing unprefixed keys changes.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _online_entry(data, window=60):
    import time
    return {"data": data, "at": int(time.time()), "window": window}


class _FleetAlertCase(unittest.TestCase):
    """Drives one notify_remote_* function with a fake HOST_DATA and
    captures what it emits/clears."""

    def setUp(self):
        import app
        from backend import notify
        self.app, self.notify = app, notify
        self.emitted = []
        self.cleared = []
        self.settings = {}

    def run_fn(self, fn_name, host_data, *extra_args):
        fn = getattr(self.notify, fn_name)
        with mock.patch.dict(self.app.HOST_DATA, host_data, clear=True), \
             mock.patch.object(self.app, "_emit",
                               side_effect=lambda s, k, lvl, t, d, rules=None: self.emitted.append((k, lvl, t, d))), \
             mock.patch.object(self.app, "_clear", side_effect=lambda k: self.cleared.append(k)):
            fn(self.settings, None, *extra_args)

    def keys(self):
        return [e[0] for e in self.emitted]


class TestRemoteContainers(_FleetAlertCase):
    def test_crashed_container_on_remote_fires_host_prefixed_key(self):
        # docker lives under data["host"]["docker"] -- probe.py's main()
        # spreads read_docker()'s return into the top-level "host" dict, it
        # is never a sibling of "host" (same for systemd/gpu below).
        data = {"host": {"docker": {"available": True, "containers": [
            {"name": "ollama", "status": "crit", "status_text": "Exited (1)"}]}}}
        self.run_fn("notify_remote_containers", {"vader": _online_entry(data)})
        self.assertIn("container:vader:ollama", self.keys())

    def test_healthy_container_clears(self):
        data = {"host": {"docker": {"available": True, "containers": [
            {"name": "ollama", "status": "ok"}]}}}
        self.run_fn("notify_remote_containers", {"vader": _online_entry(data)})
        self.assertIn("container:vader:ollama", self.cleared)

    def test_offline_host_is_skipped_not_alerted(self):
        # Stale entry (last poll far outside its window) must not be alerted
        # on as current -- notify_host_down owns "host is unreachable".
        data = {"host": {"docker": {"available": True, "containers": [
            {"name": "ollama", "status": "crit"}]}}}
        entry = {"data": data, "at": 100, "window": 60}
        self.run_fn("notify_remote_containers", {"vader": entry})
        self.assertEqual(self.keys(), [])

    def test_never_polled_host_is_skipped(self):
        self.run_fn("notify_remote_containers", {"vader": {"error": "timeout"}})
        self.assertEqual(self.keys(), [])


class TestRemoteSystemd(_FleetAlertCase):
    def test_failed_unit_on_remote_fires_host_prefixed_key(self):
        data = {"host": {"systemd": {"available": True, "services": [
            {"name": "nginx.service", "status": "crit", "active": "failed", "sub": "failed"}]}}}
        self.run_fn("notify_remote_systemd", {"vader": _online_entry(data)})
        self.assertIn("systemd:vader:nginx.service", self.keys())

    def test_ok_unit_clears(self):
        data = {"host": {"systemd": {"available": True, "services": [
            {"name": "nginx.service", "status": "ok"}]}}}
        self.run_fn("notify_remote_systemd", {"vader": _online_entry(data)})
        self.assertIn("systemd:vader:nginx.service", self.cleared)


class TestRemoteDisks(_FleetAlertCase):
    def test_full_disk_on_remote_fires_host_prefixed_key(self):
        data = {"host": {"disks": [{"mount": "/", "pct": 96, "used": 96, "total": 100}]}}
        self.run_fn("notify_remote_disks", {"vader": _online_entry(data)}, 90)
        self.assertIn("disk:vader:/", self.keys())
        level = dict((k, lvl) for k, lvl, *_ in self.emitted)["disk:vader:/"]
        self.assertEqual(level, "critical")

    def test_disk_under_threshold_clears(self):
        data = {"host": {"disks": [{"mount": "/", "pct": 40, "used": 40, "total": 100}]}}
        self.run_fn("notify_remote_disks", {"vader": _online_entry(data)}, 90)
        self.assertIn("disk:vader:/", self.cleared)


class TestRemoteVram(_FleetAlertCase):
    def test_high_vram_on_remote_fires_suffixed_key(self):
        data = {"host": {"gpu": {"mem_total": 24576, "mem_used": 24000}}}
        self.run_fn("notify_remote_vram", {"vader": _online_entry(data)})
        self.assertIn("gpu:vram_pressure:vader", self.keys())

    def test_low_vram_usage_clears(self):
        data = {"host": {"gpu": {"mem_total": 24576, "mem_used": 1000}}}
        self.run_fn("notify_remote_vram", {"vader": _online_entry(data)})
        self.assertIn("gpu:vram_pressure:vader", self.cleared)

    def test_host_with_no_gpu_does_not_fire(self):
        data = {"host": {"gpu": {}}}
        self.run_fn("notify_remote_vram", {"vader": _online_entry(data)})
        self.assertEqual(self.keys(), [])


class TestHubKeysUnchanged(unittest.TestCase):
    """Zero-regression guard: the hub's own inline blocks in notify_scan
    must still use their exact pre-fix unprefixed key shapes."""

    def test_hub_container_key_is_unprefixed(self):
        import app
        from backend import notify
        emitted = []
        settings = {"alerts_enabled": "1", "discord_webhook_url": "http://example.invalid"}
        health = {"docker": {"available": True, "containers": [
            {"name": "ollama", "status": "crit", "status_text": "Exited (1)"}]},
            "systemd": {"available": False}}
        with mock.patch.object(app, "HEALTH", health), \
             mock.patch.object(app, "LATEST", {}), \
             mock.patch.object(app, "HOST_DATA", {}), \
             mock.patch.object(app, "get_settings", return_value=settings), \
             mock.patch.object(app, "get_notification_rules", return_value=[]), \
             mock.patch.object(app, "_emit",
                               side_effect=lambda s, k, lvl, t, d, rules=None: emitted.append(k)), \
             mock.patch.object(app, "_clear"):
            notify.notify_scan()
        self.assertIn("container:ollama", emitted)
        self.assertNotIn("container:local:ollama", emitted)


if __name__ == "__main__":
    unittest.main()
