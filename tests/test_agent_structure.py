"""Structural rules for the agent packages, checked on the source with `ast`:

- only backend/agent/ports.py imports `app` (the seam the refactor re-points);
  backend/tools/ never does;
- only the executor runs commands on hosts — the single exec path — apart from
  the fixed `sudo -n true` pre-check;
- nothing in the agent builds a shell (`sh -c`, `bash -c`) or calls subprocess.
"""
import ast
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXEC_FUNCS = {"run_on_host", "run_on_host_windows", "docker_req", "systemd_unit_action"}


def _py_files(sub):
    base = os.path.join(ROOT, *sub.split("/"))
    for d, _dirs, files in os.walk(base):
        for f in files:
            if f.endswith(".py"):
                yield os.path.join(d, f)


def _tree(path):
    with open(path, encoding="utf-8") as f:
        return ast.parse(f.read(), filename=path)


class TestStructure(unittest.TestCase):
    def test_only_ports_imports_app(self):
        for sub in ("backend/agent", "backend/tools"):
            for path in _py_files(sub):
                for node in ast.walk(_tree(path)):
                    names = []
                    if isinstance(node, ast.Import):
                        names = [a.name for a in node.names]
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        names = [node.module]
                    if "app" in names:
                        self.assertTrue(path.endswith(os.path.join("agent", "ports.py")), path)

    def test_single_exec_path(self):
        for path in _py_files("backend/agent"):
            rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
            for node in ast.walk(_tree(path)):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                        and node.func.attr in EXEC_FUNCS and getattr(node.func.value, "id", "") == "ports":
                    if rel == "backend/agent/executor.py":
                        continue
                    self.assertEqual(rel, "backend/agent/catalog/checks.py", rel)
                    self.assertEqual(ast.literal_eval(node.args[1]), "sudo -n true")

    def test_no_shell_no_subprocess(self):
        for path in list(_py_files("backend/agent")) + list(_py_files("backend/tools")):
            src = open(path, encoding="utf-8").read()
            tree = ast.parse(src)
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                    self.assertNotIn("subprocess", mods, path)
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    self.assertNotIn("sh -c", node.value, path)
                    self.assertNotIn("bash -c", node.value, path)


if __name__ == "__main__":
    unittest.main()
