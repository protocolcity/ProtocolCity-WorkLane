"""wl-191: assignment-aware worker= filter on /api/admin/tasks/ready.

Default lanes (no label= narrowing) probe the whole product pool, which
counts other workers' worker:* assigned tickets they may never claim —
the morgan 2026-07-16 false-WEDGED class. worker=<name> applies the
assignment law: a ticket carrying any worker:* label is ready for the
caller only when worker:<name> matches; unlabeled tickets stay ready
for everyone.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from worklane.trackers.sqlite import SQLiteTracker


class ReadyWorkerFilterTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wl_ready_worker_")
        self.root = Path(self._tmp.name)
        (self.root / "data").mkdir(parents=True, exist_ok=True)
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
        os.environ["WORKLANE_DB"] = str(self.root / "data" / "tradeos.db")
        os.environ.pop("TRADEOS_TRACKER_DB", None)
        os.environ["TRADEOS_TICKETS_SOURCE"] = "sqlite"
        os.environ["WL_DEFAULT_PRODUCT"] = "tradeos"
        os.environ.pop("WL_DEFAULT_PROJECT", None)
        os.environ.pop("WL_PROJECT", None)
        os.environ.pop("WL_PRODUCT", None)
        self.tracker = SQLiteTracker(db_path=self.root / "data" / "tradeos.db")

        from worklane.task_server import router

        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)

        self.unlabeled = self.tracker.create_task(title="Anyone's work")
        self.mine = self.tracker.create_task(
            title="Morgan's work", labels=["worker:morgan"]
        )
        self.theirs = self.tracker.create_task(
            title="Neo's feed leaf", labels=["worker:neo"]
        )

    def tearDown(self) -> None:
        for k, v in self._env_before.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    def _ready_ids(self, **params: str) -> set:
        r = self.client.get(
            "/api/admin/tasks/ready", params={"product": "tradeos", **params}
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["count"], len(body["tasks"]))
        return {row["id"] for row in body["tasks"]}

    def test_no_worker_param_keeps_full_pool(self) -> None:
        ids = self._ready_ids()
        self.assertEqual(
            ids,
            {
                f"t-{self.unlabeled.id}",
                f"t-{self.mine.id}",
                f"t-{self.theirs.id}",
            },
        )

    def test_worker_sees_own_and_unassigned_only(self) -> None:
        ids = self._ready_ids(worker="morgan")
        self.assertIn(f"t-{self.unlabeled.id}", ids)
        self.assertIn(f"t-{self.mine.id}", ids)
        self.assertNotIn(f"t-{self.theirs.id}", ids)

    def test_worker_name_is_case_insensitive(self) -> None:
        ids = self._ready_ids(worker="Morgan")
        self.assertIn(f"t-{self.mine.id}", ids)
        self.assertNotIn(f"t-{self.theirs.id}", ids)

    def test_worker_with_no_assignments_sees_only_unassigned(self) -> None:
        ids = self._ready_ids(worker="tess")
        self.assertEqual(ids, {f"t-{self.unlabeled.id}"})

    def test_worker_composes_with_label_filter(self) -> None:
        both = self.tracker.create_task(
            title="Labeled and assigned", labels=["area:ui", "worker:morgan"]
        )
        ids = self._ready_ids(worker="morgan", label="area:ui")
        self.assertEqual(ids, {f"t-{both.id}"})


if __name__ == "__main__":
    unittest.main()
