#!/usr/bin/env bash
# Run the full test suite on a Linux box (the suite can't run on Windows: the
# backup tests die on tempfile permissions there). Ships the working tree,
# uncommitted changes included, to a scratch dir and runs pytest inside
# python:3.12-slim. Dev convenience only — CI runs the same thing.
#
#   scripts/ardi-test.sh [ssh-target] [pytest args...]
#   scripts/ardi-test.sh ardi@100.97.120.53 -q tests/test_agent_policy.py
#
# The tree goes over scp as a file, not `tar | ssh`: some Windows ssh builds
# drop the tail of a piped stdin.
set -euo pipefail
TARGET="${1:-ardi@100.97.120.53}"; shift || true
DIR="${HLM_TEST_DIR:-hlm-agents-build}"
ARGS="${*:--q -p no:cacheprovider}"
cd "$(dirname "$0")/.."
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
tar cf "$TMP/tree.tar" --exclude=.git --exclude=__pycache__ --exclude=data --exclude=node_modules .
scp -q "$TMP/tree.tar" "$TARGET:/tmp/$DIR.tar"
ssh "$TARGET" "rm -rf ~/$DIR && mkdir -p ~/$DIR && tar xf /tmp/$DIR.tar -C ~/$DIR && rm -f /tmp/$DIR.tar"
ssh "$TARGET" "cd ~/$DIR && docker run --rm -u \$(id -u):\$(id -g) -e HOME=/tmp -v \$PWD:/w -w /w python:3.12-slim \
  sh -c 'pip install -q --user -r requirements.txt pytest >/dev/null 2>&1; python -m pytest $ARGS'"
