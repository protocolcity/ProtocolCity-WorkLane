"""wl-106: Allocation panel — filed-vs-closed per lane and per author over a
selectable window, plus a totals row reconciling with wl_counts.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from worklane.trackers.protocol import TaskStatus
from worklane.trackers.sqlite import SQLiteTracker


class AllocationViewTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wl_allocation_")
        self.root = Path(self._tmp.name)
        (self.root / "data").mkdir(parents=True, exist_ok=True)

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
        os.environ["WORKLANE_DB"] = str(self.root / "data" / "tradeos.db")
        os.environ.pop("TRADEOS_TRACKER_DB", None)
        os.environ["TRADEOS_TICKETS_SOURCE"] = "sqlite"

        self.alpha = SQLiteTracker(db_path=self.root / "data" / "tradeos.db")
        self._seed()

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

    def _seed(self) -> None:
        # Two tickets in lane:build, one filed+closed, one still open —
        # plus one unlabeled ticket, so the 'unlabeled' bucket is exercised.
        t1 = self.alpha.create_task(
            title="build one", description="x", labels=["lane:build"],
        )
        self.alpha.add_comment(t1.id, "Intake: filed by agent-a", author="agent-a")
        self.alpha.update_status(t1.id, TaskStatus.DONE)
        self.alpha.add_comment(
            t1.id,
            "Completed: shipped\nVerification: tests green\nLinks: -\nFollow-ups: none",
            author="agent-a",
        )

        t2 = self.alpha.create_task(
            title="build two", description="x", labels=["lane:build"],
        )
        self.alpha.add_comment(t2.id, "Intake: filed by agent-b", author="agent-b")

        t3 = self.alpha.create_task(title="no lane", description="x")
        self.alpha.add_comment(t3.id, "Intake: filed by agent-a", author="agent-a")

    # wl-156: the allocation PANEL is retired from /admin/overview (founder
    # ruling — it answers a workers question; successor view is oc-22 on the
    # dispatch side). The derivations below stay covered as unit tests
    # because the dispatch report will consume them through a seam.

    def test_allocation_lane_rows_filed_vs_closed(self) -> None:
        from datetime import datetime, timedelta, timezone

        from worklane.task_server import (
            _allocation_lane_rows,
            _merged_scope_tasks_for_filters,
        )

        since = datetime.now(timezone.utc) - timedelta(days=14)
        rows = _allocation_lane_rows(
            _merged_scope_tasks_for_filters("tradeos"), since)
        by_lane = {r["lane"]: r for r in rows}
        self.assertEqual(by_lane["build"]["filed"], 2)
        self.assertEqual(by_lane["build"]["closed"], 1)
        self.assertEqual(by_lane["unlabeled"]["filed"], 1)
        self.assertEqual(by_lane["unlabeled"]["closed"], 0)

    def test_allocation_author_rows_from_signed_comments(self) -> None:
        from datetime import datetime, timedelta, timezone

        from worklane.task_server import _allocation_author_rows

        since = datetime.now(timezone.utc) - timedelta(days=14)
        rows = _allocation_author_rows("tradeos", since)
        by_author = {r["author"]: r for r in rows}
        self.assertEqual(by_author["agent-a"]["filed"], 2)
        self.assertEqual(by_author["agent-a"]["closed"], 1)
        self.assertEqual(by_author["agent-b"]["filed"], 1)
        self.assertEqual(by_author["agent-b"]["closed"], 0)

    def test_overview_no_longer_serves_the_allocation_panel(self) -> None:
        r = self.client.get("/admin/overview")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("Allocation ·", r.text)
        self.assertIn("verdictStrip", r.text)

    def test_totals_reconcile_with_tp_counts(self) -> None:
        from worklane.mcp.handlers import TPHandlers

        handlers = TPHandlers(author="test")
        counts = handlers.wl_counts(product="tradeos")

        from worklane.task_server import _merged_scope_tasks_for_filters, _status_totals

        all_tasks = _merged_scope_tasks_for_filters("tradeos")
        totals = _status_totals(all_tasks)

        self.assertEqual(totals["total"], counts["total"])
        self.assertEqual(totals["counts"], counts["counts"])


if __name__ == "__main__":
    unittest.main()
