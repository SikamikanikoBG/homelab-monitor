#!/usr/bin/env python3
"""Release prep — run on `next`, locally, before opening the next -> main PR.

    python scripts/release.py 0.32.0 "Some catchy release title"

Bumps app.py's VERSION, promotes the CHANGELOG's `## [Unreleased]` section
into a dated release heading (and re-opens a fresh empty Unreleased section
above it), then runs the full test suite to prove nothing broke. Does NOT
commit, push, or open the PR — review the diff yourself and do that part by
hand, this only automates the maintainer's own version-bump ritual, not a
per-PR action (see CONTRIBUTING.md).
"""
import argparse
import datetime
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_PY = ROOT / "app.py"
CHANGELOG = ROOT / "CHANGELOG.md"
REPO_URL = "https://github.com/SikamikanikoBG/homelab-monitor"

VERSION_RE = re.compile(r'^(VERSION\s*=\s*)"[\d.]+"', re.MULTILINE)
UNRELEASED_RE = re.compile(r"^## \[Unreleased\] — `next`\s*\n", re.MULTILINE)
HEADING_RE = re.compile(r"^## \[", re.MULTILINE)


def fail(msg):
    print(f"release.py: {msg}", file=sys.stderr)
    sys.exit(1)


def run(*cmd):
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)


def bump_version_text(new_version):
    """Compute app.py's new text without writing it, so a later failure (e.g.
    in promote_changelog_text) never leaves a half-bumped tree."""
    text = APP_PY.read_text()
    m = re.search(r'^VERSION\s*=\s*"([\d.]+)"', text, re.MULTILINE)
    if not m:
        fail('could not find VERSION = "..." in app.py')
    old_version = m.group(1)
    if old_version == new_version:
        fail(f"app.py is already at version {new_version}")
    text = VERSION_RE.sub(lambda mm: f'{mm.group(1)}"{new_version}"', text, count=1)
    return old_version, text


def promote_changelog_text(new_version, title):
    """Compute CHANGELOG.md's new text without writing it (see bump_version_text)."""
    text = CHANGELOG.read_text()
    um = UNRELEASED_RE.search(text)
    if not um:
        fail("could not find the '## [Unreleased] — `next`' heading in CHANGELOG.md")
    body_start = um.end()
    rest = text[body_start:]
    next_heading = HEADING_RE.search(rest)
    body_end = body_start + (next_heading.start() if next_heading else len(rest))
    carried_body = text[body_start:body_end]
    if not carried_body.strip():
        fail(
            "the '## [Unreleased] — `next`' section is already empty — nothing "
            "to promote. Add release notes there first, or if this release "
            "genuinely has none, edit CHANGELOG.md by hand."
        )

    date = datetime.date.today().isoformat()
    new_heading = f"## [{new_version}]({REPO_URL}/releases/tag/v{new_version}) — {date} · **{title}**\n"
    replacement = f"## [Unreleased] — `next`\n\n{new_heading}{carried_body}"
    return text[: um.start()] + replacement + text[body_end:]


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("version", help="new version, e.g. 0.32.0")
    parser.add_argument("title", help="one-line release title")
    args = parser.parse_args()

    if not re.fullmatch(r"\d+\.\d+\.\d+", args.version):
        fail(f"version must be X.Y.Z, got {args.version!r}")

    status = run("git", "status", "--porcelain")
    if status.returncode != 0:
        fail("`git status` failed — not a git repo? " + status.stderr)
    if status.stdout.strip():
        fail("working tree isn't clean — commit or stash first:\n" + status.stdout)

    branch = run("git", "branch", "--show-current").stdout.strip()
    if branch != "next":
        print(f"release.py: warning — running on branch {branch!r}, not next", file=sys.stderr)

    # Compute both files' new content before writing either — a failure in
    # promote_changelog_text must never leave app.py bumped with a stale
    # CHANGELOG (or vice versa).
    old_version, app_text = bump_version_text(args.version)
    changelog_text = promote_changelog_text(args.version, args.title)
    APP_PY.write_text(app_text)
    CHANGELOG.write_text(changelog_text)
    print(f"Bumped VERSION {old_version} -> {args.version}, promoted CHANGELOG heading.")

    print("Running the full test suite ...")
    test = run(sys.executable, "-m", "pytest", "tests/", "-q")
    sys.stdout.write(test.stdout)
    sys.stderr.write(test.stderr)
    if test.returncode != 0:
        fail("test suite failed after the version bump — fix before committing "
             "(app.py / CHANGELOG.md changes left in place for inspection)")

    changed = run("git", "status", "--porcelain").stdout
    changed_files = {line[3:] for line in changed.splitlines()}
    expected = {"app.py", "CHANGELOG.md"}
    if changed_files != expected:
        fail(
            "unexpected files changed besides app.py and CHANGELOG.md: "
            f"{sorted(changed_files - expected)} — investigate before committing"
        )

    print("\nAll green. Review the diff, then:")
    print("  git add app.py CHANGELOG.md")
    print(f'  git commit -m "release: v{args.version}"')
    print("  git push origin next")
    print(f"  gh pr create --base main --head next --title 'Release v{args.version}'")


if __name__ == "__main__":
    main()
