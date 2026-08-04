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

        # Bust the module-level /api/scene cache so each test gets a fresh
        # build regardless of test-suite ordering (wl-243).
        import worklane.api.scene as _scene_api  # noqa: PLC0415
        _scene_api._scene_cache_ts = 0.0
        _scene_api._scene_cache_payload = None
        # wl-353 / pc-881: same for /api/dev/attention single-flight cache.
        import worklane.api.tasks as _tasks_api  # noqa: PLC0415
        _tasks_api._invalidate_attention_cache()

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
            # wl-158: the way back to the room lives in the nameplate
            self.assertIn("room-back", r.text)

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

    def test_admin_tasks_project_alias_scopes_like_product(self) -> None:
        # wl-64: the JSON list endpoint honors ``project`` as the canonical
        # scope param, resolving identically to the ``product`` back-compat
        # alias — /api/admin/tasks was the last surface still product-only,
        # so a client sending project= silently fell through to no-scope.
        by_product = self.client.get("/api/admin/tasks?product=beta").json()
        by_project = self.client.get("/api/admin/tasks?project=beta").json()
        self.assertTrue(by_product["ok"])
        self.assertTrue(by_project["ok"])
        ids_product = sorted(t["id"] for t in by_product["tasks"])
        ids_project = sorted(t["id"] for t in by_project["tasks"])
        self.assertEqual(ids_project, ids_product)
        # Scope is real, not a no-op: beta's two seeded tasks, not alpha's.
        self.assertEqual(len(ids_project), 2)
        titles = {t["title"] for t in by_project["tasks"]}
        self.assertTrue(all("beta" in x for x in titles), titles)

    def test_admin_tasks_project_scopes_counts_not_just_tasks(self) -> None:
        # wl-193: project=<slug> must scope scope_counts AND column_counts to
        # that store, not just the tasks array — the reported bug was that
        # every slug returned the identical city-wide aggregate. Seed: alpha
        # (tradeos) = 3 backlog; beta = 1 in_progress + 1 backlog.
        by_all = self.client.get("/api/admin/tasks?project=all").json()
        by_beta = self.client.get("/api/admin/tasks?project=beta").json()

        # City-wide aggregate (project=all keeps current behavior).
        self.assertEqual(by_all["scope_counts"][TaskStatus.BACKLOG], 4)
        self.assertEqual(by_all["scope_counts"][TaskStatus.IN_PROGRESS], 1)

        # Per-store scope is real, not the global aggregate.
        self.assertEqual(by_beta["scope_counts"][TaskStatus.BACKLOG], 1)
        self.assertEqual(by_beta["scope_counts"][TaskStatus.IN_PROGRESS], 1)

        # The whole point of the bug: per-store and all must differ.
        self.assertNotEqual(
            by_beta["scope_counts"][TaskStatus.BACKLOG],
            by_all["scope_counts"][TaskStatus.BACKLOG],
        )

        # column_counts scopes the same way under a status filter.
        beta_backlog = self.client.get(
            "/api/admin/tasks?project=beta&status=backlog"
        ).json()
        self.assertEqual(beta_backlog["column_counts"][TaskStatus.BACKLOG], 1)

        # city-steward roster-queue contract: project=all&status=backlog&limit=1
        # reads column_counts.backlog as the city-wide queue depth — must stay
        # global, never collapse to a single store, while per-store scoping works.
        roster_probe = self.client.get(
            "/api/admin/tasks?project=all&status=backlog&limit=1"
        ).json()
        self.assertEqual(roster_probe["column_counts"][TaskStatus.BACKLOG], 4)

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
        # wl-145 + jobs-first: work-order drawer clears founder stamps on-page
        for marker in ('id="wo"', 'id="scrim"', "openWO", "signWO",
                       "/api/admin/tasks/", "woSignBtn", "runWoAct",
                       'data-wo-act="approve"', "Sign verdict", "Release claim"):
            self.assertIn(marker, body)
        # Cabinet skim — ledgers are the nav (tray-corner dropdown retired)
        self.assertNotIn('id="trayFilter"', body)
        self.assertIn("wl_desk_tray_filter", body)
        self.assertIn("hood-scope", body)
        self.assertIn("setTrayFilter", body)
        self.assertIn("All cabinets", body)
        self.assertIn("clearSkim", body)
        self.assertIn("Show all", body)
        self.assertNotIn(">WO '+", body)
        # Title-first slips + EMBARGO stamp maps kind=embargo
        self.assertIn("Title-first slips", body)
        self.assertIn('kind==="embargo"', body)
        # City DNA sprites; stamp mast (sky/kiosk retired wl-179)
        for marker in ("spriteChip", "DNA_PALETTE", "dnaHash", "mast-stamp"):
            self.assertIn(marker, body)
        self.assertNotIn('id="sky"', body)
        # [Folder] Desk mast (sixth amendment) when city-branded
        self.assertIn("class='folder'", body)
        self.assertIn("class='fn'>Desk</span>", body)
        # wl-168: the paper line — desk-counter stations FILED→CLAIMED→
        # SIGN-OFF DUE→SIGNED with live counts; flyers via setInterval
        for marker in ('id="paperLine"', 'id="plFiled"', 'id="plClaimed"',
                       'id="plSignoff"', 'id="plSigned"', "renderPaperLine",
                       "flyPaper", "recent_transitions", "plFiledN"):
            self.assertIn(marker, body)
        # wl-186: Office deep-link film stays on Desk D0; outbox/pad stay live
        for marker in ("bootFiltersFromQuery", "STATUS_F", "syncDeskQuery",
                       "setStatusFilter", "renderStatusSkim", "statusSkimUrl",
                       "statusSkimKind", 'searchParams.set("status"',
                       'searchParams.set("cabinet"'):
            self.assertIn(marker, body)
        self.assertNotIn("Outbox paused while skimming", body)
        self.assertIn("Skim filed (backlog) on this desk", body)
        self.assertNotIn("open Board · filed (backlog)", body)
        self.assertIn('id="inTrayTitle"', body)
        self.assertIn("PL_MAX", body)
        self.assertIn("/admin/tickets/", body)

    def test_attention_attributes_non_default_store(self) -> None:
        # wl-144: founder attention items in a non-default store must surface
        # with that store's composite id. Gold is human-gate (not bare
        # in_review — 2026-07-30 scarce For You / file=decided).
        t = self.beta.create_task(title="beta review", description="x")
        self.beta.update_status(t.id, TaskStatus.IN_PROGRESS)
        self.beta.update_task(
            t.id, gate_type="human", gate_note="approve beta ship", actor="test"
        )
        j = self.client.get("/api/dev/attention").json()
        match = [
            i for i in j["items"]
            if i.get("title") == "beta review"
            and i.get("kind") == "human_gate"
        ]
        self.assertEqual(len(match), 1, j["items"])
        it = match[0]
        self.assertEqual(it["product"], "beta")
        self.assertNotEqual(str(it["id"]), str(t.id))  # composite, never bare
        self.assertTrue(str(it["id"]).endswith(f"-{t.id}"), it["id"])
        self.assertEqual(it["url"], f"/admin/desk?open={it['id']}")

    def test_bare_in_review_not_in_attention(self) -> None:
        # 2026-07-30: reserve/soft-lock must not gold For You.
        t = self.beta.create_task(title="beta reserved only", description="x")
        self.beta.update_status(t.id, TaskStatus.IN_PROGRESS)
        self.beta.update_status(t.id, TaskStatus.IN_REVIEW)
        j = self.client.get("/api/dev/attention").json()
        match = [
            i for i in j["items"]
            if i.get("title") == "beta reserved only"
        ]
        self.assertEqual(match, [], j["items"])

    def test_add_project_warns_without_neighborhood_folder(self) -> None:
        # wl-155: founding-path guardrail — slug must match a neighborhood
        # folder for the city join; warn, never refuse. WL_CITY_ROOT drives
        # the check deterministically here.
        city = self.root / "city"
        (city / "goodhood").mkdir(parents=True)
        (city / "goodhood" / "AGENTS.md").write_text("# hood\n")
        os.environ["WL_CITY_ROOT"] = str(city)
        try:
            r = self.client.post("/api/admin/products", json={"slug": "goodhood"}).json()
            self.assertTrue(r["ok"])
            self.assertIsNone(r["warning"])
            r = self.client.post("/api/admin/products", json={"slug": "ghost"}).json()
            self.assertTrue(r["ok"])  # soft guardrail: created anyway
            self.assertIn("no neighborhood folder", r["warning"])
        finally:
            os.environ.pop("WL_CITY_ROOT", None)

    def test_add_project_spaced_folder_no_false_warning(self) -> None:
        # wl-270 / pc-313: spaced dirname must slugify-match store slug —
        # "SE Local HC" → se-local-hc must NOT warn about missing folder.
        city = self.root / "city-spaced"
        hood = city / "SE Local HC"
        hood.mkdir(parents=True)
        (hood / "AGENTS.md").write_text("# SE Local HC\n")
        os.environ["WL_CITY_ROOT"] = str(city)
        try:
            r = self.client.post(
                "/api/admin/products",
                json={"slug": "se-local-hc", "display": "SE Local HC", "prefix": "selo"},
            ).json()
            self.assertTrue(r["ok"], r)
            self.assertIsNone(r.get("warning"), r)
        finally:
            os.environ.pop("WL_CITY_ROOT", None)

    def test_founder_identity_roundtrip_and_desk_prefill(self) -> None:
        # wl-148: default identity, alias PATCH roundtrip, desk injection
        j = self.client.get("/api/admin/identity").json()
        self.assertTrue(j["ok"])
        self.assertEqual(j["founder_id"], "founder")
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
        self.assertIn('"founder"', body)
        # wl-150: the desk signs for the founder — no author box, ever
        self.assertNotIn('id="woAuthor"', body)
        self.assertIn('id="woSignAs"', body)
        self.assertIn("SIGNED AS", body)
        # wl-149: the classic ticket page renders the alias the same way
        t = self.beta.create_task(title="alias render", description="x")
        self.beta.add_comment(
            t.id, "Intake: filed by founder", author="founder")
        page = self.client.get(f"/admin/tasks/beta-{t.id}").text
        self.assertIn("The Mayor", page)
        self.assertIn("(founder)", page)

    def test_api_scene_shape(self) -> None:
        j = self.client.get("/api/scene").json()
        self.assertTrue(j["ok"])
        self.assertIn("stores", j)
        self.assertIn("attention", j)
        self.assertIn("filed", j)
        # wl-168: paper-line transition window (old→new pairs the activity
        # feed does not carry — engines compute, scenes animate)
        self.assertIn("recent_transitions", j)
        self.assertIsInstance(j["recent_transitions"], list)
        slugs = {s["slug"] for s in j["stores"]}
        self.assertIn("tradeos", slugs)
        for s in j["stores"]:
            for k in ("backlog", "in_progress", "in_review", "done_total", "ready"):
                self.assertIsInstance(s[k], int, k)

    def test_api_scene_recent_transitions_old_to_new(self) -> None:
        # A claim (backlog → in_progress) must surface as a recent_transitions
        # row with both statuses so the paper line can fly the sheet.
        t = self.beta.create_task(title="paper line flyer", description="x")
        self.beta.add_comment(t.id, "Owner: grok", author="grok")
        self.beta.update_status(t.id, TaskStatus.IN_PROGRESS)
        j = self.client.get("/api/scene").json()
        match = [
            tr for tr in j["recent_transitions"]
            if tr.get("to_status") == TaskStatus.IN_PROGRESS
            and str(t.id) in str(tr.get("task_id", ""))
        ]
        self.assertTrue(match, j["recent_transitions"])
        tr = match[0]
        self.assertEqual(tr["from_status"], TaskStatus.BACKLOG)
        self.assertEqual(tr["to_status"], TaskStatus.IN_PROGRESS)
        self.assertEqual(tr["store"], "beta")
        self.assertEqual(tr["author"], "grok")
        self.assertTrue(tr["id"])
        self.assertTrue(tr["ts"])

    def test_api_scene_recent_transitions_includes_create_birth(self) -> None:
        # Intake create is event_type=created (not status_change). Without it
        # Office/Desk never animate YOU→Desk for new filings (CITY_FLOW F1/F3).
        t = self.beta.create_task(title="birth flyer", description="file me")
        self.beta.add_comment(t.id, "Intake: filed by grok", author="grok")
        j = self.client.get("/api/scene").json()
        match = [
            tr for tr in j["recent_transitions"]
            if tr.get("to_status") == TaskStatus.BACKLOG
            and not tr.get("from_status")
            and str(t.id) in str(tr.get("task_id", ""))
        ]
        self.assertTrue(match, j["recent_transitions"])
        tr = match[0]
        self.assertEqual(tr["from_status"], "")
        self.assertEqual(tr["to_status"], TaskStatus.BACKLOG)
        self.assertEqual(tr["store"], "beta")
        self.assertEqual(tr["author"], "grok")
        self.assertTrue(tr["id"])
        self.assertTrue(tr["ts"])

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
        # wl-117 design req 4: utility chrome still present. Suite theme
        # toggle is on all brands; Settings gear remains either way.
        self.assertIn('id="theme-toggle"', r.text)
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
