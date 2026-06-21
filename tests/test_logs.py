"""Unit tests for the container logs endpoint guard (issue #28) plus the
structured log tail + local-LLM "Summarize errors" feature (next_ai).

No real docker or ollama is touched — both are mocked. Covers the constraints the
reviewer hammers:
  • container-name validation: unknown / injection-y names → clean 404, and NEVER
    a shell-out (subprocess is never invoked for an unresolved name).
  • subprocess is invoked with an ARGUMENT LIST (shell=False), name passed after
    `--`, so no client value is ever parsed by a shell.
  • lines cap enforced; structured JSON shape + truncated flag.
  • summarize reuses the ollama path and graceful-degrades when the LLM is down;
    the no-recent-errors state; always-200 / clean-404.
  • log lines stay verbatim (the UI escapes them); summarize sends log text ONLY
    to the local ollama path.
"""
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


FAKE_CONTAINERS = [
    {"id": "abc123def456", "name": "grafana", "image": "grafana/grafana", "ip": None, "ports": []},
    {"id": "0011223344ff", "name": "ollama", "image": "ollama/ollama", "ip": None, "ports": []},
]


def _fake_run_factory(stdout=b"", returncode=0):
    def _run(args, **kw):
        _run.last_args = args
        _run.last_kw = kw
        return MagicMock(stdout=stdout, stderr=b"", returncode=returncode)
    _run.last_args = None
    _run.last_kw = None
    return _run


class TestContainerNameGuard(unittest.TestCase):
    def test_accepts_normal_names(self):
        for n in ("ollama", "immich_server", "langfuse-stack-redis-1", "a.b_c-1"):
            self.assertTrue(app._CT_NAME_RE.match(n), n)

    def test_rejects_injection_and_paths(self):
        for n in ("../etc", "a/b", "a b", "", "-leading", "name;rm", "json?all=1"):
            self.assertIsNone(app._CT_NAME_RE.match(n), n)

    def test_endpoint_400_on_bad_name(self):
        # A leading-dash name reaches the handler but fails the guard before any
        # Docker socket access -> 400 (not 500).
        r = app.app.test_client().get("/api/containers/-bad/logs")
        self.assertEqual(r.status_code, 400)


class TestResolveContainer(unittest.TestCase):
    def test_resolves_known_name_and_id(self):
        with patch("app.containers", return_value=FAKE_CONTAINERS):
            self.assertEqual(app._resolve_container("grafana"), ("abc123def456", "grafana"))
            self.assertEqual(app._resolve_container("abc123def456"), ("abc123def456", "grafana"))
            self.assertEqual(app._resolve_container("abc123"), ("abc123def456", "grafana"))

    def test_unknown_name_rejected(self):
        with patch("app.containers", return_value=FAKE_CONTAINERS):
            self.assertEqual(app._resolve_container("does-not-exist"), (None, None))

    def test_injection_names_rejected_by_regex(self):
        for bad in ["grafana; rm -rf /", "grafana && curl evil", "$(id)", "a b",
                    "../etc/passwd", "-flag", "name|pipe", "`backtick`", ""]:
            self.assertEqual(app._resolve_container(bad), (None, None), bad)


