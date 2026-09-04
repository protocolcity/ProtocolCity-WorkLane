"""wl-493: GET /api/admin/tasks?q= matches id and title across statuses.

Bound search for the suite searchlight proxy. Existing list without q=
must stay unchanged (default limit 200, status filter, descriptions).
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


_FAT = ("body " * 80) + "\n## Detail\n" + ("paragraph " * 40)


class AdminTasksSearchTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wl_admin_tasks_search_")
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
        self.beta = SQLiteTracker(db_path=self.root / "data" / "beta.db")
        self._seed()

        from worklane.task_server import router

        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)

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
        self.needle = self.alpha.create_task(
            title="searchlight needle",
            description=_FAT,
        )
        self.closed = self.alpha.create_task(
            title="closed slip xyzzy",
            description="done body",
        )
        self.alpha.update_status(self.closed.id, TaskStatus.DONE)
        self.canceled = self.alpha.create_task(
            title="canceled slip plugh",
            description="canceled body",
        )
        self.alpha.update_status(self.canceled.id, TaskStatus.CANCELED)
        self.ordinary = self.alpha.create_task(
            title="ordinary backlog card",
            description="keep me",
        )
        self.beta_cousin = self.beta.create_task(
            title="searchlight cousin",
            description="beta fat",
        )
        self.beta_other = self.beta.create_task(
            title="unrelated beta card",
            description="nope",
        )
        self.pad = []
        for i in range(55):
            self.pad.append(
                self.alpha.create_task(
                    title="capneedle pad %02d" % i,
                    description="pad",
                )
            )

    def _ids(self, payload):
        return [t["id"] for t in payload.get("tasks") or []]

    def test_q_matches_composite_id(self) -> None:
        composite = "t-%s" % self.needle.id
        r = self.client.get(
            "/api/admin/tasks",
            params={"project": "tradeos", "q": composite},
        )
        self.assertEqual(r.status_code, 200)
        j = r.json()
        self.assertTrue(j["ok"])
        self.assertIn(composite, self._ids(j))
        hit = [t for t in j["tasks"] if t["id"] == composite][0]
        self.assertEqual(hit["title"], "searchlight needle")

    def test_q_matches_bare_numeric_id(self) -> None:
        r = self.client.get(
            "/api/admin/tasks",
            params={"project": "tradeos", "q": str(self.needle.id)},
        )
        self.assertEqual(r.status_code, 200)
        j = r.json()
        self.assertTrue(j["ok"])
        self.assertIn("t-%s" % self.needle.id, self._ids(j))

    def test_q_matches_title_substring(self) -> None:
        r = self.client.get(
            "/api/admin/tasks",
            params={"project": "tradeos", "q": "searchlight"},
        )
        self.assertEqual(r.status_code, 200)
        j = r.json()
        self.assertTrue(j["ok"])
        ids = self._ids(j)
        self.assertIn("t-%s" % self.needle.id, ids)
        self.assertNotIn("t-%s" % self.ordinary.id, ids)

    def test_q_matches_closed_and_canceled_when_status_omitted(self) -> None:
        r = self.client.get(
            "/api/admin/tasks",
            params={"project": "tradeos", "q": "xyzzy"},
        )
        self.assertEqual(r.status_code, 200)
        j = r.json()
        self.assertTrue(j["ok"])
        self.assertIn("t-%s" % self.closed.id, self._ids(j))
        hit = [t for t in j["tasks"] if t["id"] == "t-%s" % self.closed.id][0]
        self.assertEqual(hit["status"], TaskStatus.DONE)

        r2 = self.client.get(
            "/api/admin/tasks",
            params={"project": "tradeos", "q": "plugh"},
        )
        self.assertIn("t-%s" % self.canceled.id, self._ids(r2.json()))

        # Explicit status still filters.
        r3 = self.client.get(
            "/api/admin/tasks",
            params={
                "project": "tradeos",
                "q": "xyzzy",
                "status": TaskStatus.BACKLOG,
            },
        )
        self.assertEqual(self._ids(r3.json()), [])

    def test_q_default_limit_20_and_max_50(self) -> None:
        r = self.client.get(
            "/api/admin/tasks",
            params={"project": "tradeos", "q": "capneedle"},
        )
        self.assertEqual(r.status_code, 200)
        j = r.json()
        self.assertTrue(j["ok"])
        self.assertEqual(len(j["tasks"]), 20)

        r3 = self.client.get(
            "/api/admin/tasks",
            params={"project": "tradeos", "q": "capneedle", "limit": 3},
        )
        self.assertEqual(len(r3.json()["tasks"]), 3)

        r_cap = self.client.get(
            "/api/admin/tasks",
            params={"project": "tradeos", "q": "capneedle", "limit": 999},
        )
        self.assertEqual(len(r_cap.json()["tasks"]), 50)

    def test_q_unknown_returns_empty_ok(self) -> None:
        r = self.client.get(
            "/api/admin/tasks",
            params={"project": "tradeos", "q": "no-such-token-zzzz"},
        )
        self.assertEqual(r.status_code, 200)
        j = r.json()
        self.assertTrue(j["ok"])
        self.assertEqual(j["tasks"], [])

    def test_list_without_q_unchanged(self) -> None:
        r = self.client.get(
            "/api/admin/tasks",
            params={"project": "tradeos", "limit": 200},
        )
        self.assertEqual(r.status_code, 200)
        j = r.json()
        self.assertTrue(j["ok"])
        # 1 needle + 1 closed + 1 canceled + 1 ordinary + 55 pad = 59
        self.assertEqual(len(j["tasks"]), 59)
        fat = [t for t in j["tasks"] if t["id"] == "t-%s" % self.needle.id][0]
        self.assertIn("paragraph", fat["description"])

        backlog = self.client.get(
            "/api/admin/tasks",
            params={"project": "tradeos", "status": TaskStatus.BACKLOG},
        ).json()
        self.assertTrue(
            all(t["status"] == TaskStatus.BACKLOG for t in backlog["tasks"])
        )
        self.assertNotIn("t-%s" % self.closed.id, self._ids(backlog))

    def test_q_does_not_dump_description(self) -> None:
        r = self.client.get(
            "/api/admin/tasks",
            params={"project": "tradeos", "q": "searchlight needle"},
        )
        j = r.json()
        hit = [t for t in j["tasks"] if t["id"] == "t-%s" % self.needle.id][0]
        self.assertEqual(hit["description"], "")

    def test_q_project_all_cross_store(self) -> None:
        r = self.client.get(
            "/api/admin/tasks",
            params={"project": "all", "q": "searchlight"},
        )
        self.assertEqual(r.status_code, 200)
        ids = self._ids(r.json())
        self.assertIn("t-%s" % self.needle.id, ids)
        self.assertIn("beta-%s" % self.beta_cousin.id, ids)
        self.assertNotIn("beta-%s" % self.beta_other.id, ids)

    def test_q_composite_does_not_hit_other_store_same_rowid(self) -> None:
        # beta also has rowid 1. q=t-1 must not return beta-1.
        r = self.client.get(
            "/api/admin/tasks",
            params={"project": "all", "q": "t-%s" % self.needle.id},
        )
        ids = self._ids(r.json())
        self.assertIn("t-%s" % self.needle.id, ids)
        self.assertNotIn("beta-%s" % self.beta_cousin.id, ids)

    def test_q_like_metacharacters_do_not_match_all(self) -> None:
        r = self.client.get(
            "/api/admin/tasks",
            params={"project": "tradeos", "q": "%"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["tasks"], [])

        r2 = self.client.get(
            "/api/admin/tasks",
            params={"project": "tradeos", "q": "_"},
        )
        self.assertEqual(r2.json()["tasks"], [])


if __name__ == "__main__":
    unittest.main()
