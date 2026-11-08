"""Unit tests for the remote probe's docker inventory (per-host Containers).
docker CLI is mocked — these verify parsing, the stats/size merges, the
per-container VRAM attribution via pid cgroups, the problem count, and that a
docker-less host degrades to {} rather than an error."""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import probe

CID1 = "a" * 64
CID2 = "b" * 64


def _res(stdout, rc=0):
    r = mock.Mock()
    r.returncode = rc
    r.stdout = stdout.encode()
    return r


def _ps_line(name, image, state, status, ports="", cid=CID1, running_for="9 hours ago"):
    return json.dumps({"ID": cid, "Names": name, "Image": image, "State": state,
                       "Status": status, "Ports": ports, "RunningFor": running_for})


def _dispatch(ps="", sizes="", stats=""):
    """Route the three docker calls read_docker makes to canned outputs."""
    def run(args, **kw):
        if args[1] == "stats":
            return _res(stats)
        if "-s" in args:
            return _res(sizes)
        return _res(ps)
    return run


class TestStatsMemBytes(unittest.TestCase):
    def test_units(self):
        self.assertEqual(probe._stats_mem_bytes("1.5GiB / 62GiB"), int(1.5 * 1024**3))
        self.assertEqual(probe._stats_mem_bytes("512MiB / 8GiB"), 512 * 1024**2)
        self.assertEqual(probe._stats_mem_bytes("900kB / 1GB"), 900 * 1000)
        self.assertEqual(probe._stats_mem_bytes("2.5MB (virtual 1.2GB)"), int(2.5 * 1000**2))
        self.assertIsNone(probe._stats_mem_bytes("garbage"))
        self.assertIsNone(probe._stats_mem_bytes(None))


class TestReadDocker(unittest.TestCase):
    def test_inventory_with_stats_uptime_and_size(self):
        ps = "\n".join([
            _ps_line("ollama", "ollama/ollama:latest", "running", "Up 2 days (healthy)",
                     "0.0.0.0:11434->11434/tcp", cid=CID1, running_for="2 days ago"),
            _ps_line("dead", "old:1", "exited", "Exited (137) 3 hours ago", cid=CID2),
            _ps_line("flappy", "img:2", "restarting", "Restarting (1) 5 seconds ago"),
            _ps_line("clean-exit", "img:3", "exited", "Exited (0) 2 weeks ago"),
        ])
        sizes = f"{CID1}\t2.5MB (virtual 1.2GB)\n{CID2}\t0B (virtual 500MB)\n"
        stats = json.dumps({"Name": "ollama", "MemUsage": "2GiB / 62GiB", "CPUPerc": "12.5%"})
        fake_stats_json = {"memory_stats": {"stats": {"anon": 500 * 1024**2, "shmem": 12 * 1024**2}}}
        with mock.patch("probe.subprocess.run", side_effect=_dispatch(ps, sizes, stats)), \
             mock.patch("probe._docker_stats", return_value=fake_stats_json):
            out = probe.read_docker()
        dk = out["docker"]
        self.assertTrue(dk["available"])
        self.assertEqual(dk["summary"], {"total": 4, "running": 1, "problems": 2})
        byname = {c["name"]: c for c in dk["containers"]}
        self.assertEqual(byname["ollama"]["mem_bytes"], 500 * 1024**2 + 12 * 1024**2)
        self.assertEqual(byname["ollama"]["cpu_pct"], 12.5)
        self.assertEqual(byname["ollama"]["uptime"], "2 days")   # " ago" stripped
        self.assertEqual(byname["ollama"]["disk_bytes"], int(2.5 * 1000**2))
        self.assertEqual(byname["dead"]["disk_bytes"], 0)
        self.assertEqual(byname["dead"]["uptime"], "")           # not running
        self.assertNotIn("mem_bytes", byname["dead"])

    def test_vram_attribution_via_pid_cgroup(self):
        ps = "\n".join([
            _ps_line("ollama", "ollama/ollama", "running", "Up 1 hour", cid=CID1),
            _ps_line("idlebox", "img", "running", "Up 1 hour", cid=CID2),
        ])
        gpu_procs = [{"pid": 100, "name": "ollama", "mem": 20000},
                     {"pid": 200, "name": "ollama", "mem": 3000},
                     {"pid": 300, "name": "python", "mem": 500}]   # host process
        cgroups = {100: CID1, 200: CID1, 300: None}
        with mock.patch("probe.subprocess.run", side_effect=_dispatch(ps)), \
             mock.patch("probe._pid_container_id", side_effect=lambda p: cgroups.get(p)), \
             mock.patch("probe._docker_stats", return_value=None):
            out = probe.read_docker(gpu_procs=gpu_procs)
        byname = {c["name"]: c for c in out["docker"]["containers"]}
        self.assertEqual(byname["ollama"]["vram_mb"], 23000)
        self.assertNotIn("vram_mb", byname["idlebox"])

    def test_per_card_vram_is_carried_up_to_the_container(self):
        # The pid is the only thing that knows BOTH which GPU it sits on and
        # which container it belongs to. Nothing downstream can rebuild that
        # link from a container name — the container is "ollama" while the
        # process on the card is "llama-server" — so the probe must attach it.
        ps = "\n".join([
            _ps_line("ollama", "ollama/ollama", "running", "Up 1 hour", cid=CID1),
            _ps_line("idlebox", "img", "running", "Up 1 hour", cid=CID2),
        ])
        gpu_procs = [
            {"pid": 100, "name": "llama-server", "mem": 44000,
             "by_card": {"0": 22000, "1": 22000}},
            {"pid": 200, "name": "llama-server", "mem": 19000,
             "by_card": {"2": 19000}},
            {"pid": 300, "name": "python", "mem": 500, "by_card": {"0": 500}},
        ]
        cgroups = {100: CID1, 200: CID1, 300: None}
        with mock.patch("probe.subprocess.run", side_effect=_dispatch(ps)), \
             mock.patch("probe._pid_container_id", side_effect=lambda p: cgroups.get(p)), \
             mock.patch("probe._docker_stats", return_value=None):
            out = probe.read_docker(gpu_procs=gpu_procs)
        byname = {c["name"]: c for c in out["docker"]["containers"]}
        self.assertEqual(byname["ollama"]["vram_by_card"],
                         {"0": 22000, "1": 22000, "2": 19000})
        self.assertNotIn("vram_by_card", byname["idlebox"])

    def test_no_per_card_data_leaves_the_container_key_absent(self):
        # An older driver reports no gpu_uuid, so there is no split to carry.
        ps = _ps_line("ollama", "ollama/ollama", "running", "Up 1 hour", cid=CID1)
        gpu_procs = [{"pid": 100, "name": "ollama", "mem": 20000}]
        with mock.patch("probe.subprocess.run", side_effect=_dispatch(ps)), \
             mock.patch("probe._pid_container_id", side_effect=lambda p: CID1), \
             mock.patch("probe._docker_stats", return_value=None):
            out = probe.read_docker(gpu_procs=gpu_procs)
        c = out["docker"]["containers"][0]
        self.assertEqual(c["vram_mb"], 20000)
        self.assertNotIn("vram_by_card", c)

    def test_stats_failure_degrades_to_inventory_only(self):
        ps = _ps_line("web", "nginx", "running", "Up 1 hour")
        def run(args, **kw):
            if args[1] == "stats" or "-s" in args:
                raise probe.subprocess.TimeoutExpired(args, 8)
            return _res(ps)
        with mock.patch("probe.subprocess.run", side_effect=run), \
             mock.patch("probe._docker_stats", return_value=None):
            out = probe.read_docker()
        c = out["docker"]["containers"][0]
        self.assertEqual(c["name"], "web")
        self.assertNotIn("mem_bytes", c)
        self.assertNotIn("disk_bytes", c)

    def test_no_docker_returns_empty(self):
        with mock.patch("probe.subprocess.run", side_effect=FileNotFoundError):
            self.assertEqual(probe.read_docker(), {})
        with mock.patch("probe.subprocess.run", return_value=_res("permission denied", rc=1)):
            self.assertEqual(probe.read_docker(), {})

    def test_no_containers_still_reports_available(self):
        with mock.patch("probe.subprocess.run", return_value=_res("")):
            out = probe.read_docker()
        self.assertTrue(out["docker"]["available"])
        self.assertEqual(out["docker"]["summary"]["total"], 0)


