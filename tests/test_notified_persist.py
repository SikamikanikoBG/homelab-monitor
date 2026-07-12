"""Persist built-in notifier edge-state across restarts.

The built-in notify_scan() alerts (crashed container, full disk, VRAM pressure,
failed systemd unit, GPU OOM) are edge-triggered via the in-memory `_NOTIFIED`
dict. Before this slice that dict was volatile: a restart/redeploy emptied it, so
an already-fired-and-still-true condition would spuriously RE-FIRE a duplicate
notification on the next scan. These tests prove the state now survives a restart
(simulated by wiping the in-memory dict and calling restore_notified_state()),
that a legitimately-new condition still fires, that recovery still clears, that the
table is bounded/idempotent, and that a persistence failure never breaks dispatch.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


def _wipe():
    app.DB.execute("DELETE FROM notified_state")
    app.DB.commit()
    app._NOTIFIED.clear()


class TestNotifiedPersist(unittest.TestCase):
    def setUp(self):
        _wipe()

    def tearDown(self):
        _wipe()

    # ── helper: an _emit that records dispatches instead of sending ───────────
    def _emit_and_count(self, key, level="warning"):
        calls = []
        s = {**app.SETTING_DEFAULTS}
        with patch.object(app, "dispatch_alert",
                          side_effect=lambda *a, **k: calls.append(a) or []):
            app._emit(s, key, level, "Title", "Body")
        return len(calls)

    def test_emit_persists_to_db(self):
        n = self._emit_and_count("container:web")
        self.assertEqual(n, 1)                       # fired once
        self.assertIn("container:web", app._NOTIFIED)
        row = app.DB.execute(
            "SELECT notified_at FROM notified_state WHERE key=?",
            ("container:web",)).fetchone()
        self.assertIsNotNone(row)                    # written through to SQLite

    def test_no_refire_after_restart(self):
        # Fire → persist → simulate a restart (fresh in-memory state) → restore →
        # emit again for the STILL-TRUE condition → assert NO duplicate dispatch.
        self.assertEqual(self._emit_and_count("disk:/"), 1)
        # Simulate process restart: in-memory dict is lost, DB row remains.
        app._NOTIFIED.clear()
        self.assertNotIn("disk:/", app._NOTIFIED)
        app.restore_notified_state()
        self.assertIn("disk:/", app._NOTIFIED)       # rehydrated from SQLite
        # Next scan sees the same still-true condition → must be suppressed.
        self.assertEqual(self._emit_and_count("disk:/"), 0)

    def test_legit_fire_after_restart_still_fires(self):
        # A condition that was NOT armed before the restart (newly true, or it had
        # cleared) must still fire — don't over-suppress.
        self.assertEqual(self._emit_and_count("container:api"), 1)
        app._NOTIFIED.clear()
        app.restore_notified_state()
        # A brand-new key never seen before the restart still fires exactly once.
        self.assertEqual(self._emit_and_count("container:brand-new"), 1)

    def test_clear_removes_persisted_state_then_refires(self):
        # Fire, recover (_clear removes the DB row), then the SAME condition
        # re-tripping after a restart must fire again (edge re-armed).
        self.assertEqual(self._emit_and_count("gpu:vram_pressure"), 1)
        app._clear("gpu:vram_pressure")
        self.assertNotIn("gpu:vram_pressure", app._NOTIFIED)
        row = app.DB.execute(
            "SELECT 1 FROM notified_state WHERE key=?",
            ("gpu:vram_pressure",)).fetchone()
        self.assertIsNone(row)                       # cleared from SQLite too
        # Restart + restore: nothing armed → the re-trip fires again.
        app._NOTIFIED.clear()
        app.restore_notified_state()
        self.assertEqual(self._emit_and_count("gpu:vram_pressure"), 1)

    def test_restore_is_idempotent(self):
        self.assertEqual(self._emit_and_count("systemd:nginx"), 1)
        app._NOTIFIED.clear()
        app.restore_notified_state()
        app.restore_notified_state()                 # second call must not error/dup
        self.assertEqual(list(app._NOTIFIED), ["systemd:nginx"])

    def test_restore_prunes_over_cap(self):
        # Stuff the table past the cap; restore must prune to the newest N and never
        # let the table grow without bound.
        cap = app._NOTIFIED_STATE_CAP
        rows = [(f"container:c{i}", i) for i in range(cap + 25)]
        app.DB.executemany(
            "INSERT INTO notified_state(key,notified_at) VALUES(?,?)", rows)
        app.DB.commit()
        app._NOTIFIED.clear()
        app.restore_notified_state()
        remaining = app.DB.execute(
            "SELECT COUNT(*) FROM notified_state").fetchone()[0]
        self.assertEqual(remaining, cap)
        # The oldest (smallest notified_at) rows were the ones dropped.
        self.assertNotIn("container:c0", app._NOTIFIED)
        self.assertIn(f"container:c{cap + 24}", app._NOTIFIED)

    def test_runtime_bound_holds_without_restart(self):
        # OOM keys (`oom:SVC:TS`) are per-event and never _clear()ed. Prove the
        # table stays bounded during ONE long-lived process — i.e. WITHOUT any
        # restart/restore — by persisting many distinct transient keys through the
        # normal write-through path and asserting the count never materially
        # exceeds the cap. This exercises the occasional runtime prune.
        cap = app._NOTIFIED_STATE_CAP
        s = {**app.SETTING_DEFAULTS}
        with patch.object(app, "dispatch_alert", side_effect=lambda *a, **k: []):
            # Emit far more distinct keys than the cap, no restore in between.
            for i in range(cap * 2 + 137):
                app._emit(s, f"oom:svc:{i}", "critical", "T", "B")
        remaining = app.DB.execute(
            "SELECT COUNT(*) FROM notified_state").fetchone()[0]
        # Never grows past the cap by more than one prune interval's worth of
        # inserts (the prune runs every _NOTIFIED_PRUNE_EVERY inserts).
        self.assertLessEqual(remaining, cap + app._NOTIFIED_PRUNE_EVERY)
        # And an explicit prune call brings it exactly to the cap.
        app._prune_notified_state()
        remaining2 = app.DB.execute(
            "SELECT COUNT(*) FROM notified_state").fetchone()[0]
        self.assertEqual(remaining2, cap)

    def test_prune_failure_does_not_break_dispatch(self):
        # A prune failure (as opposed to a persist failure) must also never break
        # alert dispatch — the prune is wrapped independently.
        calls = []
        s = {**app.SETTING_DEFAULTS}
        with patch.object(app, "_prune_notified_state",
                          side_effect=RuntimeError("prune boom")):
            with patch.object(app, "dispatch_alert",
                              side_effect=lambda *a, **k: calls.append(a) or []):
                # Force a key count that lands on the prune interval.
                for i in range(app._NOTIFIED_PRUNE_EVERY):
                    app._emit(s, f"container:x{i}", "warning", "T", "B")
        # Every emit dispatched despite the prune raising on the interval hit.
        self.assertEqual(len(calls), app._NOTIFIED_PRUNE_EVERY)

    def test_persist_failure_does_not_break_dispatch(self):
        # A forced DB error inside the write-through path must NOT prevent the alert
        # from being dispatched, and must leave the in-memory arm intact.
        # sqlite3.Connection.execute is a read-only C attribute, so swap the whole
        # handle for a fake that raises on every call.
        class _BoomDB:
            def execute(self, *a, **k): raise RuntimeError("db boom")
            def commit(self): raise RuntimeError("db boom")
        calls = []
        s = {**app.SETTING_DEFAULTS}
        with patch.object(app, "DB", _BoomDB()):
            with patch.object(app, "dispatch_alert",
                              side_effect=lambda *a, **k: calls.append(a) or []):
                app._emit(s, "disk:/var", "critical", "Title", "Body")
        self.assertEqual(len(calls), 1)              # dispatched despite DB failure
        self.assertIn("disk:/var", app._NOTIFIED)    # in-memory arm still set

    def test_restore_failure_is_safe(self):
        # A forced error during restore must not raise; _NOTIFIED simply stays empty.
        class _BoomDB:
            def execute(self, *a, **k): raise RuntimeError("db boom")
            def commit(self): raise RuntimeError("db boom")
        app._NOTIFIED.clear()
        with patch.object(app, "DB", _BoomDB()):
            app.restore_notified_state()             # must not raise
        self.assertEqual(app._NOTIFIED, {})


if __name__ == "__main__":
    unittest.main()