class TestLogsTailEndpoint(unittest.TestCase):
    def setUp(self):
        self.c = app.app.test_client()

    def test_unknown_container_404_no_shellout(self):
        with patch("app.containers", return_value=FAKE_CONTAINERS), \
             patch("app.subprocess.run") as run:
            r = self.c.get("/api/logs/nope")
        self.assertEqual(r.status_code, 404)
        j = r.get_json()
        self.assertFalse(j["ok"])
        self.assertEqual(j["error"], "no_such_container")
        run.assert_not_called()

    def test_injection_name_404_no_shellout(self):
        with patch("app.containers", return_value=FAKE_CONTAINERS), \
             patch("app.subprocess.run") as run:
            r = self.c.get("/api/logs/" + "grafana;id")
        self.assertEqual(r.status_code, 404)
        run.assert_not_called()

    def test_known_container_returns_structured_lines(self):
        out = b"2026-06-21T10:00:00.000Z hello world\n2026-06-21T10:00:01.000Z second line\n"
        fake = _fake_run_factory(stdout=out)
        with patch("app.containers", return_value=FAKE_CONTAINERS), \
             patch("app.shutil.which", return_value="/usr/bin/docker"), \
             patch("app.subprocess.run", side_effect=fake):
            r = self.c.get("/api/logs/grafana?lines=50")
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertTrue(j["ok"])
        self.assertEqual(j["container"], "grafana")
        self.assertEqual(len(j["lines"]), 2)
        self.assertEqual(j["lines"][0]["text"], "hello world")
        self.assertEqual(j["lines"][0]["ts"], "2026-06-21T10:00:00.000Z")
        self.assertFalse(j["truncated"])

    def test_subprocess_invoked_with_arg_list_not_shell(self):
        fake = _fake_run_factory(stdout=b"x\n")
        with patch("app.containers", return_value=FAKE_CONTAINERS), \
             patch("app.shutil.which", return_value="/usr/bin/docker"), \
             patch("app.subprocess.run", side_effect=fake):
            self.c.get("/api/logs/grafana?lines=10")
        args, kw = fake.last_args, fake.last_kw
        self.assertIsInstance(args, list)
        self.assertEqual(args[0], "/usr/bin/docker")
        self.assertEqual(args[1], "logs")
        self.assertIn("--", args)
        self.assertEqual(args[args.index("--") + 1], "abc123def456")
        self.assertFalse(kw.get("shell", False))
        self.assertIn("timeout", kw)

    def test_lines_cap_enforced(self):
        fake = _fake_run_factory(stdout=b"x\n")
        with patch("app.containers", return_value=FAKE_CONTAINERS), \
             patch("app.shutil.which", return_value="/usr/bin/docker"), \
             patch("app.subprocess.run", side_effect=fake):
            self.c.get("/api/logs/grafana?lines=999999")
        ti = fake.last_args.index("--tail")
        self.assertEqual(int(fake.last_args[ti + 1]), app._LOG_LINES_CAP)

    def test_bad_lines_param_falls_back(self):
        fake = _fake_run_factory(stdout=b"x\n")
        with patch("app.containers", return_value=FAKE_CONTAINERS), \
             patch("app.shutil.which", return_value="/usr/bin/docker"), \
             patch("app.subprocess.run", side_effect=fake):
            r = self.c.get("/api/logs/grafana?lines=abc")
        self.assertEqual(r.status_code, 200)

    def test_truncated_flag_when_more_than_cap(self):
        n = app._LOG_LINES_CAP + 5
        out = ("\n".join("2026-01-01T00:00:00Z line %d" % i for i in range(n)) + "\n").encode()
        fake = _fake_run_factory(stdout=out)
        with patch("app.containers", return_value=FAKE_CONTAINERS), \
             patch("app.shutil.which", return_value="/usr/bin/docker"), \
             patch("app.subprocess.run", side_effect=fake):
            lines, truncated, err = app._docker_logs_tail("abc123def456", app._LOG_LINES_CAP)
        self.assertIsNone(err)
        self.assertTrue(truncated)
        self.assertEqual(len(lines), app._LOG_LINES_CAP)

    def test_docker_missing_graceful(self):
        with patch("app.containers", return_value=FAKE_CONTAINERS), \
             patch("app.shutil.which", return_value=None):
            r = self.c.get("/api/logs/grafana")
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertFalse(j["ok"])
        self.assertEqual(j["error"], "docker_missing")

    def test_docker_timeout_graceful(self):
        import subprocess as _sp
        with patch("app.containers", return_value=FAKE_CONTAINERS), \
             patch("app.shutil.which", return_value="/usr/bin/docker"), \
             patch("app.subprocess.run", side_effect=_sp.TimeoutExpired("docker", 8)):
            lines, truncated, err = app._docker_logs_tail("abc123def456", 50)
        self.assertEqual(err, "timeout")
        self.assertEqual(lines, [])

    def test_nonzero_returncode_is_unreachable_no_stderr_leak(self):
        fake = _fake_run_factory(stdout=b"", returncode=1)
        with patch("app.containers", return_value=FAKE_CONTAINERS), \
             patch("app.shutil.which", return_value="/usr/bin/docker"), \
             patch("app.subprocess.run", side_effect=fake):
            lines, truncated, err = app._docker_logs_tail("abc123def456", 50)
        self.assertEqual(err, "unreachable")
        self.assertEqual(lines, [])

    def test_log_text_kept_verbatim_for_ui_escaping(self):
        out = b'2026-01-01T00:00:00Z <script>alert(1)</script> & "tok"\n'
        fake = _fake_run_factory(stdout=out)
        with patch("app.containers", return_value=FAKE_CONTAINERS), \
             patch("app.shutil.which", return_value="/usr/bin/docker"), \
             patch("app.subprocess.run", side_effect=fake):
            lines, _t, err = app._docker_logs_tail("abc123def456", 50)
        self.assertIsNone(err)
        self.assertEqual(lines[0]["text"], '<script>alert(1)</script> & "tok"')