class TestStatsResidentMem(unittest.TestCase):
    """Mirrors app.py's _container_stats three-way derivation: anon+shmem
    (cgroup v2) > rss+rss_huge (cgroup v1) > usage-cache (last resort)."""

    def test_cgroup_v2_anon_plus_shmem(self):
        d = {"memory_stats": {"stats": {"anon": 500 * 1024**2, "shmem": 12 * 1024**2},
                              "usage": 999 * 1024**2}}
        self.assertEqual(probe._stats_resident_mem(d), 512 * 1024**2)

    def test_cgroup_v1_rss_plus_rss_huge(self):
        d = {"memory_stats": {"stats": {"rss": 300 * 1024**2, "rss_huge": 20 * 1024**2}}}
        self.assertEqual(probe._stats_resident_mem(d), 320 * 1024**2)

    def test_usage_minus_cache_fallback(self):
        d = {"memory_stats": {"stats": {"inactive_file": 100 * 1024**2},
                              "usage": 400 * 1024**2}}
        self.assertEqual(probe._stats_resident_mem(d), 300 * 1024**2)

    def test_no_usage_returns_none(self):
        self.assertIsNone(probe._stats_resident_mem({"memory_stats": {"stats": {}}}))
        self.assertIsNone(probe._stats_resident_mem(None))


class TestDockerStats(unittest.TestCase):
    def test_socket_failure_returns_none(self):
        with mock.patch("probe.socket.socket", side_effect=OSError):
            self.assertIsNone(probe._docker_stats(CID1))


class TestPidContainerId(unittest.TestCase):
    def test_extracts_64hex_from_cgroup(self):
        body = "0::/system.slice/docker-%s.scope\n" % CID1
        with mock.patch("builtins.open", mock.mock_open(read_data=body)):
            self.assertEqual(probe._pid_container_id(123), CID1)

    def test_host_process_and_gone_pid(self):
        with mock.patch("builtins.open", mock.mock_open(read_data="0::/user.slice\n")):
            self.assertIsNone(probe._pid_container_id(123))
        with mock.patch("builtins.open", side_effect=FileNotFoundError):
            self.assertIsNone(probe._pid_container_id(999999))


if __name__ == "__main__":
    unittest.main()
