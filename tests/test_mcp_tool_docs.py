"""Guards against the MCP tool inventory drifting out of sync with the docs
(bug #8): README.md / website/mcp.md claimed "19 tools" and the mcp.md table
was missing two tools, while mcp/server.py actually registers 20.

This test never imports the `mcp` package (it may not be installed in every
test env) — it parses `mcp/server.py`'s source with the `ast` module to find
every `@mcp.tool()`-decorated function, and parses the tool names out of the
markdown tables/prose in the docs with regexes.
"""
import ast
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER_PY = os.path.join(ROOT, "mcp", "server.py")
WEBSITE_MCP_MD = os.path.join(ROOT, "website", "mcp.md")
README_MD = os.path.join(ROOT, "README.md")


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _is_tool_decorator(dec):
    """True for `@mcp.tool()` / `@mcp.tool` — not `@mcp.resource(...)` or `@_track`."""
    node = dec
    if isinstance(node, ast.Call):
        node = node.func
    # node is now an Attribute like `mcp.tool` or a Name like `_track`.
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "tool"
        and isinstance(node.value, ast.Name)
        and node.value.id == "mcp"
    )


def registered_tool_names():
    """Every function name decorated with @mcp.tool() in mcp/server.py."""
    tree = ast.parse(_read(SERVER_PY), filename=SERVER_PY)
    names = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if any(_is_tool_decorator(dec) for dec in node.decorator_list):
                names.append(node.name)
    return names


# Matches a markdown-code-spanned tool call at the start of a table cell or
# separated by " / " (e.g. "`get_events(range)` / `get_alerts(range)`"),
# capturing just the bare function name before the parens.
_TOOL_CALL_RE = re.compile(r"`(?P<name>[a-z][a-z0-9_]*)\([^`]*\)`")


def documented_tool_names(markdown_text):
    """Tool names named as `name(...)` anywhere in the given markdown text.

    Restricted to the "## Tools" section (up to the next "## " heading) so we
    don't accidentally pick up unrelated code spans (e.g. env var examples).
    """
    m = re.search(r"^## Tools\b(.*?)(?=^## )", markdown_text, re.S | re.M)
    section = m.group(1) if m else markdown_text
    return set(_TOOL_CALL_RE.findall(section))


class TestMcpToolInventoryMatchesDocs(unittest.TestCase):
    def setUp(self):
        self.real_tools = set(registered_tool_names())

    def test_server_has_expected_tool_count(self):
        # Sanity check on the parser itself — catches the parser silently
        # matching zero tools just as well as a real drift.
        self.assertGreaterEqual(len(self.real_tools), 1,
                                 "no @mcp.tool() functions found in mcp/server.py — "
                                 "the AST parser may be broken")

    def test_website_mcp_md_tool_table_matches_registered_tools(self):
        documented = documented_tool_names(_read(WEBSITE_MCP_MD))
        missing = self.real_tools - documented
        extra = documented - self.real_tools
        self.assertEqual(
            missing, set(),
            "website/mcp.md's tool table is missing documentation for: %s"
            % ", ".join(sorted(missing)),
        )
        self.assertEqual(
            extra, set(),
            "website/mcp.md's tool table documents tools that no longer exist "
            "in mcp/server.py: %s" % ", ".join(sorted(extra)),
        )

    def test_mcp_readme_tool_table_matches_registered_tools(self):
        documented = documented_tool_names(_read(os.path.join(ROOT, "mcp", "README.md")))
        missing = self.real_tools - documented
        extra = documented - self.real_tools
        self.assertEqual(
            missing, set(),
            "mcp/README.md's tool table is missing documentation for: %s"
            % ", ".join(sorted(missing)),
        )
        self.assertEqual(
            extra, set(),
            "mcp/README.md's tool table documents tools that no longer exist "
            "in mcp/server.py: %s" % ", ".join(sorted(extra)),
        )

    def test_readme_tool_count_claim_matches_reality(self):
        text = _read(README_MD)
        m = re.search(r"(\d+)\s+named tools", text)
        self.assertIsNotNone(m, "README.md no longer states a tool count as "
                                 "'<N> named tools' — update this test's regex "
                                 "to match the new wording")
        claimed = int(m.group(1))
        self.assertEqual(
            claimed, len(self.real_tools),
            "README.md claims %d named tools but mcp/server.py registers %d"
            % (claimed, len(self.real_tools)),
        )

    def test_website_mcp_md_frontmatter_count_claim_matches_reality(self):
        text = _read(WEBSITE_MCP_MD)
        m = re.search(r"(\d+)\s+tools", text)
        self.assertIsNotNone(m, "website/mcp.md's frontmatter no longer states "
                                 "a tool count as '<N> tools' — update this "
                                 "test's regex to match the new wording")
        claimed = int(m.group(1))
        self.assertEqual(
            claimed, len(self.real_tools),
            "website/mcp.md frontmatter claims %d tools but mcp/server.py "
            "registers %d" % (claimed, len(self.real_tools)),
        )


if __name__ == "__main__":
    unittest.main()
