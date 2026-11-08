# Snapshot helper: write/compare JSON endpoint responses.
# Run with UPDATE_SNAPSHOTS=1 pytest to regenerate all snapshots.
import calendar as _calendar
import json
import os
import pathlib
import time as _time
import unittest
import datetime as _dt
from unittest.mock import patch

SNAP_DIR = pathlib.Path(__file__).parent / "snapshots"
SNAP_DIR.mkdir(exist_ok=True)

FROZEN_TS = 1735689600  # 2025-01-01 00:00:00 UTC
FROZEN_DT = _dt.datetime(2025, 1, 1, 0, 0, 0)


def assert_snapshot(test_case, name: str, data: dict):
    """Compare `data` against tests/snapshots/<name>.json.

    A missing baseline FAILS the test — it is never created as a side effect,
    because an auto-created baseline is one nobody reviewed. Run
    UPDATE_SNAPSHOTS=1 pytest to create or rebaseline deliberately.
    """
    path = SNAP_DIR / f"{name}.json"
    serialized = json.dumps(data, indent=2, sort_keys=True, default=str)
    if os.environ.get("UPDATE_SNAPSHOTS"):
        path.write_text(serialized + "\n")
        return
    if not path.exists():
        test_case.fail(
            f"Snapshot missing for {name}. Run UPDATE_SNAPSHOTS=1 pytest to create it deliberately."
        )
    expected = json.loads(path.read_text())  # json.loads ignores trailing whitespace
    test_case.assertEqual(
        serialized,
        json.dumps(expected, indent=2, sort_keys=True, default=str),
        f"Snapshot mismatch for {name}. Run UPDATE_SNAPSHOTS=1 pytest to rebaseline.",
    )


class _MultiPatch:
    """Context manager that applies multiple patches simultaneously."""
    def __init__(self, *patches):
        self._patches = patches
        self._mocks = []

    def __enter__(self):
        self._mocks = [p.start() for p in self._patches]
        return self._mocks[0] if len(self._mocks) == 1 else self._mocks

    def __exit__(self, *args):
        for p in reversed(self._patches):
            p.stop()


def frozen_time():
    """Freeze time deterministically across all modules and timezones.

    Patches:
    - time.time()      → FROZEN_TS (via app.time so the mock.patch traversal
                          resolves to the stdlib time module globally)
    - time.localtime() → time.gmtime (UTC) so heatmap day/hour bucketing is
                          the same on any CI runner regardless of system timezone
    - time.mktime()    → calendar.timegm (UTC inverse of gmtime) so midnight
                          calculations are also timezone-independent
    """
    return _MultiPatch(
        patch("app.time.time", return_value=FROZEN_TS),
        patch("app.time.localtime", side_effect=_time.gmtime),
        patch("app.time.mktime", side_effect=_calendar.timegm),
    )
