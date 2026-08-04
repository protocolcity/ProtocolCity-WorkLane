"""wl-354: /api/admin/tasks list must not materialize full stores for counts.

WorkForce preflight hits ``project=all&status=backlog&limit=1``. Before the
fix, every such request did ``SELECT *`` across every product store solely
to build scope_counts / column_counts — O(all tickets × description bytes)
on the single-threaded desk, which wedged under concurrent preflights.
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from worklane.trackers.protocol import TaskStatus
from worklane.trackers.sqlite import SQLiteTracker


# Synthetic multi-store city sized like production (~4k tickets) with fat
# descriptions so a regression to full-row materialization is measurable.
_N_PRIMARY = 3500
_N_SECONDARY = 500
_DESC = ("x" * 800) + "\n## Detail\n" + ("paragraph " * 40)
# Cold-process budget on a laptop; SQL GROUP BY should be well under this.
_LIST_BUDGET_S = 2.0


class AdminTasksListPerfTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wl_admin_list_perf_")
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

        self.primary = SQLiteTracker(db_path=self.root / "data" / "tradeos.db")
        self.secondary = SQLiteTracker(db_path=self.root / "data" / "beta.db")
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
        # Bulk SQL seed — create_task per row would dominate wall time.
        now = "2026-08-03T00:00:00+00:00"
        self._bulk_seed(
            self.primary,
            n=_N_PRIMARY,
            title_prefix="primary",
            done_every=10,
            now=now,
        )
        self._bulk_seed(
            self.secondary,
            n=_N_SECONDARY,
            title_prefix="beta",
            done_every=0,
            now=now,
        )
        live = self.secondary.create_task(title="beta live", description=_DESC)
        self.secondary.update_status(live.id, TaskStatus.IN_PROGRESS)

    def _bulk_seed(
        self,
        tracker: SQLiteTracker,
        *,
        n: int,
        title_prefix: str,
        done_every: int,
        now: str,
    ) -> None:
        rows = []
        for i in range(n):
            if done_every and i % done_every == 0:
                st = TaskStatus.DONE
            else:
                st = TaskStatus.BACKLOG
            rows.append(
                (
                    f"{title_prefix} {i}",
                    _DESC,
                    st,
                    1 if i % 5 == 0 else 3,
                    "[]",
                    now,
                    now,
                    None,
                )
            )
        with tracker._connect() as conn:
            with conn:
                conn.executemany(
                    """
                    INSERT INTO tasks
                        (title, description, status, priority, labels,
                         created_at, updated_at, intake)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )

    def test_count_tasks_by_status_matches_list(self) -> None:
        sql = self.primary.count_tasks_by_status()
        py: dict = {s: 0 for s in TaskStatus.ALL}
        for t in self.primary.list_tasks(limit=None):
            if t.status in py:
                py[t.status] += 1
        self.assertEqual(sql, py)

    def test_project_all_limit_1_is_bounded(self) -> None:
        # Warm once (schema/connect paths), then measure.
        warm = self.client.get(
            "/api/admin/tasks?project=all&status=backlog&limit=1"
        )
        self.assertEqual(warm.status_code, 200)
        self.assertTrue(warm.json()["ok"])

        t0 = time.perf_counter()
        r = self.client.get(
            "/api/admin/tasks?project=all&status=backlog&limit=1"
        )
        elapsed = time.perf_counter() - t0
        self.assertEqual(r.status_code, 200)
        j = r.json()
        self.assertTrue(j["ok"])
        self.assertLessEqual(len(j["tasks"]), 1)
        # City-wide backlog: 90% of primary + all secondary (minus the one
        # promoted to in_progress). 3500*0.9 = 3150 primary backlog + 500 beta.
        expected_backlog = (_N_PRIMARY * 9 // 10) + _N_SECONDARY
        self.assertEqual(j["scope_counts"][TaskStatus.BACKLOG], expected_backlog)
        self.assertEqual(j["column_counts"][TaskStatus.BACKLOG], expected_backlog)
        self.assertGreater(j["scope_total"], _N_PRIMARY)
        self.assertLess(
            elapsed,
            _LIST_BUDGET_S,
            "admin/tasks project=all&limit=1 took %.3fs (budget %.1fs) — "
            "likely re-materializing full stores for scope_counts"
            % (elapsed, _LIST_BUDGET_S),
        )

    def test_project_scope_counts_still_store_local(self) -> None:
        # Regression: SQL path must still honor project= scoping (wl-193).
        by_beta = self.client.get("/api/admin/tasks?project=beta").json()
        self.assertTrue(by_beta["ok"])
        self.assertEqual(
            by_beta["scope_counts"][TaskStatus.BACKLOG], _N_SECONDARY
        )
        self.assertEqual(
            by_beta["scope_counts"][TaskStatus.IN_PROGRESS], 1
        )


if __name__ == "__main__":
    unittest.main()
