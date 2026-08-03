"""wl-344: store addressing guard — reject bare-id writes without project=.

Prevents default-store bleed (e.g. workspace-root MCP defaulting to tradeos
and posting wl-N close-outs onto ts-N). Covers:

- products.resolve_write_task_id pure function
- MCP write tools (comment/claim/close/label/…)
- HTTP create honoring product= alias
- HTTP write endpoints rejecting bare path ids without project=
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from worklane.mcp.handlers import TPHandlers, ToolError, dispatch_tool
from worklane.products import (
    known_prefix_slug,
    resolve_write_task_id,
    split_task_id,
)
from worklane.trackers.sqlite import SQLiteTracker


def _make_env(tmp: Path) -> None:
    (tmp / "data").mkdir(parents=True, exist_ok=True)
    os.environ["WORKLANE_RUNTIME_DIR"] = str(tmp)
    os.environ["WORKLANE_RUNTIME_DIR"] = str(tmp)
    os.environ["WORKLANE_DB"] = str(tmp / "data" / "tradeos.db")
    os.environ.pop("TRADEOS_TRACKER_DB", None)
    os.environ["TRADEOS_TICKETS_SOURCE"] = "sqlite"
    # Isolate from live city roster / products.json overlays.
    os.environ["WL_WORKFORCE_NO_CITY_ROSTER"] = "1"
    os.environ.pop("WL_WORKFORCE_ROSTER", None)
    os.environ.pop("WL_WORKFORCE_ROSTER", None)


class ResolveWriteTaskIdTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._env_before = {
            k: os.environ.get(k)
            for k in (
                "WORKLANE_RUNTIME_DIR",
                "WORKLANE_RUNTIME_DIR",
                "WORKLANE_DB",
                "TRADEOS_TRACKER_DB",
                "TRADEOS_TICKETS_SOURCE",
                "WL_WORKFORCE_NO_CITY_ROSTER",
                "WL_WORKFORCE_ROSTER",
                "WL_WORKFORCE_ROSTER",
            )
        }
        _make_env(self.root)
        # Seed stores so prefixes discover.
        SQLiteTracker(db_path=self.root / "data" / "tradeos.db").create_task(
            title="seed-t", description="d"
        )
        SQLiteTracker(db_path=self.root / "data" / "worklane.db").create_task(
            title="seed-wl", description="d"
        )

    def tearDown(self) -> None:
        for k, v in self._env_before.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    def test_known_prefix_slug(self) -> None:
        self.assertEqual(known_prefix_slug("wl-12"), "worklane")
        # tradeos live prefix is "t" in un-overlaid test env
        self.assertEqual(known_prefix_slug("t-12"), "tradeos")
        self.assertIsNone(known_prefix_slug("12"))
        self.assertIsNone(known_prefix_slug("zz-9"))

    def test_composite_ok_without_project(self) -> None:
        slug, raw = resolve_write_task_id("wl-42")
        self.assertEqual(slug, "worklane")
        self.assertEqual(raw, "42")

    def test_composite_matching_project_ok(self) -> None:
        slug, raw = resolve_write_task_id("wl-42", project="worklane")
        self.assertEqual((slug, raw), ("worklane", "42"))

    def test_composite_project_mismatch_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            resolve_write_task_id("wl-42", project="tradeos")
        self.assertIn("belongs to product", str(ctx.exception))
        self.assertIn("worklane", str(ctx.exception))

    def test_bare_without_project_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            resolve_write_task_id("328")
        msg = str(ctx.exception).lower()
        self.assertIn("project", msg)
        self.assertIn("wl-344", str(ctx.exception))

    def test_bare_with_explicit_project_ok(self) -> None:
        slug, raw = resolve_write_task_id("328", project="worklane")
        self.assertEqual((slug, raw), ("worklane", "328"))

    def test_read_split_still_defaults(self) -> None:
        # Read path keeps default-store fallback (not a write).
        slug, raw = split_task_id("328")
        self.assertEqual(raw, "328")
        self.assertEqual(slug, "tradeos")


class McpWriteAddressingTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._env_before = {
            k: os.environ.get(k)
            for k in (
                "WORKLANE_RUNTIME_DIR",
                "WORKLANE_RUNTIME_DIR",
                "WORKLANE_DB",
                "TRADEOS_TRACKER_DB",
                "TRADEOS_TICKETS_SOURCE",
                "WL_WORKFORCE_NO_CITY_ROSTER",
                "WL_WORKFORCE_ROSTER",
                "WL_WORKFORCE_ROSTER",
            )
        }
        _make_env(self.root)
        SQLiteTracker(db_path=self.root / "data" / "tradeos.db").create_task(
            title="seed", description="d"
        )
        SQLiteTracker(
            db_path=self.root / "data" / "worklane.db"
        ).create_task(title="seed", description="d")
        # Default product tradeos — the workspace-root footgun.
        self.h = TPHandlers(author="lili", default_product="tradeos")

    def tearDown(self) -> None:
        for k, v in self._env_before.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    def _seed_worklane_ticket(self) -> str:
        created = self.h.wl_create(
            title="WL target",
            description="Problem: addressing. Expected: lands on worklane.",
            product="worklane",
            labels=["worker:lili"],
        )
        return created["task"]["id"]  # wl-N

    def test_comment_bare_id_without_project_rejected(self) -> None:
        tid = self._seed_worklane_ticket()
        raw = tid.split("-", 1)[1]
        with self.assertRaises(ToolError) as ctx:
            self.h.wl_comment(raw, "hello from mis-aimed hand")
        self.assertIn("wl-344", ctx.exception.message)
        self.assertIn("project", ctx.exception.message.lower())

    def test_comment_bare_id_with_project_ok(self) -> None:
        tid = self._seed_worklane_ticket()
        raw = tid.split("-", 1)[1]
        out = self.h.wl_comment(raw, "signed comment", product="worklane")
        self.assertTrue(out["ok"])
        self.assertEqual(out["comment"]["task_id"], tid)

    def test_comment_composite_ok_without_project(self) -> None:
        tid = self._seed_worklane_ticket()
        out = self.h.wl_comment(tid, "composite path")
        self.assertTrue(out["ok"])
        self.assertEqual(out["comment"]["task_id"], tid)

    def test_comment_composite_mismatch_rejected(self) -> None:
        tid = self._seed_worklane_ticket()
        with self.assertRaises(ToolError) as ctx:
            self.h.wl_comment(tid, "nope", product="tradeos")
        self.assertIn("belongs to product", ctx.exception.message)

    def test_claim_bare_without_project_rejected(self) -> None:
        tid = self._seed_worklane_ticket()
        raw = tid.split("-", 1)[1]
        with self.assertRaises(ToolError) as ctx:
            self.h.wl_claim(raw)
        self.assertIn("wl-344", ctx.exception.message)

    def test_close_bare_without_project_rejected(self) -> None:
        tid = self._seed_worklane_ticket()
        self.h.wl_claim(tid)
        raw = tid.split("-", 1)[1]
        with self.assertRaises(ToolError) as ctx:
            self.h.wl_close(
                raw,
                completed="x",
                verification="y",
                links="z",
            )
        self.assertIn("wl-344", ctx.exception.message)

    def test_label_update_cancel_reopen_bare_rejected(self) -> None:
        tid = self._seed_worklane_ticket()
        raw = tid.split("-", 1)[1]
        for fn, kwargs in (
            (self.h.wl_label, {"add": ["area:x"]}),
            (self.h.wl_update, {"priority": 2}),
            (self.h.wl_cancel, {"reason": "dup"}),
            (self.h.wl_reserve, {}),
            (self.h.wl_release, {"reason": "scope"}),
        ):
            with self.assertRaises(ToolError) as ctx:
                fn(raw, **kwargs)  # type: ignore[operator]
            self.assertIn("wl-344", ctx.exception.message, msg=fn.__name__)

    def test_show_bare_still_defaults_for_read(self) -> None:
        # Read path: bare id + default product may still resolve (or not found).
        # Must NOT raise the write-addressing guard.
        with self.assertRaises(ToolError) as ctx:
            self.h.wl_show("999999")
        self.assertIn("not found", ctx.exception.message.lower())
        self.assertNotIn("wl-344", ctx.exception.message)

    def test_dispatch_project_alias_on_bare_write(self) -> None:
        tid = self._seed_worklane_ticket()
        raw = tid.split("-", 1)[1]
        out = dispatch_tool(
            self.h,
            "wl_comment",
            {"task_id": raw, "body": "via project alias", "project": "worklane"},
        )
        self.assertTrue(out["ok"])
        self.assertEqual(out["comment"]["task_id"], tid)

    def test_does_not_bleed_to_tradeos(self) -> None:
        """Regression: bare numeric must not write the default tradeos row."""
        # Create a tradeos ticket with a known raw id, and a worklane ticket.
        t_created = self.h.wl_create(
            title="TradeOS decoy",
            description="Problem: same number. Expected: not touched.",
            product="tradeos",
            labels=["worker:lili"],
        )
        wl_created = self.h.wl_create(
            title="WorkLane real",
            description="Problem: target. Expected: only this gets comment.",
            product="worklane",
            labels=["worker:lili"],
        )
        # Force same raw sequence if possible — usually sequential per store.
        t_raw = t_created["task"]["raw_id"]
        wl_raw = wl_created["task"]["raw_id"]
        # Attempt bare write with no project — must refuse, not hit tradeos.
        with self.assertRaises(ToolError):
            self.h.wl_comment(t_raw, "would have hit tradeos")
        # Explicit project=worklane with bare id only hits worklane.
        if t_raw == wl_raw:
            self.h.wl_comment(wl_raw, "only worklane", product="worklane")
            shown_t = self.h.wl_show(t_created["task"]["id"])
            shown_w = self.h.wl_show(wl_created["task"]["id"])
            t_bodies = [c["body"] for c in shown_t["comments"]]
            w_bodies = [c["body"] for c in shown_w["comments"]]
            self.assertNotIn("only worklane", t_bodies)
            self.assertIn("only worklane", w_bodies)


class HttpAddressingTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._env_before = {
            k: os.environ.get(k)
            for k in (
                "WORKLANE_RUNTIME_DIR",
                "WORKLANE_RUNTIME_DIR",
                "WORKLANE_DB",
                "TRADEOS_TRACKER_DB",
                "TRADEOS_TICKETS_SOURCE",
                "WL_WORKFORCE_NO_CITY_ROSTER",
                "WL_WORKFORCE_ROSTER",
                "WL_WORKFORCE_ROSTER",
            )
        }
        _make_env(self.root)
        SQLiteTracker(db_path=self.root / "data" / "tradeos.db").create_task(
            title="seed", description="d"
        )
        SQLiteTracker(
            db_path=self.root / "data" / "worklane.db"
        ).create_task(title="seed", description="d")
        # Import app after env is set so products discover the temp data dir.
        from worklane.task_server import app  # noqa: PLC0415

        self.client = TestClient(app)

    def tearDown(self) -> None:
        for k, v in self._env_before.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    def test_create_honors_product_alias(self) -> None:
        r = self.client.post(
            "/api/admin/tasks",
            json={
                "title": "Via product field",
                "author": "lili",
                "description": "Problem: product= ignored. Expected: worklane store.",
                "product": "worklane",
                "labels": ["worker:lili"],
            },
        )
        self.assertEqual(r.status_code, 200, r.text)
        tid = r.json()["task"]["id"]
        self.assertTrue(tid.startswith("wl-"), tid)

    def test_create_product_project_conflict_rejected(self) -> None:
        r = self.client.post(
            "/api/admin/tasks",
            json={
                "title": "Conflict",
                "author": "lili",
                "description": "Problem: conflict. Expected: 400.",
                "project": "worklane",
                "product": "tradeos",
                "labels": ["worker:lili"],
            },
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("conflicting", r.json()["error"].lower())

    def test_comment_bare_id_rejected(self) -> None:
        r = self.client.post(
            "/api/admin/tasks",
            json={
                "title": "Target",
                "author": "lili",
                "description": "d",
                "project": "worklane",
                "labels": ["worker:lili"],
            },
        )
        self.assertEqual(r.status_code, 200, r.text)
        tid = r.json()["task"]["id"]
        raw = tid.split("-", 1)[1]
        bad = self.client.post(
            f"/api/admin/tasks/{raw}/comments",
            json={"body": "misfile attempt", "author": "lili"},
        )
        self.assertEqual(bad.status_code, 400, bad.text)
        self.assertIn("wl-344", bad.json()["error"])

    def test_comment_bare_id_with_project_query_ok(self) -> None:
        r = self.client.post(
            "/api/admin/tasks",
            json={
                "title": "Target",
                "author": "lili",
                "description": "d",
                "project": "worklane",
                "labels": ["worker:lili"],
            },
        )
        tid = r.json()["task"]["id"]
        raw = tid.split("-", 1)[1]
        ok = self.client.post(
            f"/api/admin/tasks/{raw}/comments?project=worklane",
            json={"body": "scoped write", "author": "lili"},
        )
        self.assertEqual(ok.status_code, 200, ok.text)

    def test_comment_composite_mismatch_rejected(self) -> None:
        r = self.client.post(
            "/api/admin/tasks",
            json={
                "title": "Target",
                "author": "lili",
                "description": "d",
                "project": "worklane",
                "labels": ["worker:lili"],
            },
        )
        tid = r.json()["task"]["id"]
        bad = self.client.post(
            f"/api/admin/tasks/{tid}/comments",
            json={
                "body": "mismatch",
                "author": "lili",
                "project": "tradeos",
            },
        )
        self.assertEqual(bad.status_code, 400, bad.text)
        self.assertIn("belongs to product", bad.json()["error"])

    def test_patch_bare_id_rejected(self) -> None:
        r = self.client.post(
            "/api/admin/tasks",
            json={
                "title": "Target",
                "author": "lili",
                "description": "d",
                "project": "worklane",
                "labels": ["worker:lili"],
            },
        )
        tid = r.json()["task"]["id"]
        raw = tid.split("-", 1)[1]
        bad = self.client.patch(
            f"/api/admin/tasks/{raw}",
            json={"priority": 1, "author": "lili"},
        )
        self.assertEqual(bad.status_code, 400, bad.text)
        self.assertIn("wl-344", bad.json()["error"])


if __name__ == "__main__":
    unittest.main()
