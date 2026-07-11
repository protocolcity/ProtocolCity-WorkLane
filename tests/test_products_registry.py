"""Product registry + multi-store surface tests (one product = one DB).

Covers:
- discovery of ``<slug>.db`` stores in the runtime data dir,
- composite task-id namespacing (``t-`` / ``wl-`` / unknown fallback),
- the Pool surface routing (tab per product, 404 for unknown slugs),
- PROTOCOL.md enforcement on the comments API (§3.8 signed comments,
  §5 close-out contract).
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

import worklane.products as products
from worklane.trackers.sqlite import SQLiteTracker


def _make_env(tmp: Path) -> None:
    """Point the registry + default tracker at an isolated runtime dir.

    Sets ``WL_DEFAULT_PRODUCT`` explicitly — the registry no longer
    hardcodes a default product slug (wl-43), so tests configure the
    tradeos host profile the same way a real host would.
    """
    (tmp / "data").mkdir(parents=True, exist_ok=True)
    os.environ["WORKLANE_RUNTIME_DIR"] = str(tmp)
    os.environ["WORKLANE_DB"] = str(tmp / "data" / "tradeos.db")
    os.environ.pop("TRADEOS_TRACKER_DB", None)
    os.environ["TRADEOS_TICKETS_SOURCE"] = "sqlite"
    os.environ["WL_DEFAULT_PRODUCT"] = "tradeos"
    os.environ.pop("WL_PRODUCT", None)


class ProductRegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._env_before = {
            k: os.environ.get(k)
            for k in (
                "WORKLANE_RUNTIME_DIR",
                "WORKLANE_DB",
                "TRADEOS_TRACKER_DB",
                "TRADEOS_TICKETS_SOURCE",
                "WL_DEFAULT_PRODUCT",
                "WL_PRODUCT",
            )
        }
        _make_env(self.root)

    def tearDown(self) -> None:
        for k, v in self._env_before.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    def _seed(self, slug: str, title: str) -> None:
        tr = SQLiteTracker(db_path=self.root / "data" / f"{slug}.db")
        tr.create_task(title=title)

    # ── discovery ────────────────────────────────────────────────────

    def test_tradeos_always_present(self) -> None:
        slugs = [s.slug for s in products.discover_products()]
        self.assertEqual(slugs, ["tradeos"])

    def test_new_db_file_becomes_product(self) -> None:
        self._seed("worklane", "WL ticket")
        slugs = [s.slug for s in products.discover_products()]
        self.assertEqual(slugs, ["tradeos", "worklane"])
        spec = products.get_product("worklane")
        assert spec is not None
        self.assertEqual(spec.display, "WorkLane")
        self.assertEqual(spec.prefix, "wl")

    def test_unknown_slug_gets_default_meta(self) -> None:
        self._seed("myapp", "hello")
        spec = products.get_product("myapp")
        assert spec is not None
        self.assertEqual(spec.display, "Myapp")
        self.assertEqual(spec.prefix, "myapp")

    def test_legacy_ops_store_is_ignored(self) -> None:
        self._seed("ops_tickets", "legacy")
        self.assertIsNone(products.get_product("ops_tickets"))

    # ── composite ids ────────────────────────────────────────────────

    # ── config overlay ───────────────────────────────────────────────

    def test_products_json_overrides_display_and_prefix(self) -> None:
        self._seed("myapp", "hello")
        cfg = self.root / "config" / "products.json"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text('{"myapp": {"display": "My App", "prefix": "ma"}}')
        spec = products.get_product("myapp")
        assert spec is not None
        self.assertEqual(spec.display, "My App")
        self.assertEqual(spec.prefix, "ma")
        self.assertEqual(products.split_task_id("ma-7"), ("myapp", "7"))

    def test_prefix_collision_falls_back_to_slug(self) -> None:
        self._seed("myapp", "hello")
        cfg = self.root / "config" / "products.json"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text('{"myapp": {"prefix": "t"}}')  # clashes with tradeos
        spec = products.get_product("myapp")
        assert spec is not None
        self.assertEqual(spec.prefix, "myapp")

    def test_malformed_overlay_is_ignored(self) -> None:
        self._seed("myapp", "hello")
        cfg = self.root / "config" / "products.json"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text("{not json")
        spec = products.get_product("myapp")
        assert spec is not None
        self.assertEqual(spec.prefix, "myapp")

    def test_register_product_meta_persists(self) -> None:
        self._seed("myapp", "hello")
        products.register_product_meta("myapp", display="My App", prefix="ma")
        spec = products.get_product("myapp")
        assert spec is not None
        self.assertEqual((spec.display, spec.prefix), ("My App", "ma"))

    def test_register_product_meta_preserves_default_key(self) -> None:
        os.environ.pop("WL_DEFAULT_PRODUCT", None)
        os.environ.pop("WL_PRODUCT", None)
        self._seed("myapp", "hello")
        cfg = self.root / "config" / "products.json"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text('{"default": "worklane"}')
        products.register_product_meta("myapp", display="My App")
        self.assertEqual(products.default_product_slug(), "worklane")

    def test_split_task_id(self) -> None:
        self._seed("worklane", "WL ticket")
        self.assertEqual(products.split_task_id("t-12"), ("tradeos", "12"))
        self.assertEqual(
            products.split_task_id("wl-3"), ("worklane", "3")
        )
        self.assertEqual(products.split_task_id("12"), ("tradeos", "12"))
        self.assertEqual(products.split_task_id("o-9"), ("ops", "9"))
        # unknown prefix falls back to tradeos, id untouched
        self.assertEqual(products.split_task_id("zz-9"), ("tradeos", "zz-9"))

    # ── default product resolution (wl-43) ──────────────────────────

    def test_default_product_slug_env_wins_over_config(self) -> None:
        cfg = self.root / "config" / "products.json"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text('{"default": "worklane"}')
        os.environ["WL_DEFAULT_PRODUCT"] = "myapp"
        self.assertEqual(products.default_product_slug(), "myapp")

    def test_default_product_slug_falls_back_to_config_overlay(self) -> None:
        os.environ.pop("WL_DEFAULT_PRODUCT", None)
        os.environ.pop("WL_PRODUCT", None)
        cfg = self.root / "config" / "products.json"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text('{"default": "worklane"}')
        self.assertEqual(products.default_product_slug(), "worklane")
        # and it actually drives discovery/ordering, not just the getter
        self._seed("worklane", "WL ticket")
        slugs = [s.slug for s in products.discover_products()]
        self.assertEqual(slugs[0], "worklane")

    def test_default_product_slug_falls_back_to_first_discovered(self) -> None:
        os.environ.pop("WL_DEFAULT_PRODUCT", None)
        os.environ.pop("WL_PRODUCT", None)
        self._seed("myapp", "hello")
        self.assertEqual(products.default_product_slug(), "myapp")

    def test_default_product_slug_empty_on_fresh_install(self) -> None:
        os.environ.pop("WL_DEFAULT_PRODUCT", None)
        os.environ.pop("WL_PRODUCT", None)
        self.assertEqual(products.default_product_slug(), "")
        self.assertEqual(products.discover_products(), [])
        self.assertEqual(products.split_task_id("12"), ("", "12"))

    # ── product_tracker routing (wl-52) ─────────────────────────────

    def test_product_tracker_binds_own_file_even_when_configured_default(
        self,
    ) -> None:
        """A non-tradeos product configured as the process default (this
        repo's own .mcp.json sets WL_PRODUCT=worklane for the MCP
        subprocess) must still bind its own db file, not silently collide
        with get_default_tracker()'s tradeos.db."""
        self._seed("worklane", "WL ticket")
        os.environ.pop("WL_DEFAULT_PRODUCT", None)
        os.environ["WL_PRODUCT"] = "worklane"
        self.assertEqual(products.default_product_slug(), "worklane")
        tracker = products.product_tracker("worklane")
        self.assertEqual(
            tracker._db_path.resolve(),
            (self.root / "data" / "worklane.db").resolve(),
        )

    def test_product_tracker_tradeos_goes_through_default_tracker(self) -> None:
        tracker = products.product_tracker("tradeos")
        self.assertEqual(
            tracker._db_path.resolve(),
            (self.root / "data" / "tradeos.db").resolve(),
        )


class SurfaceRoutingTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._env_before = {
            k: os.environ.get(k)
            for k in (
                "WORKLANE_RUNTIME_DIR",
                "WORKLANE_DB",
                "TRADEOS_TRACKER_DB",
                "TRADEOS_TICKETS_SOURCE",
                "WL_DEFAULT_PRODUCT",
                "WL_PRODUCT",
            )
        }
        _make_env(self.root)
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

    def test_create_routes_to_product_store(self) -> None:
        r = self.client.post(
            "/api/admin/tasks",
            json={"title": "WL self-ticket", "author": "work-pool", "description": "test intake body", "surface": "worklane"},
        )
        # store doesn't exist yet → unknown surface
        self.assertEqual(r.status_code, 400)

        # create the store, then it's a first-class surface
        SQLiteTracker(
            db_path=self.root / "data" / "worklane.db"
        ).create_task(title="seed")
        r = self.client.post(
            "/api/admin/tasks",
            json={"title": "WL self-ticket", "author": "work-pool", "description": "test intake body", "surface": "worklane"},
        )
        self.assertEqual(r.status_code, 200)
        tid = r.json()["task"]["id"]
        self.assertTrue(tid.startswith("wl-"))

        got = self.client.get(f"/api/admin/tasks/{tid}")
        self.assertEqual(got.status_code, 200)
        self.assertEqual(got.json()["task"]["title"], "WL self-ticket")

    def test_settings_page_renders(self) -> None:
        r = self.client.get("/admin/settings")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Id prefix", r.text)
        self.assertIn("products.json", r.text)

    def test_docs_index_redirects_to_first_doc(self) -> None:
        r = self.client.get("/admin/docs", follow_redirects=False)
        self.assertEqual(r.status_code, 307)
        self.assertTrue(r.headers["location"].startswith("/admin/docs/"))

    def test_docs_page_renders_process_md(self) -> None:
        r = self.client.get("/admin/docs/process")
        self.assertEqual(r.status_code, 200)
        self.assertIn("PROTOCOL.md", r.text)
        self.assertIn("ts-doc-body", r.text)

    def test_docs_page_renders_all_known_docs(self) -> None:
        for slug in ("process", "truth", "readme", "claude"):
            r = self.client.get(f"/admin/docs/{slug}")
            self.assertEqual(r.status_code, 200, msg=slug)

    def test_docs_page_unknown_doc_404s(self) -> None:
        r = self.client.get("/admin/docs/nope")
        self.assertEqual(r.status_code, 404)

    # ── product bootstrap (wl-12) ───────────────────────────────────────

    def test_create_product_bootstraps_store(self) -> None:
        r = self.client.post(
            "/api/admin/products",
            json={"slug": "myapp", "display": "My App", "prefix": "ma"},
        )
        self.assertEqual(r.status_code, 200)
        product = r.json()["product"]
        self.assertEqual(product["slug"], "myapp")
        self.assertEqual(product["display"], "My App")
        self.assertEqual(product["prefix"], "ma")
        self.assertTrue((self.root / "data" / "myapp.db").exists())

        # the new surface accepts tickets immediately, no restart needed
        r = self.client.post(
            "/api/admin/tasks", json={"title": "first", "author": "work-pool", "description": "test intake body", "surface": "myapp"}
        )
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["task"]["id"].startswith("ma-"))

    def test_create_product_rejects_bad_slug(self) -> None:
        for bad in ("", "  ", "Has Spaces", "-leading-dash", "way" * 20):
            r = self.client.post("/api/admin/products", json={"slug": bad})
            self.assertEqual(r.status_code, 400, msg=bad)

    def test_create_product_rejects_reserved_slug(self) -> None:
        for reserved in ("all", "ops", "op"):
            r = self.client.post("/api/admin/products", json={"slug": reserved})
            self.assertEqual(r.status_code, 400)

    def test_create_product_rejects_duplicate(self) -> None:
        r = self.client.post("/api/admin/products", json={"slug": "dupe"})
        self.assertEqual(r.status_code, 200)
        r = self.client.post("/api/admin/products", json={"slug": "dupe"})
        self.assertEqual(r.status_code, 409)

        r = self.client.post("/api/admin/products", json={"slug": "tradeos"})
        self.assertEqual(r.status_code, 409)

    def test_create_product_rejects_prefix_collision(self) -> None:
        r = self.client.post(
            "/api/admin/products", json={"slug": "one", "prefix": "x"}
        )
        self.assertEqual(r.status_code, 200)
        r = self.client.post(
            "/api/admin/products", json={"slug": "two", "prefix": "x"}
        )
        self.assertEqual(r.status_code, 400)

    # ── product edit (wl-17) ─────────────────────────────────────────────

    def test_update_product_renames_display_and_prefix(self) -> None:
        self.client.post("/api/admin/products", json={"slug": "myapp"})
        r = self.client.patch(
            "/api/admin/products/myapp",
            json={"display": "My App", "prefix": "ma"},
        )
        self.assertEqual(r.status_code, 200)
        product = r.json()["product"]
        self.assertEqual(product["display"], "My App")
        self.assertEqual(product["prefix"], "ma")

        # persisted, not just returned in the response — new tickets under
        # this surface pick up the renamed prefix
        spec = self.client.post(
            "/api/admin/tasks",
            json={"title": "t", "author": "work-pool", "description": "d", "surface": "myapp"},
        )
        self.assertTrue(spec.json()["task"]["id"].startswith("ma-"))

    def test_update_product_unknown_slug_404s(self) -> None:
        r = self.client.patch(
            "/api/admin/products/nosuchproduct", json={"display": "X"}
        )
        self.assertEqual(r.status_code, 404)

    def test_update_product_requires_a_field(self) -> None:
        self.client.post("/api/admin/products", json={"slug": "myapp"})
        r = self.client.patch("/api/admin/products/myapp", json={})
        self.assertEqual(r.status_code, 400)

    def test_update_product_rejects_blank_display(self) -> None:
        self.client.post("/api/admin/products", json={"slug": "myapp"})
        r = self.client.patch("/api/admin/products/myapp", json={"display": "   "})
        self.assertEqual(r.status_code, 400)

    def test_update_product_rejects_reserved_o_prefix(self) -> None:
        self.client.post("/api/admin/products", json={"slug": "myapp"})
        r = self.client.patch("/api/admin/products/myapp", json={"prefix": "o"})
        self.assertEqual(r.status_code, 400)

    def test_update_product_rejects_prefix_collision(self) -> None:
        self.client.post("/api/admin/products", json={"slug": "one", "prefix": "x"})
        self.client.post("/api/admin/products", json={"slug": "two", "prefix": "y"})
        r = self.client.patch("/api/admin/products/two", json={"prefix": "x"})
        self.assertEqual(r.status_code, 400)

    def test_update_product_allows_keeping_own_prefix(self) -> None:
        self.client.post("/api/admin/products", json={"slug": "one", "prefix": "x"})
        r = self.client.patch(
            "/api/admin/products/one", json={"display": "One", "prefix": "x"}
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["product"]["prefix"], "x")

    def test_board_page_per_surface(self) -> None:
        SQLiteTracker(
            db_path=self.root / "data" / "worklane.db"
        ).create_task(title="seed")
        self.assertEqual(
            self.client.get("/admin/tickets/worklane?view=board").status_code,
            200,
        )
        self.assertEqual(
            self.client.get("/admin/tickets/all?view=board").status_code, 200
        )
        self.assertEqual(
            self.client.get("/admin/tickets/nosuch?view=board").status_code, 404
        )

    # ── PROTOCOL.md enforcement on comments ──────────────────────────

    def _mk_task(self) -> str:
        r = self.client.post("/api/admin/tasks", json={"title": "guard target", "author": "work-pool", "description": "test intake body"})
        return r.json()["task"]["id"]

    def test_create_requires_author_and_description(self) -> None:
        r = self.client.post("/api/admin/tasks", json={"title": "bare"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("author", r.json()["error"])

        r = self.client.post(
            "/api/admin/tasks", json={"title": "bare", "author": "cowork"}
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("description", r.json()["error"])

    def test_create_signs_intake_comment(self) -> None:
        tid = self._mk_task()
        got = self.client.get(f"/api/admin/tasks/{tid}").json()["task"]
        comments = got.get("comments") or []
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0]["author"], "work-pool")
        self.assertIn("Intake: filed by work-pool", comments[0]["body"])

    def test_comment_requires_author(self) -> None:
        tid = self._mk_task()
        r = self.client.post(
            f"/api/admin/tasks/{tid}/comments", json={"body": "hello"}
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("§3.8", r.json()["error"])

        r = self.client.post(
            f"/api/admin/tasks/{tid}/comments",
            json={"body": "hello", "author": "work-pool"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["comment"]["author"], "work-pool")

    def test_completed_comment_requires_verification_and_links(self) -> None:
        tid = self._mk_task()
        r = self.client.post(
            f"/api/admin/tasks/{tid}/comments",
            json={"body": "Completed: did the thing", "author": "grok"},
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("Verification", r.json()["error"])

        ok_body = (
            "Completed:\n- the thing\n\nVerification:\n- pytest green\n\n"
            "Links:\n- abc123 on main\n\nFollow-ups:\n- none"
        )
        r = self.client.post(
            f"/api/admin/tasks/{tid}/comments",
            json={"body": ok_body, "author": "grok"},
        )
        self.assertEqual(r.status_code, 200)

    def test_blocked_comment_requires_next_step(self) -> None:
        tid = self._mk_task()
        r = self.client.post(
            f"/api/admin/tasks/{tid}/comments",
            json={"body": "Blocked: no reason", "author": "cursor"},
        )
        self.assertEqual(r.status_code, 400)
        r = self.client.post(
            f"/api/admin/tasks/{tid}/comments",
            json={
                "body": "Blocked: upstream API down\nNext step: retry after deploy",
                "author": "cursor",
            },
        )
        self.assertEqual(r.status_code, 200)

    # ── wl-50: default-identity autonomous-write guard ───────────────

    def test_default_identity_owner_mismatch_logs_warning(self) -> None:
        tid = self._mk_task()
        with self.assertLogs("worklane.task_server", level="WARNING") as cm:
            r = self.client.post(
                f"/api/admin/tasks/{tid}/comments",
                json={
                    "body": "Owner: wl-pool (claude-sonnet-5)\nStart: now",
                    "author": "founder-terminal",
                },
            )
        self.assertEqual(r.status_code, 200)
        self.assertTrue(
            any("wl-pool" in msg and "founder-terminal" in msg for msg in cm.output)
        )

    def test_default_identity_normal_use_stays_silent(self) -> None:
        tid = self._mk_task()
        with self.assertRaises(AssertionError):
            with self.assertLogs("worklane.task_server", level="WARNING"):
                r = self.client.post(
                    f"/api/admin/tasks/{tid}/comments",
                    json={"body": "just a note", "author": "founder-terminal"},
                )
                self.assertEqual(r.status_code, 200)

    def test_non_default_identity_owner_marker_stays_silent(self) -> None:
        tid = self._mk_task()
        with self.assertRaises(AssertionError):
            with self.assertLogs("worklane.task_server", level="WARNING"):
                r = self.client.post(
                    f"/api/admin/tasks/{tid}/comments",
                    json={
                        "body": "Owner: wl-pool (claude-sonnet-5)\nStart: now",
                        "author": "work-pool",
                    },
                )
                self.assertEqual(r.status_code, 200)


if __name__ == "__main__":
    unittest.main()