class TestSummarizeEndpoint(unittest.TestCase):
    def setUp(self):
        self.c = app.app.test_client()
        self._en = app.COPILOT_ENABLED

    def tearDown(self):
        app.COPILOT_ENABLED = self._en

    ERR_LOG = (b"2026-01-01T00:00:00Z INFO started ok\n"
               b"2026-01-01T00:00:01Z ERROR connection refused to db\n"
               b"2026-01-01T00:00:02Z WARN retrying in 5s\n")

    def test_unknown_container_404(self):
        with patch("app.containers", return_value=FAKE_CONTAINERS), \
             patch("app.subprocess.run") as run:
            r = self.c.post("/api/logs/nope/summarize")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.get_json()["llm_status"], "no_such_container")
        run.assert_not_called()

    def test_summary_uses_ollama_path(self):
        fake = _fake_run_factory(stdout=self.ERR_LOG)
        with patch("app.containers", return_value=FAKE_CONTAINERS), \
             patch("app.shutil.which", return_value="/usr/bin/docker"), \
             patch("app.subprocess.run", side_effect=fake), \
             patch("app._ollama_generate", return_value=("The DB is unreachable.", None)) as gen:
            r = self.c.post("/api/logs/grafana/summarize")
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertEqual(j["source"], "llm")
        self.assertEqual(j["llm_status"], "ok")
        self.assertEqual(j["summary"], "The DB is unreachable.")
        gen.assert_called_once()
        prompt = gen.call_args[0][0]
        self.assertIn("connection refused", prompt)

    def test_summary_graceful_when_llm_unreachable(self):
        fake = _fake_run_factory(stdout=self.ERR_LOG)
        with patch("app.containers", return_value=FAKE_CONTAINERS), \
             patch("app.shutil.which", return_value="/usr/bin/docker"), \
             patch("app.subprocess.run", side_effect=fake), \
             patch("app._ollama_generate", return_value=(None, "unreachable")):
            r = self.c.post("/api/logs/grafana/summarize")
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertEqual(j["source"], "none")
        self.assertEqual(j["llm_status"], "unreachable")
        self.assertEqual(j["summary"], "")

    def test_no_recent_errors_state(self):
        clean = b"2026-01-01T00:00:00Z INFO all good\n2026-01-01T00:00:01Z INFO serving\n"
        fake = _fake_run_factory(stdout=clean)
        with patch("app.containers", return_value=FAKE_CONTAINERS), \
             patch("app.shutil.which", return_value="/usr/bin/docker"), \
             patch("app.subprocess.run", side_effect=fake), \
             patch("app._ollama_generate") as gen:
            r = self.c.post("/api/logs/grafana/summarize")
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertEqual(j["llm_status"], "no_errors")
        self.assertEqual(j["error_lines"], 0)
        gen.assert_not_called()

    def test_log_read_failure_graceful(self):
        with patch("app.containers", return_value=FAKE_CONTAINERS), \
             patch("app.shutil.which", return_value=None), \
             patch("app._ollama_generate") as gen:
            r = self.c.post("/api/logs/grafana/summarize")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["llm_status"], "docker_missing")
        gen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
