"""wl-23 slice 2: Settings compact API, detail read-through, board exclusion.

Fixture stores only — never the live product DBs under local/data/.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from worklane import archival
from worklane.trackers.protocol import TaskStatus
from worklane.trackers.sqlite import SQLiteTracker


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


class ArchivalHttpTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wl_archival_http_")
        self.root = Path(self._tmp.name)
        (self.root / "data").mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "data" / "tradeos.db"

        self._env_before = {
            k: os.environ.get(k)
            for k in (
                "WORKLANE_RUNTIME_DIR",
                "WORKLANE_DB",
                "TRADEOS_TRACKER_DB",
                "TRADEOS_TICKETS_SOURCE",
                "WL_DEFAULT_PROJECT",
                "WL_DEFAULT_PRODUCT",
                "WL_PROJECT",
                "WL_PRODUCT",
            )
        }
        os.environ["WORKLANE_RUNTIME_DIR"] = str(self.root)
        os.environ["WORKLANE_DB"] = str(self.db_path)
        os.environ.pop("TRADEOS_TRACKER_DB", None)
        os.environ["TRADEOS_TICKETS_SOURCE"] = "sqlite"
        os.environ["WL_DEFAULT_PRODUCT"] = "tradeos"
        os.environ.pop("WL_DEFAULT_PROJECT", None)
        os.environ.pop("WL_PROJECT", None)
        os.environ.pop("WL_PRODUCT", None)

        self.tracker = SQLiteTracker(db_path=self.db_path)
        self.now = datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc)

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

    def _make(self, *, title: str, status: str, age_days: int, ext_id=None):
        t = self.tracker.create_task(title=title, description="x", ext_id=ext_id)
        self.tracker.update_status(t.id, status)
        self._set_updated(t.id, self.now - timedelta(days=age_days))
        return t

    def _set_updated(self, task_id: str, dt: datetime) -> None:
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute(
                "UPDATE tasks SET updated_at = ? WHERE id = ?",
                (_iso(dt), int(task_id)),
            )
            conn.commit()
        finally:
            conn.close()

    def test_detail_read_through_archived_id(self) -> None:
        cold = self._make(title="archived detail", status=TaskStatus.DONE, age_days=200)
        live = self._make(title="still hot", status=TaskStatus.BACKLOG, age_days=1)
        archival.archive_cold_tickets(
            self.db_path, older_than_days=90, now=self.now
        )

        r = self.client.get(f"/api/admin/tasks/{cold.id}")
        self.assertEqual(r.status_code, 200, r.text)
        j = r.json()
        self.assertTrue(j.get("ok"))
        self.assertTrue(j.get("archived"))
        self.assertEqual(j["task"]["title"], "archived detail")
        self.assertTrue(j["task"].get("archived"))

        r_html = self.client.get(f"/admin/tasks/{cold.id}")
        self.assertEqual(r_html.status_code, 200)
        self.assertIn("Archived (cold storage)", r_html.text)
        self.assertIn("archived detail", r_html.text)

        # Live tickets still load from hot without the archived flag.
        r_live = self.client.get(f"/api/admin/tasks/{live.id}")
        self.assertEqual(r_live.status_code, 200)
        self.assertFalse(r_live.json().get("archived"))

    def test_board_excludes_archived_after_compact(self) -> None:
        cold = self._make(title="cold board", status=TaskStatus.DONE, age_days=200)
        live = self._make(title="live board", status=TaskStatus.BACKLOG, age_days=1)

        cold_cid = f"t-{cold.id}"
        live_cid = f"t-{live.id}"

        before = self.client.get("/api/admin/tasks?product=tradeos&limit=500")
        self.assertEqual(before.status_code, 200)
        ids_before = {t["id"] for t in before.json()["tasks"]}
        self.assertIn(cold_cid, ids_before)
        self.assertIn(live_cid, ids_before)
        counts_before = before.json()["scope_counts"]
        self.assertGreaterEqual(counts_before.get(TaskStatus.DONE, 0), 1)

        r = self.client.post(
            "/api/admin/products/tradeos/compact",
            json={"older_than_days": 90},
        )
        # Compact API uses wall-clock now; tickets aged 200d absolute still qualify.
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json().get("ok"))
        self.assertGreaterEqual(r.json().get("tickets", 0), 1)

        after = self.client.get("/api/admin/tasks?product=tradeos&limit=500")
        self.assertEqual(after.status_code, 200)
        j = after.json()
        ids_after = {t["id"] for t in j["tasks"]}
        self.assertNotIn(cold_cid, ids_after)
        self.assertIn(live_cid, ids_after)
        # Done column no longer counts the archived cold ticket.
        self.assertEqual(j["scope_counts"].get(TaskStatus.DONE, 0), 0)

    def test_compact_api_and_settings_surface(self) -> None:
        self._make(title="old done", status=TaskStatus.DONE, age_days=200)
        r = self.client.post(
            "/api/admin/products/tradeos/compact",
            json={"older_than_days": 90},
        )
        self.assertEqual(r.status_code, 200, r.text)
        j = r.json()
        self.assertTrue(j["ok"])
        self.assertEqual(j["tickets"], 1)
        self.assertEqual(j["archive_count"], 1)

        settings = self.client.get("/admin/settings")
        self.assertEqual(settings.status_code, 200)
        self.assertIn("Done work order archival", settings.text)
        self.assertIn("Compact now", settings.text)
        self.assertIn("tsSettingsCompact", settings.text)

    def test_archive_db_not_discovered_as_product(self) -> None:
        from worklane import products as products_mod

        self._make(title="old", status=TaskStatus.DONE, age_days=200)
        archival.archive_cold_tickets(
            self.db_path, older_than_days=90, now=self.now
        )
        archive = archival.archive_db_path_for(self.db_path)
        self.assertTrue(archive.exists())
        slugs = [s.slug for s in products_mod.discover_products()]
        self.assertIn("tradeos", slugs)
        self.assertNotIn("tradeos_archive", slugs)

    def test_compact_unknown_product_404(self) -> None:
        r = self.client.post(
            "/api/admin/products/nope/compact",
            json={"older_than_days": 90},
        )
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
