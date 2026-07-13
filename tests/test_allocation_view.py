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

    def test_allocation_panel_renders_with_default_window(self) -> None:
        r = self.client.get("/admin/overview")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Allocation", r.text)
        self.assertIn("By lane", r.text)
        self.assertIn("By author", r.text)
        self.assertIn("build", r.text)
        self.assertIn("unlabeled", r.text)
        self.assertIn("agent-a", r.text)
        self.assertIn("agent-b", r.text)
        # Default window is 14d.
        self.assertIn("Allocation · 14d", r.text)

    def test_window_selector_accepts_7_14_30(self) -> None:
        for days in (7, 14, 30):
            r = self.client.get(f"/admin/overview?days={days}")
            self.assertEqual(r.status_code, 200, days)
            self.assertIn(f"Allocation · {days}d", r.text)

    def test_invalid_window_falls_back_to_14(self) -> None:
        r = self.client.get("/admin/overview?days=999")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Allocation · 14d", r.text)

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
