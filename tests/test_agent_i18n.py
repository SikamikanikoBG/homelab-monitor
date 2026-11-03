"""EC-18: every string the agent and login UI show goes through i18n.

- every agT/agTf/t('key', 'English') call in static/agent.js and static/csrf.js
  has its key in locales/en.json, with the same English;
- template names/descriptions and catalogue action titles have keys too;
- zh-CN carries every key (scripts/i18n-sync.py --check is the CI gate) and
  stays above the 90% bar that keeps it in the language switcher.
"""
import json
import os
import re
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CALL = re.compile(r"""\b(?:agT|agTf|t)\(\s*'([a-z0-9_.\-]+)'\s*,\s*'((?:[^'\\]|\\.)*)'""")


def _load(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as f:
        return f.read()


class TestAgentI18n(unittest.TestCase):
    def setUp(self):
        self.en = json.loads(_load("locales/en.json"))
        self.zh = json.loads(_load("locales/zh-CN.json"))

    def test_ui_keys_exist_with_matching_english(self):
        seen = 0
        for fn in ("static/agent.js", "static/csrf.js"):
            for key, eng in CALL.findall(_load(fn)):
                seen += 1
                self.assertIn(key, self.en, "%s: %s missing from en.json" % (fn, key))
                self.assertEqual(self.en[key], eng.replace("\\'", "'"), key)
        self.assertGreater(seen, 200)

    def test_templates_and_actions_have_keys(self):
        tdir = os.path.join(ROOT, "backend", "agent", "templates")
        for fn in os.listdir(tdir):
            if fn.endswith(".json"):
                t = json.loads(_load(os.path.join("backend", "agent", "templates", fn)))
                self.assertEqual(self.en.get("agent.tpl.%s.name" % t["id"]), t["name"])
                self.assertEqual(self.en.get("agent.tpl.%s.desc" % t["id"]), t["desc"])
        for a in json.loads(_load("backend/agent/catalog/actions.json"))["actions"]:
            self.assertEqual(self.en.get("agent.act." + a["id"]), a["title"], a["id"])

    def test_login_page_keys(self):
        for key in re.findall(r'data-i18n(?:-html|-ph)?="([a-z0-9_.]+)"', _load("static/login.html")):
            self.assertIn(key, self.en)

    def test_zh_cn_in_sync(self):
        for k in self.en:
            if not k.startswith("_"):
                self.assertIn(k, self.zh, k)
        agent = [k for k in self.en if k.startswith(("agent.", "sec_auth.", "login."))]
        translated = [k for k in agent if self.zh.get(k) != self.en[k]]
        self.assertGreater(len(translated) / len(agent), 0.9)

    def test_sync_tool_is_clean(self):
        r = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "i18n-sync.py"), "--check"],
                           capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
