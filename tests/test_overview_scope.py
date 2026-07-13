"""wl-85: Overview landing — WL-native name, per-project scope everywhere.

Cockpit (host vocabulary) and Pulse merged and renamed Overview. The page
and its summary APIs filter to a chosen project store; legacy routes
redirect. The board-summary pills API takes the same scope.
"""

from __future__ import annotations

import os
import re
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
            # wl-89: analytics live inside the Pulse grid — one themed surface.
            self.assertIn("Breakdown", r.text)
            self.assertIn("Service health", r.text)
            self.assertIn(f'data-ops-scope="{scope_attr}"', r.text)
            # Scope tabs present on the landing (wl-91: unified vocabulary)
            self.assertIn('aria-label="Project scopes"', r.text)

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

    # ── wl-117: scope switcher stays bounded at any store count ───────────

    def test_scope_nav_no_overflow_at_current_scale(self) -> None:
        """alpha (tradeos) + beta = 2 stores today; well under the inline
        threshold, so no "More" collapse — matches the current 6-store
        steady state (wl-117 design req: no regression at today's scale)."""
        r = self.client.get("/admin/overview/all")
        self.assertEqual(r.status_code, 200)
        # The CSS rule for .ts-seg-more-wrap is always present in the page
        # <style> block; check for the actual <details> element, not the
        # class name (which would false-positive against the stylesheet).
        self.assertNotIn("<details class='ts-seg-more-wrap'", r.text)

    def test_scope_nav_collapses_beyond_inline_threshold(self) -> None:
        """20 project stores (wl-117's synthetic scale target) must not
        render 20 flat pills — the row bounds at _SCOPE_NAV_MAX_INLINE and
        the rest collapse into the "More" disclosure, reachable and titled."""
        from worklane.task_server import _SCOPE_NAV_MAX_INLINE

        for i in range(20):
            SQLiteTracker(db_path=self.root / "data" / f"synth{i:02d}.db").create_task(
                title=f"synth {i}"
            )
        r = self.client.get("/admin/overview/all")
        self.assertEqual(r.status_code, 200)
        self.assertIn("<details class='ts-seg-more-wrap'", r.text)
        self.assertIn("ts-seg-more-menu", r.text)
        # Inline pills (exact "ts-seg" / "ts-seg ts-seg--on" classes, not the
        # "-more"/"-more-item" variants) = All + first N stores, capped.
        inline_pills = re.findall(r"class='ts-seg(?: ts-seg--on)?'", r.text)
        self.assertLessEqual(len(inline_pills), _SCOPE_NAV_MAX_INLINE + 1)
        # Overflowed stores still reachable inside the menu.
        self.assertIn("synth19", r.text)
        # wl-117 design req 4: utility chrome (settings/theme) still present.
        self.assertIn("id=\"theme-toggle\"", r.text)
        self.assertIn("/admin/settings", r.text)

    def test_scope_nav_middle_truncates_long_display_names(self) -> None:
        """The internal→public arrow convention (wl-113/wl-115) produces long
        display names; the switcher must keep the public-facing tail visible
        rather than end-truncating it away (wl-117 design req 2)."""
        from worklane.task_server import _split_for_middle_truncate

        head, tail = _split_for_middle_truncate("WorkLane → WorkLane")
        self.assertEqual(head, "WorkLane")
        self.assertEqual(tail, " → WorkLane")
        # Short names pass through untouched.
        head, tail = _split_for_middle_truncate("Socials")
        self.assertEqual((head, tail), ("Socials", ""))


if __name__ == "__main__":
    unittest.main()
