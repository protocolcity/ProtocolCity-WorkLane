"""wl-107: Cycle & age panel (median/p90 cycle-time and age per lane and per
priority) + Focus cut panel (lanes ranked by open P1/P2 x staleness x
blocked-status), and the intake>drain flag on the Allocation panel's lane
table (wl-103 split 2/2).
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from worklane.trackers.protocol import TaskStatus
from worklane.trackers.sqlite import SQLiteTracker


class CycleAgeFocusPanelTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wl_cycleage_")
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
        # lane:build — one closed quickly (short cycle time), one still open
        # and old (stale, P1 -> feeds the focus cut).
        t1 = self.alpha.create_task(
            title="build one", description="x", labels=["lane:build"], priority=3,
        )
        self.alpha.add_comment(t1.id, "Intake: filed by agent-a", author="agent-a")
        self.alpha.update_status(t1.id, TaskStatus.DONE)
        self.alpha.add_comment(
            t1.id,
            "Completed: shipped\nVerification: tests green\nLinks: -\nFollow-ups: none",
            author="agent-a",
        )

        t2 = self.alpha.create_task(
            title="build two — stale P1", description="x", labels=["lane:build"], priority=1,
        )
        old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        with self.alpha._connect() as conn:
            with conn:
                conn.execute(
                    "UPDATE tasks SET created_at = ?, updated_at = ? WHERE id = ?",
                    (old, old, int(t2.id)),
                )

    # wl-156: the cycle/age and focus-cut PANELS retired with the pulse page
    # (the report's aging rack + priority-integrity widgets carry the
    # decisions; the row-builder derivations stay unit-covered below for a
    # future dispatch-report revival).

    def test_report_page_carries_aging_and_priority_widgets(self) -> None:
        r = self.client.get("/admin/overview")
        self.assertEqual(r.status_code, 200)
        self.assertIn("agingRows", r.text)
        self.assertIn("urgentList", r.text)
        self.assertNotIn("Cycle &amp; age", r.text)
        self.assertNotIn("Focus cut", r.text)

    def test_api_report_flags_the_stale_p1(self) -> None:
        j = self.client.get("/api/report").json()
        self.assertTrue(j["ok"])
        urgent = {e["id"]: e for e in j["urgent_unclaimed"]}
        stale = [e for e in urgent.values() if e["age_days"] > 9]
        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0]["priority"], 1)

    def test_percentile_helper_matches_known_values(self) -> None:
        from worklane.task_server import _percentile

        self.assertIsNone(_percentile([], 0.5))
        self.assertEqual(_percentile([5.0], 0.9), 5.0)
        vals = sorted([1.0, 2.0, 3.0, 4.0, 5.0])
        self.assertEqual(_percentile(vals, 0.5), 3.0)
        self.assertEqual(_percentile(vals, 0.0), 1.0)
        self.assertEqual(_percentile(vals, 1.0), 5.0)

    def test_focus_cut_rows_score_open_p1p2_staleness_and_blocked(self) -> None:
        from worklane.task_server import _focus_cut_rows, _merged_scope_tasks_for_filters

        now = datetime.now(timezone.utc)
        all_tasks = _merged_scope_tasks_for_filters("tradeos")
        rows = _focus_cut_rows(all_tasks, [], now=now)
        lanes = {r["lane"]: r for r in rows}
        self.assertIn("build", lanes)
        self.assertEqual(lanes["build"]["open_p1p2"], 1)
        self.assertGreater(lanes["build"]["staleness_hours"], 24 * 9)

    def test_allocation_lane_rows_show_intake_over_drain(self) -> None:
        # The panel's intake>drain flag retired with the pulse page; the
        # underlying signal (filed > closed per lane) stays unit-covered
        # for the oc-22 dispatch-report seam.
        from datetime import datetime, timedelta, timezone

        from worklane.server_helpers import _allocation_lane_rows
        from worklane.task_server import _merged_scope_tasks_for_filters

        since = datetime.now(timezone.utc) - timedelta(days=14)
        rows = _allocation_lane_rows(
            _merged_scope_tasks_for_filters("tradeos"), since)
        build = next(r for r in rows if r["lane"] == "build")
        self.assertGreater(build["filed"], build["closed"])


if __name__ == "__main__":
    unittest.main()
