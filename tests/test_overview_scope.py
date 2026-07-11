"""wl-85: Overview landing — WL-native name, per-project scope everywhere.

Cockpit (host vocabulary) and Pulse merged and renamed Overview. The page
and its summary APIs filter to a chosen project store; legacy routes
redirect. The board-summary pills API takes the same scope.
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


class OverviewScopeTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wl_overview_scope_")
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

        # Two project stores — scope filtering needs a boundary to respect.
        # Seed both up front: a store is only discovered once its DB file
        # exists on disk.
        self.alpha = SQLiteTracker(db_path=self.root / "data" / "tradeos.db")
        self.beta = SQLiteTracker(db_path=self.root / "data" / "beta.db")
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
        for i in range(3):
            self.alpha.create_task(title=f"alpha {i}", description="x")
        t = self.beta.create_task(title="beta live", description="x")
        self.beta.update_status(t.id, TaskStatus.IN_PROGRESS)
        self.beta.create_task(title="beta backlog", description="x")

    # ── Page routes ──────────────────────────────────────────────────────

    def test_overview_scopes_render(self) -> None:
        for path, scope_attr in (
            ("/admin/overview", ""),
            ("/admin/overview/all", ""),
            ("/admin/overview/beta", "beta"),
        ):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200, path)
            self.assertIn("WorkLane · Overview", r.text)
            self.assertIn(f'data-ops-scope="{scope_attr}"', r.text)
            # Scope tabs present on the landing
            self.assertIn('aria-label="Overview scopes"', r.text)

    def test_unknown_scope_404s(self) -> None:
        self.assertEqual(self.client.get("/admin/overview/nope").status_code, 404)

    def test_legacy_routes_redirect(self) -> None:
        for legacy in ("/", "/admin/cockpit", "/admin/pulse"):
            r = self.client.get(legacy, follow_redirects=False)
            self.assertEqual(r.status_code, 302, legacy)
            self.assertEqual(r.headers["location"], "/admin/overview", legacy)

    # ── Summary APIs ─────────────────────────────────────────────────────

    def test_board_summary_scope_filters_counts(self) -> None:
        j_all = self.client.get("/api/dev/board-summary").json()
        self.assertEqual(j_all["ready_count"], 4)  # 3 alpha + 1 beta backlog
        self.assertEqual(j_all["in_flight_count"], 1)

        j_beta = self.client.get("/api/dev/board-summary?scope=beta").json()
        self.assertEqual(j_beta["ready_count"], 1)
        self.assertEqual(j_beta["in_flight_count"], 1)

        j_alpha = self.client.get("/api/dev/board-summary?scope=tradeos").json()
        self.assertEqual(j_alpha["ready_count"], 3)
        self.assertEqual(j_alpha["in_flight_count"], 0)

        r = self.client.get("/api/dev/board-summary?scope=nope")
        self.assertEqual(r.status_code, 404)

    def test_overview_summary_scope_filters_counts(self) -> None:
        j_all = self.client.get("/api/admin/overview/summary").json()
        self.assertEqual(j_all["status_counts"][TaskStatus.BACKLOG], 4)
        self.assertEqual(j_all["status_counts"][TaskStatus.IN_PROGRESS], 1)

        j_beta = self.client.get("/api/admin/overview/summary?scope=beta").json()
        self.assertEqual(j_beta["status_counts"][TaskStatus.BACKLOG], 1)
        self.assertEqual(j_beta["status_counts"][TaskStatus.IN_PROGRESS], 1)

        r = self.client.get("/api/admin/overview/summary?scope=nope")
        self.assertEqual(r.status_code, 404)

    def test_old_summary_route_removed(self) -> None:
        r = self.client.get("/api/admin/cockpit/summary")
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
