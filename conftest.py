"""Global test isolation for the shared, module-level SQLite handle (``app.DB``).

Root cause of the observed order-dependent flakes (``tests/test_metrics.py`` and
``tests/test_uptime_cert.py`` passing in isolation but failing under certain
full-suite orderings):

* Every test shares ONE module-level connection ``app.DB``.
* On the dev box the default ``DB_PATH`` (``/data/gpu.db``) resolves to a
  *persistent* on-disk file (``C:\\data\\gpu.db``), so state also leaks across
  whole test *runs*, not just between tests in one run.
* Test files clean up inconsistently — ``tests/test_uptime_slo.py`` wipes the
  ``settings`` table, ``tests/test_uptime.py`` / ``tests/test_uptime_cert.py``
  wipe only the ``uptime_*`` tables, and many files wipe nothing at all. Rows a
  test never seeded therefore survive into a later test that reads global DB
  state (``/metrics``, ``uptime_overview()``, cost/SLO projections, …), and a
  test that leaves the connection in an aborted-transaction state wedges every
  DB query that follows (the ``sqlite3.OperationalError`` cascade seen when the
  suite is run in a non-default file order).

Two autouse fixtures fix this at the root — no test bodies, no reordering, no
skips:

1. ``_isolated_db_session`` (session scope): repoints ``app.DB`` *and*
   ``app.DB_PATH`` / ``$DB_PATH`` (so ``app.reopen_db()`` and the backup/restore
   path stay inside the sandbox) at a throwaway temp database for the whole
   session. Tests never touch the real persistent ``C:\\data\\gpu.db`` — matching
   CI's ephemeral behaviour and removing cross-run leakage.

2. ``_clean_db_state`` (function scope, autouse): before every test, resets the
   DB to a clean, freshly-migrated state (all user tables emptied, any half-open
   transaction rolled back, and a wedged connection rebuilt) and clears the
   module-level caches that outlive a single test (``_uptime_due``,
   ``_LLM_LAST``). Every test starts from the same known-empty state regardless
   of what ran before it → deterministic in any ordering.

This is a test-only change; it does not touch ``app.py`` and does not alter any
runtime behaviour of the deployed service.
"""
import os
import sys
import shutil
import tempfile

import pytest

# conftest.py lives at the repo root; make ``import app`` resolve exactly like it
# does inside every test module (which each insert the repo root themselves).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app  # noqa: E402


def _reset_db():
    """Return ``app.DB`` to a clean, freshly-migrated state.

    Empties every user table (schema kept), rolls back any transaction a failing
    test left half-open, and — if the shared connection has been wedged — rebuilds
    it from scratch so the next test always gets a healthy handle.
    """
    conn = app.DB
    try:
        with app.LOCK:
            # Abort any aborted/half-open transaction a prior test left behind.
            conn.rollback()
            tables = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            ]
            for t in tables:
                conn.execute(f"DELETE FROM {t}")
            conn.commit()
    except Exception:
        # Connection wedged (closed / bad state) — rebuild a fresh migrated one.
        try:
            conn.close()
        except Exception:
            pass
        app.DB = app._open_db_connection(app.DB_PATH)
        app._apply_schema_migrations(app.DB)

    # Module-level state that survives a single test and can leak across tests.
    try:
        app._uptime_due.clear()
    except Exception:
        pass
    # Latest-inference cache read by /metrics + the AI cockpit; a leftover value
    # would make another test see a throughput sample it never produced.
    app._LLM_LAST = None


@pytest.fixture(scope="session", autouse=True)
def _isolated_db_session():
    """Sandbox the whole test session onto a throwaway temp DB (see module doc)."""
    tmpdir = tempfile.mkdtemp(prefix="homelab_test_db_")
    tmp_db = os.path.join(tmpdir, "test_gpu.db")

    orig_db = app.DB
    orig_path = app.DB_PATH
    orig_env = os.environ.get("DB_PATH")

    # Repoint the module global AND the env var so reopen_db()/restore land here.
    app.DB_PATH = tmp_db
    os.environ["DB_PATH"] = tmp_db
    try:
        orig_db.close()
    except Exception:
        pass
    app.DB = app._open_db_connection(tmp_db)
    app._apply_schema_migrations(app.DB)

    try:
        yield
    finally:
        try:
            app.DB.close()
        except Exception:
            pass
        # Restore the process to how we found it (defensive; the process exits).
        app.DB_PATH = orig_path
        if orig_env is None:
            os.environ.pop("DB_PATH", None)
        else:
            os.environ["DB_PATH"] = orig_env
        try:
            app.DB = app._open_db_connection(orig_path)
        except Exception:
            pass
        shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture(autouse=True)
def _clean_db_state():
    """Give every test a clean, isolated DB + module-cache state before it runs."""
    _reset_db()
    yield
