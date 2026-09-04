#!/usr/bin/env python3
"""Validate that HEAD on `main` is a properly-prepared release commit.

Checks:
  1. app.py's VERSION matches CHANGELOG.md's newest release heading.
  2. The CHANGELOG's `## [Unreleased] — \\`next\\`` section is empty.

Exits non-zero with a clear message on either failure. On success, prints
the version and (if running in GitHub Actions) writes `version=X.Y.Z` to
$GITHUB_OUTPUT.
"""
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_PY = ROOT / "app.py"
CHANGELOG = ROOT / "CHANGELOG.md"

HEADING_RE = re.compile(r"^##\s*\[([\d.]+)\]", re.MULTILINE)
UNRELEASED_RE = re.compile(r"^## \[Unreleased\] — `next`\s*\n", re.MULTILINE)
NEXT_HEADING_RE = re.compile(r"^## \[", re.MULTILINE)


def fail(msg):
    print(f"check_release_ready.py: {msg}", file=sys.stderr)
    sys.exit(1)


def main():
    app_text = APP_PY.read_text()
    m = re.search(r'^VERSION\s*=\s*"([\d.]+)"', app_text, re.MULTILINE)
    if not m:
        fail('could not find VERSION = "..." in app.py')
    app_version = m.group(1)

    changelog_text = CHANGELOG.read_text()
    hm = HEADING_RE.search(changelog_text)
    if not hm:
        fail("no versioned '## [X.Y.Z]' heading found in CHANGELOG.md")
    changelog_version = hm.group(1)

    if app_version != changelog_version:
        fail(
            f"app.py VERSION ({app_version}) does not match CHANGELOG.md's "
            f"newest heading ({changelog_version}) — this main push wasn't "
            f"prepared with scripts/release.py"
        )

    um = UNRELEASED_RE.search(changelog_text)
    if not um:
        fail("could not find the '## [Unreleased] — `next`' heading in CHANGELOG.md")
    rest = changelog_text[um.end():]
    next_heading = NEXT_HEADING_RE.search(rest)
    body = rest[: next_heading.start() if next_heading else len(rest)]
    if body.strip():
        fail(
            "the '## [Unreleased] — `next`' section still has content after "
            f"the {app_version} release — it should have been reset empty:\n{body.strip()}"
        )

    print(f"OK: app.py and CHANGELOG.md agree on v{app_version}, Unreleased is empty.")
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as f:
            f.write(f"version={app_version}\n")


if __name__ == "__main__":
    main()
