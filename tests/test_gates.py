"""wl-21: gates as data.

Gate fields (gate_type/gate_until/gate_note) withhold a ticket from the
ready queue. Human gates withhold until manually cleared; timer gates
withhold until gate_until passes, then auto-thaw — computed at read time
(task_is_gated), unlike the dependency-freeze label which is written by a
mutating status transition.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from worklane.devqueue.queue import WorkQueue
from worklane.trackers.protocol import Task, task_is_gated
from worklane.trackers.sqlite import SQLiteTracker


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class TaskIsGatedTest(unittest.TestCase):
    def test_no_gate(self) -> None:
        t = Task(id="1", title="x")
        self.assertFalse(task_is_gated(t))

    def test_human_gate_always_gated(self) -> None:
        t = Task(id="1", title="x", gate_type="human")
        self.assertTrue(task_is_gated(t))

    def test_timer_gate_future_is_gated(self) -> None:
        future = _iso(datetime.now(timezone.utc) + timedelta(days=1))
        t = Task(id="1", title="x", gate_type="timer", gate_until=future)
        self.assertTrue(task_is_gated(t))

    def test_timer_gate_past_auto_thaws(self) -> None:
        past = _iso(datetime.now(timezone.utc) - timedelta(days=1))
        t = Task(id="1", title="x", gate_type="timer", gate_until=past)
        self.assertFalse(task_is_gated(t))

    def test_timer_gate_missing_until_fails_safe_gated(self) -> None:
        t = Task(id="1", title="x", gate_type="timer", gate_until=None)
        self.assertTrue(task_is_gated(t))

    def test_timer_gate_unparseable_until_fails_safe_gated(self) -> None:
        t = Task(id="1", title="x", gate_type="timer", gate_until="not-a-date")
        self.assertTrue(task_is_gated(t))


class SQLiteTrackerGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory(prefix="tracker_gates_")
        self.db_path = Path(self.tmpdir.name) / "tickets.db"
        self.tracker = SQLiteTracker(db_path=self.db_path)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_set_human_gate(self) -> None:
        t = self.tracker.create_task(title="Gate me")
        updated = self.tracker.update_task(
            t.id, gate_type="human", gate_note="waiting on founder", actor="wl-pool"
        )
        self.assertIsNotNone(updated)
        self.assertEqual(updated.gate_type, "human")
        self.assertEqual(updated.gate_note, "waiting on founder")
        self.assertIsNone(updated.gate_until)

    def test_set_timer_gate_requires_gate_until(self) -> None:
        t = self.tracker.create_task(title="Gate me")
        with self.assertRaises(ValueError):
            self.tracker.update_task(t.id, gate_type="timer", actor="wl-pool")

    def test_set_timer_gate(self) -> None:
        t = self.tracker.create_task(title="Gate me")
        future = _iso(datetime.now(timezone.utc) + timedelta(days=7))
        updated = self.tracker.update_task(
            t.id, gate_type="timer", gate_until=future, actor="wl-pool"
        )
        self.assertEqual(updated.gate_type, "timer")
        self.assertEqual(updated.gate_until, future)

    def test_clear_gate(self) -> None:
        t = self.tracker.create_task(title="Gate me")
        self.tracker.update_task(t.id, gate_type="human", actor="wl-pool")
        cleared = self.tracker.update_task(t.id, gate_type="", actor="wl-pool")
        self.assertIsNone(cleared.gate_type)
        self.assertIsNone(cleared.gate_until)
        self.assertIsNone(cleared.gate_note)

    def test_invalid_gate_type_rejected(self) -> None:
        t = self.tracker.create_task(title="Gate me")
        with self.assertRaises(ValueError):
            self.tracker.update_task(t.id, gate_type="bogus", actor="wl-pool")

    def test_gate_until_without_gate_type_rejected(self) -> None:
        t = self.tracker.create_task(title="Gate me")
        with self.assertRaises(ValueError):
            self.tracker.update_task(t.id, gate_until="2099-01-01T00:00:00Z", actor="wl-pool")

    def test_gate_survives_reconnect(self) -> None:
        """Regression guard for the ALTER TABLE migration path."""
        t = self.tracker.create_task(title="Gate me")
        self.tracker.update_task(t.id, gate_type="human", actor="wl-pool")
        reconnected = SQLiteTracker(db_path=self.db_path)
        fetched = reconnected.get_task(t.id)
        self.assertEqual(fetched.gate_type, "human")


class WorkQueueGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory(prefix="tracker_gates_wq_")
        self.db_path = Path(self.tmpdir.name) / "tickets.db"
        self.tracker = SQLiteTracker(db_path=self.db_path)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_ready_excludes_human_gated(self) -> None:
        gated = self.tracker.create_task(title="Gated ticket")
        self.tracker.update_task(gated.id, gate_type="human", actor="wl-pool")
        free = self.tracker.create_task(title="Free ticket")

        wq = WorkQueue(self.tracker)
        ready_ids = {t.id for t in wq.ready()}
        self.assertIn(free.id, ready_ids)
        self.assertNotIn(gated.id, ready_ids)

    def test_ready_includes_expired_timer_gate(self) -> None:
        past = _iso(datetime.now(timezone.utc) - timedelta(days=1))
        thawed = self.tracker.create_task(title="Timer expired")
        self.tracker.update_task(
            thawed.id, gate_type="timer", gate_until=past, actor="wl-pool"
        )

        wq = WorkQueue(self.tracker)
        ready_ids = {t.id for t in wq.ready()}
        self.assertIn(thawed.id, ready_ids)


class HttpGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wl_gates_http_")
        self.root = Path(self._tmp.name)
        (self.root / "data").mkdir(parents=True, exist_ok=True)
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
        os.environ["WORKLANE_RUNTIME_DIR"] = str(self.root)
        os.environ["WORKLANE_DB"] = str(self.root / "data" / "tradeos.db")
        os.environ.pop("TRADEOS_TRACKER_DB", None)
        os.environ["TRADEOS_TICKETS_SOURCE"] = "sqlite"
        os.environ["WL_DEFAULT_PRODUCT"] = "tradeos"
        os.environ.pop("WL_PRODUCT", None)
        self.tracker = SQLiteTracker(db_path=self.root / "data" / "tradeos.db")

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

    def test_patch_sets_gate(self) -> None:
        t = self.tracker.create_task(title="HTTP gate")
        r = self.client.patch(
            f"/api/admin/tasks/{t.id}",
            json={"gate_type": "human", "gate_note": "founder call"},
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["task"]["gate_type"], "human")

    def test_patch_timer_gate_without_until_is_400(self) -> None:
        t = self.tracker.create_task(title="HTTP gate")
        r = self.client.patch(f"/api/admin/tasks/{t.id}", json={"gate_type": "timer"})
        self.assertEqual(r.status_code, 400)

    def test_ready_endpoint_excludes_gated(self) -> None:
        gated = self.tracker.create_task(title="Gated via HTTP")
        self.tracker.update_task(gated.id, gate_type="human", actor="wl-pool")
        free = self.tracker.create_task(title="Free via HTTP")

        r = self.client.get("/api/admin/tasks/ready", params={"product": "tradeos"})
        self.assertEqual(r.status_code, 200)
        ids = {row["id"] for row in r.json()["tasks"]}
        self.assertIn(f"t-{free.id}", ids)
        self.assertNotIn(f"t-{gated.id}", ids)


class GateChipRenderTest(unittest.TestCase):
    def test_gate_chip_rendered_on_gated_card(self) -> None:
        from worklane.board import _render_task_card

        gated_task = Task(id="1", title="Gated card", gate_type="human")
        html = _render_task_card(gated_task, preview={})
        self.assertIn("tb-card-gate", html)
        self.assertIn("Gated", html)

    def test_no_gate_chip_on_ungated_card(self) -> None:
        from worklane.board import _render_task_card

        plain_task = Task(id="1", title="Plain card")
        html = _render_task_card(plain_task, preview={})
        self.assertNotIn("tb-card-gate", html)


class HumanGateCountTrackerTest(unittest.TestCase):
    """wl-205: SQLiteTracker.count_human_gate_sets_since and human_gate_stats_since."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory(prefix="wl_hgc_")
        self.db_path = Path(self.tmpdir.name) / "tickets.db"
        self.tracker = SQLiteTracker(db_path=self.db_path)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_count_zero_on_fresh_db(self) -> None:
        since = "2000-01-01T00:00:00Z"
        self.assertEqual(self.tracker.count_human_gate_sets_since("alice", since), 0)

    def test_count_increments_per_author(self) -> None:
        since = "2000-01-01T00:00:00Z"
        for i in range(3):
            t = self.tracker.create_task(title=f"Task {i}")
            self.tracker.update_task(t.id, gate_type="human", actor="alice")
        self.assertEqual(self.tracker.count_human_gate_sets_since("alice", since), 3)

    def test_count_isolates_by_author(self) -> None:
        since = "2000-01-01T00:00:00Z"
        for i in range(2):
            t = self.tracker.create_task(title=f"Alice task {i}")
            self.tracker.update_task(t.id, gate_type="human", actor="alice")
        t2 = self.tracker.create_task(title="Bob task")
        self.tracker.update_task(t2.id, gate_type="human", actor="bob")
        self.assertEqual(self.tracker.count_human_gate_sets_since("alice", since), 2)
        self.assertEqual(self.tracker.count_human_gate_sets_since("bob", since), 1)

    def test_count_excludes_clears(self) -> None:
        since = "2000-01-01T00:00:00Z"
        t = self.tracker.create_task(title="Toggle")
        self.tracker.update_task(t.id, gate_type="human", actor="alice")
        self.tracker.update_task(t.id, gate_type="", actor="alice")
        self.assertEqual(self.tracker.count_human_gate_sets_since("alice", since), 1)

    def test_human_gate_stats_since_aggregates(self) -> None:
        since = "2000-01-01T00:00:00Z"
        for _ in range(2):
            t = self.tracker.create_task(title="x")
            self.tracker.update_task(t.id, gate_type="human", actor="alice")
        t2 = self.tracker.create_task(title="y")
        self.tracker.update_task(t2.id, gate_type="human", actor="bob")
        stats = self.tracker.human_gate_stats_since(since)
        by_author = {row["author"]: row["count"] for row in stats}
        self.assertEqual(by_author["alice"], 2)
        self.assertEqual(by_author["bob"], 1)

    def test_human_gate_stats_empty_on_fresh_db(self) -> None:
        stats = self.tracker.human_gate_stats_since("2000-01-01T00:00:00Z")
        self.assertEqual(stats, [])


