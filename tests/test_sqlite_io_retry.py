"""wl-357: SQLiteTracker retries transient disk I/O / lock OperationalErrors.

On 2026-08-03 the live desk returned 500s from list_tasks with
``sqlite3.OperationalError: disk I/O error`` while every product store still
passed PRAGMA integrity_check. The failure is treated as transient: reopen
the connection and retry a few times before surfacing.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from worklane.trackers.sqlite import (
    SQLiteTracker,
    _is_transient_sqlite_error,
)


class TransientSqliteErrorDetectTest(unittest.TestCase):
    def test_disk_io_is_transient(self) -> None:
        self.assertTrue(
            _is_transient_sqlite_error(sqlite3.OperationalError("disk I/O error"))
        )

    def test_locked_is_transient(self) -> None:
        self.assertTrue(
            _is_transient_sqlite_error(
                sqlite3.OperationalError("database is locked")
            )
        )

    def test_schema_error_is_not_transient(self) -> None:
        self.assertFalse(
            _is_transient_sqlite_error(
                sqlite3.OperationalError("no such table: tasks")
            )
        )

    def test_non_operational_is_not_transient(self) -> None:
        self.assertFalse(_is_transient_sqlite_error(ValueError("nope")))


class SqliteIoRetryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wl_sqlite_io_")
        self.db = Path(self._tmp.name) / "t.db"
        self.tracker = SQLiteTracker(db_path=self.db, product_default="")
        self.tracker.create_task(title="seed", description="", labels=[])

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_list_tasks_retries_then_succeeds(self) -> None:
        real_connect = self.tracker._connect
        calls = {"n": 0}

        def flaky_connect():
            calls["n"] += 1
            if calls["n"] < 3:
                # Mimic open succeeding then execute failing: raise from
                # context enter path by wrapping the real connection.
                cm = real_connect()
                conn = cm.__enter__()

                class BoomConn:
                    def execute(self, *a, **k):
                        raise sqlite3.OperationalError("disk I/O error")

                    def close(self) -> None:
                        conn.close()

                class BoomCM:
                    def __enter__(self):
                        return BoomConn()

                    def __exit__(self, *exc):
                        return cm.__exit__(*exc)

                return BoomCM()
            return real_connect()

        with mock.patch.object(self.tracker, "_connect", side_effect=flaky_connect):
            with mock.patch(
                "worklane.trackers.sqlite.time.sleep", return_value=None
            ):
                tasks = self.tracker.list_tasks(limit=10)

        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].title, "seed")
        self.assertEqual(calls["n"], 3)

    def test_list_tasks_raises_after_exhausting_retries(self) -> None:
        def always_io():
            raise sqlite3.OperationalError("disk I/O error")

        with mock.patch.object(
            self.tracker,
            "_connect",
            side_effect=always_io,
        ):
            with mock.patch(
                "worklane.trackers.sqlite.time.sleep", return_value=None
            ):
                with self.assertRaises(sqlite3.OperationalError) as ctx:
                    self.tracker.list_tasks(limit=1)
        self.assertIn("disk I/O", str(ctx.exception))

    def test_non_transient_raises_immediately(self) -> None:
        sleeps: list = []

        def boom():
            raise sqlite3.OperationalError("no such table: tasks")

        with mock.patch.object(self.tracker, "_connect", side_effect=boom):
            with mock.patch(
                "worklane.trackers.sqlite.time.sleep",
                side_effect=lambda s: sleeps.append(s),
            ):
                with self.assertRaises(sqlite3.OperationalError):
                    self.tracker.count_tasks_by_status()
        self.assertEqual(sleeps, [])

    def test_connect_sets_busy_timeout(self) -> None:
        with self.tracker._connect() as conn:
            row = conn.execute("PRAGMA busy_timeout").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(int(row[0]), 10000)


if __name__ == "__main__":
    unittest.main()
