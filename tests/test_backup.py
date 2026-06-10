"""Unit tests for database backup and restore (issue #86)."""
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app
import db_backup


def _seed_db(path):
    conn = sqlite3.connect(path)
    app._apply_schema_migrations(conn)
    conn.execute("INSERT INTO settings(key, value) VALUES('alerts_enabled', '1')")
    conn.execute(
        "INSERT INTO samples(ts,util,mem_used,mem_total,power,temp,cpu,ram_used,ram_total,load1,ctemp) "
        "VALUES(1,0,0,1,0,0,0,0,1,0,0)")
    conn.commit()
    conn.close()


class TestValidateBackup(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.good = os.path.join(self._tmpdir.name, "good.db")
        _seed_db(self.good)

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_accepts_homelab_db(self):
        ok, err = db_backup.validate_backup(self.good)
        self.assertTrue(ok, err)
        self.assertIsNone(err)

    def test_rejects_garbage(self):
        bad = os.path.join(self._tmpdir.name, "bad.db")
        with open(bad, "wb") as fh:
            fh.write(b"not a sqlite file" + b"x" * 200)
        ok, err = db_backup.validate_backup(bad)
        self.assertFalse(ok)
        self.assertIn("not a valid SQLite", err)

    def test_rejects_missing_table(self):
        bare = os.path.join(self._tmpdir.name, "bare.db")
        conn = sqlite3.connect(bare)
        conn.execute("CREATE TABLE samples(ts INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()
        ok, err = db_backup.validate_backup(bare)
        self.assertFalse(ok)
        self.assertIn("missing table", err)

    def test_read_missing_file_returns_error(self):
        ok, err = db_backup.validate_backup(os.path.join(self._tmpdir.name, "nope.db"))
        self.assertFalse(ok)


class TestVacuumInto(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.src = os.path.join(self._tmpdir.name, "src.db")
        self.dst = os.path.join(self._tmpdir.name, "dst.db")
        _seed_db(self.src)

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_snapshot_is_valid_backup(self):
        conn = sqlite3.connect(self.src)
        db_backup.vacuum_into(conn, self.dst)
        conn.close()
        ok, err = db_backup.validate_backup(self.dst)
        self.assertTrue(ok, err)
        conn = sqlite3.connect(self.dst)
        row = conn.execute("SELECT value FROM settings WHERE key='alerts_enabled'").fetchone()
        conn.close()
        self.assertEqual(row[0], "1")


class TestReopenDb(unittest.TestCase):
    def test_reopen_db_reconnects(self):
        app.DB.execute("SELECT 1").fetchone()
        app.reopen_db()
        row = app.DB.execute("SELECT 1").fetchone()
        self.assertEqual(row[0], 1)


class TestBackupApi(unittest.TestCase):
    def test_backup_download_returns_sqlite(self):
        client = app.app.test_client()
        rv = client.get("/api/backup")
        self.assertEqual(rv.status_code, 200)
        self.assertIn("attachment", rv.headers.get("Content-Disposition", ""))
        payload = rv.get_data()
        self.assertTrue(payload.startswith(db_backup.SQLITE_MAGIC))

    def test_restore_rejects_garbage(self):
        client = app.app.test_client()
        from io import BytesIO
        data = {"backup": (BytesIO(b"not sqlite"), "bad.db")}
        rv = client.post("/api/backup/restore", data=data, content_type="multipart/form-data")
        self.assertEqual(rv.status_code, 400)
        self.assertFalse(rv.get_json()["ok"])


if __name__ == "__main__":
    unittest.main()