class HumanGateHardStopHttpTest(unittest.TestCase):
    """wl-205: PATCH /api/admin/tasks/{id} hard stop at 3 human gates per 2h."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wl_hghs_")
        self.root = Path(self._tmp.name)
        (self.root / "data").mkdir(parents=True, exist_ok=True)
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
        os.environ["WORKLANE_RUNTIME_DIR"] = str(self.root)
        os.environ["WORKLANE_DB"] = str(self.root / "data" / "tradeos.db")
        os.environ.pop("TRADEOS_TRACKER_DB", None)
        os.environ["TRADEOS_TICKETS_SOURCE"] = "sqlite"
        os.environ["WL_DEFAULT_PRODUCT"] = "tradeos"
        os.environ.pop("WL_PRODUCT", None)
        self.tracker = SQLiteTracker(db_path=self.root / "data" / "tradeos.db")

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

    def _patch_human_gate(self, task_id: str, author: str, **extra: object) -> object:
        body = {"gate_type": "human", "author": author}
        body.update(extra)
        return self.client.patch(f"/api/admin/tasks/{task_id}", json=body)

    def test_first_three_gates_allowed(self) -> None:
        for i in range(3):
            t = self.tracker.create_task(title=f"Ticket {i}")
            r = self._patch_human_gate(f"t-{t.id}", "alice")
            self.assertEqual(r.status_code, 200, f"gate {i+1} should be allowed")

    def test_fourth_gate_blocked(self) -> None:
        for i in range(3):
            t = self.tracker.create_task(title=f"Ticket {i}")
            self._patch_human_gate(f"t-{t.id}", "alice")
        t4 = self.tracker.create_task(title="Fourth")
        r = self._patch_human_gate(f"t-{t4.id}", "alice")
        self.assertEqual(r.status_code, 429)
        body = r.json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["limit"], 3)
        self.assertEqual(body["human_gate_count"], 3)

    def test_different_authors_independent(self) -> None:
        for i in range(3):
            t = self.tracker.create_task(title=f"Ticket {i}")
            self._patch_human_gate(f"t-{t.id}", "alice")
        t_bob = self.tracker.create_task(title="Bob ticket")
        r = self._patch_human_gate(f"t-{t_bob.id}", "bob")
        self.assertEqual(r.status_code, 200)

    def test_bulk_gate_ok_bypasses_limit(self) -> None:
        for i in range(3):
            t = self.tracker.create_task(title=f"Ticket {i}")
            self._patch_human_gate(f"t-{t.id}", "alice")
        t4 = self.tracker.create_task(title="Fourth")
        r = self._patch_human_gate(
            f"t-{t4.id}",
            "alice",
            bulk_gate_ok=True,
            ticket_ids=[f"t-{t4.id}"],
            authorizing_ticket="t-99",
        )
        self.assertEqual(r.status_code, 200)

    def test_bulk_gate_ok_without_ticket_ids_is_400(self) -> None:
        for i in range(3):
            t = self.tracker.create_task(title=f"Ticket {i}")
            self._patch_human_gate(f"t-{t.id}", "alice")
        t4 = self.tracker.create_task(title="Fourth")
        r = self._patch_human_gate(
            f"t-{t4.id}",
            "alice",
            bulk_gate_ok=True,
            authorizing_ticket="t-99",
        )
        self.assertEqual(r.status_code, 400)

    def test_bulk_gate_ok_without_authorizing_ticket_is_400(self) -> None:
        for i in range(3):
            t = self.tracker.create_task(title=f"Ticket {i}")
            self._patch_human_gate(f"t-{t.id}", "alice")
        t4 = self.tracker.create_task(title="Fourth")
        r = self._patch_human_gate(
            f"t-{t4.id}",
            "alice",
            bulk_gate_ok=True,
            ticket_ids=[f"t-{t4.id}"],
        )
        self.assertEqual(r.status_code, 400)

    def test_no_author_uses_cli_update_bucket(self) -> None:
        for i in range(3):
            t = self.tracker.create_task(title=f"Ticket {i}")
            self.client.patch(
                f"/api/admin/tasks/t-{t.id}",
                json={"gate_type": "human"},
            )
        t4 = self.tracker.create_task(title="Fourth anon")
        r = self.client.patch(
            f"/api/admin/tasks/t-{t4.id}",
            json={"gate_type": "human"},
        )
        self.assertEqual(r.status_code, 429)


if __name__ == "__main__":
    unittest.main()
