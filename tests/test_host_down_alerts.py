"""Host-down alerting — a registered remote going unreachable must page, and
recovering must clear the alert and quote how long it was down.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _HostDownCase(unittest.TestCase):
    """Drives notify_host_down with a fake HOST_DATA and captures what it emits."""

    def setUp(self):
        import app
        from backend import notify
        self.app, self.notify = app, notify
        notify._HOST_DOWN_SINCE.clear()
        self.emitted = []
        self.cleared = []
        self.settings = {}

    def run_scan(self, host_data, at):
        with mock.patch.dict(self.app.HOST_DATA, host_data, clear=True), \
             mock.patch.object(self.app, "_emit",
                               side_effect=lambda s, k, lvl, t, d, rules=None: self.emitted.append((k, lvl, t, d))), \
             mock.patch.object(self.app, "_clear", side_effect=lambda k: self.cleared.append(k)), \
             mock.patch.object(self.notify.time, "time", return_value=at):
            self.notify.notify_host_down(self.settings, None)

    def keys(self):
        return [e[0] for e in self.emitted]


class TestHostGoesDown(_HostDownCase):
    def test_offline_host_fires_host_down(self):
        # window=60, last good poll 1000s ago -> well outside the window -> offline
        entry = {"data": {"host": {}}, "at": 1000, "window": 60, "error": "timeout"}
        self.run_scan({"vader": entry}, at=2000)
        self.assertIn("host:vader", self.keys())
        level = dict((k, lvl) for k, lvl, *_ in self.emitted)["host:vader"]
        self.assertEqual(level, "critical")

    def test_online_host_does_not_fire(self):
        entry = {"data": {"host": {}}, "at": 1990, "window": 60}
        self.run_scan({"vader": entry}, at=2000)
        self.assertEqual(self.keys(), [])

    def test_never_polled_host_is_treated_as_down(self):
        # No "data" key at all -> _host_is_online returns False unconditionally.
        entry = {"error": "connection refused"}
        self.run_scan({"vader": entry}, at=2000)
        self.assertIn("host:vader", self.keys())

    def test_down_detail_includes_last_error(self):
        entry = {"data": {"host": {}}, "at": 1000, "window": 60, "error": "no route to host"}
        self.run_scan({"vader": entry}, at=2000)
        detail = dict((k, d) for k, _, _, d in self.emitted)["host:vader"]
        self.assertIn("no route to host", detail)

    def test_down_key_uses_plain_host_kind_for_maintenance_matching(self):
        # _emit derives the maintenance kind via key.split(":", 1): the down
        # key must be exactly "host:<name>" (two parts) so _in_maintenance
        # gets kind="host" and name="vader" -- not some longer remainder a
        # user's maintenance-window pattern (written against the bare host
        # name) would never match.
        entry = {"data": {"host": {}}, "at": 1000, "window": 60, "error": "timeout"}
        self.run_scan({"vader": entry}, at=2000)
        key = self.keys()[0]
        kind, _, name = key.partition(":")
        self.assertEqual(kind, "host")
        self.assertEqual(name, "vader")


class TestHostRecovers(_HostDownCase):
    def test_recovery_clears_and_reports_duration(self):
        down = {"data": {"host": {}}, "at": 1000, "window": 60, "error": "timeout"}
        self.run_scan({"vader": down}, at=2000)
        self.assertIn("host:vader", self.keys())

        # Simulate it being armed (as the real _emit would have done).
        with mock.patch.dict(self.app._NOTIFIED, {"host:vader": 1}, clear=False):
            back = {"data": {"host": {}}, "at": 2100, "window": 60, "error": None}
            self.run_scan({"vader": back}, at=2100)

        self.assertIn("host:rec:vader", self.keys())
        detail = dict((k, d) for k, _, _, d in self.emitted)["host:rec:vader"]
        self.assertIn("unreachable", detail)
        self.assertIn("host:vader", self.cleared)

    def test_recovery_without_prior_down_does_not_fire_recovery_alert(self):
        online = {"data": {"host": {}}, "at": 1990, "window": 60}
        self.run_scan({"vader": online}, at=2000)
        self.assertEqual([k for k in self.keys() if "rec" in k], [])


if __name__ == "__main__":
    unittest.main()
