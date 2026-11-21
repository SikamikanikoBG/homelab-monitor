"""#328: one read-tool registry for MCP and the agent.

- Parity: every @mcp.tool() in mcp/server.py has a registry entry with the same
  name and the same parameters (the agent may add optional ones it declares as
  agent_only_params), and nothing else. mcp/server.py is parsed with `ast`, so
  the test doesn't need the mcp SDK.
- The JSON-schema subset validator.
- The in-process transport: the agent's read calls run through the real routes
  as the internal read-only caller, behind a login too.
"""
import ast
import os
import unittest

from auth_helpers import app, clear_login, login
from backend.tools import validate, defaults
from backend.tools.read import READ_TOOLS, BY_NAME, client

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _mcp_tools():
    tree = ast.parse(open(os.path.join(ROOT, "mcp", "server.py"), encoding="utf-8").read())
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for d in node.decorator_list:
                f = d.func if isinstance(d, ast.Call) else d
                if isinstance(f, ast.Attribute) and f.attr == "tool":
                    out[node.name] = [a.arg for a in node.args.args]
    return out


class TestParity(unittest.TestCase):
    def test_same_tools_as_mcp(self):
        self.assertEqual(sorted(_mcp_tools()), sorted(BY_NAME))
        self.assertEqual(len(READ_TOOLS), 20)

    def test_same_parameters(self):
        for name, args in _mcp_tools().items():
            spec = BY_NAME[name]
            mine = [p for p in spec.params["properties"] if p not in spec.agent_only_params]
            self.assertEqual(sorted(mine), sorted(args), name)

    def test_schemas_are_closed_and_described(self):
        for t in READ_TOOLS:
            self.assertIs(t.params["additionalProperties"], False, t.name)
            self.assertTrue(t.description and len(t.description) < 200, t.name)
            self.assertEqual(t.kind, "read")
            self.assertIn(t.host_scope, ("arg", "hub", "fleet", "host_opt"))
            if t.host_scope in ("arg", "host_opt"):
                self.assertIn(t.host_arg, t.params["properties"])


class TestValidate(unittest.TestCase):
    S = {"type": "object", "additionalProperties": False, "required": ["name"],
         "properties": {"name": {"type": "string", "pattern": "^[a-z]+$", "maxLength": 5},
                        "days": {"type": "integer", "minimum": 1, "maximum": 365},
                        "mode": {"type": "string", "enum": ["a", "b"]},
                        "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 2}}}

    def test_ok(self):
        self.assertEqual(validate(self.S, {"name": "abc", "days": 7, "mode": "a", "tags": ["x"]}), [])

    def test_errors(self):
        cases = [{}, {"name": "ABC"}, {"name": "abcdefg"}, {"name": "a", "days": 0},
                 {"name": "a", "days": "7"}, {"name": "a", "days": True}, {"name": "a", "mode": "c"},
                 {"name": "a", "extra": 1}, {"name": "a", "tags": [1]}, {"name": "a", "tags": ["a", "b", "c"]},
                 {"name": "a;reboot"}, {"name": "a\nb"}]
        for c in cases:
            self.assertTrue(validate(self.S, c), c)

    def test_not_an_object(self):
        self.assertEqual(validate(self.S, "x"), ["args must be object"])

    def test_defaults(self):
        self.assertEqual(defaults({"properties": {"r": {"default": "6h"}}}, {}), {"r": "6h"})


class TestInProcessTransport(unittest.TestCase):
    def setUp(self):
        clear_login()

    def tearDown(self):
        client().set_transport(None)
        clear_login()

    def _install(self):
        from backend.agent import ports
        client().set_transport(ports.internal_get)

    def test_reads_through_real_routes(self):
        self._install()
        out = BY_NAME["list_hosts"].handler()
        self.assertEqual(out["hosts"][0]["name"], "local")

    def test_works_behind_a_login_and_stays_read_only(self):
        login(app.app.test_client())
        self._install()
        self.assertEqual(BY_NAME["list_hosts"].handler()["hosts"][0]["name"], "local")
        from backend.agent import ports
        self.assertEqual(ports.internal_get("/api/backup")[0], 403)
        self.assertEqual(ports.internal_get("/api/integration/keys")[0], 403)

    def test_remote_containers_come_from_host_data(self):
        self._install()
        with app.HOST_DATA_LOCK:
            app.HOST_DATA["lab9"] = {"data": {"host": {"docker": {"available": True,
                                                                  "containers": [{"name": "web"}]}}},
                                     "at": 0}
        try:
            out = BY_NAME["get_containers"].handler(host="lab9")
            self.assertEqual(out["containers"], [{"name": "web"}])
        finally:
            with app.HOST_DATA_LOCK:
                app.HOST_DATA.pop("lab9", None)


if __name__ == "__main__":
    unittest.main()
