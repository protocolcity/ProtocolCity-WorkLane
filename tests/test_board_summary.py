"""wl-28: board-summary pills (ready / in flight / stalled) + /api/dev/queue/in-flight.

Replaces the old orphan_count semantics, which flagged every in_progress
ticket as "orphaned" regardless of how fresh it was.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from worklane.trackers.protocol import TaskStatus
from worklane.trackers.sqlite import SQLiteTracker


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


class BoardSummaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wl_board_summary_")
        self.root = Path(self._tmp.name)
        (self.root / "data").mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "data" / "tradeos.db"

        self._env_before = {
            k: os.environ.get(k)
            for k in (
                "WORKLANE_RUNTIME_DIR",
                "WORKLANE_DB",
                "TRADEOS_TRACKER_DB",
                "TRADEOS_TICKETS_SOURCE",
            )
        }
        os.environ["WORKLANE_RUNTIME_DIR"] = str(self.root)
        os.environ["WORKLANE_DB"] = str(self.db_path)
        os.environ.pop("TRADEOS_TRACKER_DB", None)
        os.environ["TRADEOS_TICKETS_SOURCE"] = "sqlite"

        self.tracker = SQLiteTracker(db_path=self.db_path)

        from worklane.task_server import router

        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)

    def tearDown(self) -> None:
        for k, v in self._env_before.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    def _make(self, *, title: str, status: str, age_minutes: float = 0.0):
        t = self.tracker.create_task(title=title, description="x")
        self.tracker.update_status(t.id, status)
        if age_minutes:
            self._backdate(t.id, age_minutes)
        return t

    def _backdate(self, task_id: str, age_minutes: float) -> None:
        conn = sqlite3.connect(str(self.db_path))
        try:
            dt = datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
            conn.execute(
                "UPDATE tasks SET updated_at = ? WHERE id = ?", (_iso(dt), int(task_id))
            )
            conn.commit()
        finally:
            conn.close()

    def test_counts_ready_in_flight_stalled(self) -> None:
        self._make(title="backlog A", status=TaskStatus.BACKLOG)
        self._make(title="backlog B", status=TaskStatus.BACKLOG)
        self._make(title="fresh in-progress", status=TaskStatus.IN_PROGRESS, age_minutes=5)
        self._make(title="fresh in-review", status=TaskStatus.IN_REVIEW, age_minutes=5)
        self._make(title="stale in-progress", status=TaskStatus.IN_PROGRESS, age_minutes=200)

        r = self.client.get("/api/dev/board-summary")
        self.assertEqual(r.status_code, 200)
        j = r.json()

        self.assertEqual(j["ready_count"], 2)
        # in-flight = in_progress + in_review, regardless of staleness
        self.assertEqual(j["in_flight_count"], 3)
        # only the one aged past the 90-minute cutoff counts as stalled
        self.assertEqual(j["stalled_count"], 1)
        self.assertEqual(j["stale_minutes"], 90)
        self.assertNotIn("orphan_count", j)

    def test_in_flight_route_excludes_backlog_and_done(self) -> None:
        self._make(title="backlog", status=TaskStatus.BACKLOG)
        ip = self._make(title="live work", status=TaskStatus.IN_PROGRESS)
        rev = self._make(title="under review", status=TaskStatus.IN_REVIEW)
        self._make(title="finished", status=TaskStatus.DONE)

        r = self.client.get("/api/dev/queue/in-flight")
        self.assertEqual(r.status_code, 200)
        j = r.json()
        ids = {t["id"] for t in j["in_flight"]}
        # wl-144: merged in-flight ids are composite (store prefix included) —
        # bare ids are ambiguous across stores and mis-resolve to the default.
        self.assertEqual(ids, {f"t-{ip.id}", f"t-{rev.id}"})

    def test_orphans_route_removed(self) -> None:
        r = self.client.get("/api/dev/queue/orphans")
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
