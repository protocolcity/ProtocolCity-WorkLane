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
        # wl-156: the report — city-wide, paper voice; scope paths stay
        # valid (404 on typos is the next test) but render the same report.
        for path in ("/admin/overview", "/admin/overview/all", "/admin/overview/beta"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200, path)
            for marker in ("verdictStrip", "flowRows", "splitBar",
                           "agingRows", "urgentList", "pruneStamp",
                           "/api/report"):
                self.assertIn(marker, r.text, path)
            self.assertIn("setInterval", r.text)
            self.assertNotIn("requestAnimationFrame", r.text)

    def test_api_report_shape(self) -> None:
        j = self.client.get("/api/report").json()
        self.assertTrue(j["ok"])
        self.assertEqual(len(j["aging_buckets"]), 4)
        self.assertIsInstance(j["open_total"], int)
        b = j["blocker"]
        for k in ("waiting_on_you", "worker_ready", "other"):
            self.assertGreaterEqual(b[k], 0, k)
        slugs = {s["slug"] for s in j["stores"]}
        self.assertIn("beta", slugs)  # beta has open work from the seed
        for s in j["stores"]:
            self.assertIn(s["verdict"],
                          ("aging", "growing", "keeping up", "steady"))
            self.assertEqual(s["net"], s["filed"] - s["signed"])

    def test_unknown_scope_404s(self) -> None:
        self.assertEqual(self.client.get("/admin/overview/nope").status_code, 404)

    def test_legacy_routes_redirect(self) -> None:
        for legacy in ("/admin/cockpit", "/admin/pulse"):
            r = self.client.get(legacy, follow_redirects=False)
            self.assertEqual(r.status_code, 302, legacy)
            self.assertEqual(r.headers["location"], "/admin/overview", legacy)

    def test_root_lands_on_the_desk(self) -> None:
        # wl-132 cutover: the living desk scene is the room you walk into.
        r = self.client.get("/", follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers["location"], "/admin/desk")

    def test_desk_scene_is_self_contained_and_live(self) -> None:
        # The scene polls the desk's OWN facts and animates on setInterval
        # (never requestAnimationFrame — suspends in background panes).
        r = self.client.get("/admin/desk")
        self.assertEqual(r.status_code, 200)
        body = r.text
        self.assertIn("/api/scene", body)
        self.assertIn("setInterval", body)
        self.assertNotIn("requestAnimationFrame", body)
        for marker in ("decisionsStack", "staleStack", "hoodList",
                       "rubberStamp", "shippedClip"):
            self.assertIn(marker, body)
        # wl-145: the work-order drawer — tickets open on the same screen,
        # wired to the existing task + comments APIs
        for marker in ('id="wo"', 'id="scrim"', "openWO", "signWO",
                       "/api/admin/tasks/", "woSignBtn"):
            self.assertIn(marker, body)

    def test_attention_attributes_non_default_store(self) -> None:
        # wl-144: in-flight work in a non-default store must surface with its
        # own store's composite id — _merged_in_flight_tasks used to return
        # bare ids, which split_task_id attributes to the DEFAULT store,
        # mislabeling the item and linking /admin/tasks/<n> to the wrong
        # ticket entirely.
        t = self.beta.create_task(title="beta review", description="x")
        self.beta.update_status(t.id, TaskStatus.IN_PROGRESS)
        self.beta.update_status(t.id, TaskStatus.IN_REVIEW)
        j = self.client.get("/api/dev/attention").json()
        match = [i for i in j["items"]
                 if i["kind"] == "in_review" and i["title"] == "beta review"]
        self.assertEqual(len(match), 1, j["items"])
        it = match[0]
        self.assertEqual(it["product"], "beta")
        self.assertNotEqual(str(it["id"]), str(t.id))  # composite, never bare
        self.assertTrue(str(it["id"]).endswith(f"-{t.id}"), it["id"])
        self.assertEqual(it["url"], f"/admin/tasks/{it['id']}")

    def test_founder_identity_roundtrip_and_desk_prefill(self) -> None:
        # wl-148: default identity, alias PATCH roundtrip, desk injection
        j = self.client.get("/api/admin/identity").json()
        self.assertTrue(j["ok"])
        self.assertEqual(j["founder_id"], "founder-terminal")
        self.assertEqual(j["founder_alias"], "")
        r = self.client.patch("/api/admin/identity",
                              json={"founder_alias": "The Mayor"})
        self.assertTrue(r.json()["ok"])
        j2 = self.client.get("/api/admin/identity").json()
        self.assertEqual(j2["founder_alias"], "The Mayor")
        # invalid founder_id is refused (ids are identity, kebab-case only)
        r = self.client.patch("/api/admin/identity",
                              json={"founder_id": "Not A Slug!"})
        self.assertEqual(r.status_code, 400)
        # the desk page carries the identity for signing + alias rendering
        body = self.client.get("/admin/desk").text
        self.assertIn("var FOUNDER=", body)
        self.assertIn("The Mayor", body)
        self.assertIn("founder-terminal", body)
        # wl-150: the desk signs for the founder — no author box, ever
        self.assertNotIn('id="woAuthor"', body)
        self.assertIn('id="woSignAs"', body)
        self.assertIn("SIGNED AS", body)

    def test_api_scene_shape(self) -> None:
        j = self.client.get("/api/scene").json()
        self.assertTrue(j["ok"])
        self.assertIn("stores", j)
        self.assertIn("attention", j)
        self.assertIn("filed", j)
        slugs = {s["slug"] for s in j["stores"]}
        self.assertIn("tradeos", slugs)
        for s in j["stores"]:
            for k in ("backlog", "in_progress", "in_review", "done_total", "ready"):
                self.assertIsInstance(s[k], int, k)

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
        # wl-156: the scope nav's home is the Board (the report is city-wide).
        r = self.client.get("/admin/tickets/all")
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
