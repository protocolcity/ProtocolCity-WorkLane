"""wl-265: gate class filter chips — board UI and API filter param."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from worklane.board import (
    _parse_gate_filter,
    _wq_column_counts,
    _wq_gate_counts,
    _render_work_queue_filters,
)
from worklane.trackers.protocol import Task, TaskStatus


class ParseGateFilterTest(unittest.TestCase):
    def test_empty_returns_none(self) -> None:
        self.assertIsNone(_parse_gate_filter(""))

    def test_none_value_returns_empty_string(self) -> None:
        self.assertEqual(_parse_gate_filter("none"), "")

    def test_human_returns_human(self) -> None:
        self.assertEqual(_parse_gate_filter("human"), "human")

    def test_deferred_returns_deferred(self) -> None:
        self.assertEqual(_parse_gate_filter("deferred"), "deferred")

    def test_unknown_returns_none(self) -> None:
        self.assertIsNone(_parse_gate_filter("bogus"))


class WqGateCountsTest(unittest.TestCase):
    def _tasks(self) -> list:
        return [
            Task(id="1", title="ready", status=TaskStatus.BACKLOG, gate_type=None),
            Task(id="2", title="for you", status=TaskStatus.BACKLOG, gate_type="human"),
            Task(id="3", title="deferred", status=TaskStatus.BACKLOG, gate_type="deferred"),
            Task(id="4", title="done", status=TaskStatus.DONE, gate_type=None),
            Task(id="5", title="canceled", status=TaskStatus.CANCELED, gate_type="human"),
        ]

    def test_counts_open_only(self) -> None:
        counts = _wq_gate_counts(self._tasks())
        self.assertEqual(counts[""], 1)       # only open ungated
        self.assertEqual(counts["human"], 1)  # only open human-gated
        self.assertEqual(counts["deferred"], 1)

    def test_done_and_canceled_excluded(self) -> None:
        counts = _wq_gate_counts(self._tasks())
        total = sum(counts.values())
        self.assertEqual(total, 3)  # 5 tasks minus done and canceled


class WqColumnCountsGateFilterTest(unittest.TestCase):
    def _tasks(self) -> list:
        return [
            Task(id="1", title="r1", status=TaskStatus.BACKLOG, gate_type=None),
            Task(id="2", title="r2", status=TaskStatus.BACKLOG, gate_type="human"),
            Task(id="3", title="r3", status=TaskStatus.BACKLOG, gate_type="deferred"),
            Task(id="4", title="ip", status=TaskStatus.IN_PROGRESS, gate_type=None),
        ]

    def test_ungated_filter(self) -> None:
        counts = _wq_column_counts(self._tasks(), gate_type="")
        self.assertEqual(counts[TaskStatus.BACKLOG], 1)
        self.assertEqual(counts[TaskStatus.IN_PROGRESS], 1)

    def test_human_filter(self) -> None:
        counts = _wq_column_counts(self._tasks(), gate_type="human")
        self.assertEqual(counts[TaskStatus.BACKLOG], 1)
        self.assertEqual(counts[TaskStatus.IN_PROGRESS], 0)

    def test_deferred_filter(self) -> None:
        counts = _wq_column_counts(self._tasks(), gate_type="deferred")
        self.assertEqual(counts[TaskStatus.BACKLOG], 1)
        self.assertEqual(counts[TaskStatus.IN_PROGRESS], 0)

    def test_no_gate_filter_counts_all(self) -> None:
        counts = _wq_column_counts(self._tasks(), gate_type=None)
        self.assertEqual(counts[TaskStatus.BACKLOG], 3)
        self.assertEqual(counts[TaskStatus.IN_PROGRESS], 1)


class GateChipsRenderTest(unittest.TestCase):
    def _tasks(self) -> list:
        return [
            Task(id="1", title="ready", status=TaskStatus.BACKLOG, gate_type=None),
            Task(id="2", title="for-you", status=TaskStatus.BACKLOG, gate_type="human"),
            Task(id="3", title="deferred", status=TaskStatus.BACKLOG, gate_type="deferred"),
        ]

    def _render(self, gate: str = "") -> str:
        return _render_work_queue_filters(
            list_path="/admin/tickets/all",
            current_view="board",
            status="",
            label="",
            priority=None,
            gate=gate,
            merged_scope_tasks=self._tasks(),
        )

    def test_gate_row_rendered(self) -> None:
        html = self._render()
        self.assertIn("wq-gate-row", html)
        self.assertIn("Ready", html)
        self.assertIn("For You", html)
        self.assertIn("Deferred", html)

    def test_all_open_chip_active_by_default(self) -> None:
        html = self._render(gate="")
        # First chip (All open) should be active
        self.assertIn("wq-gate-chip--active", html)

    def test_deferred_chip_active_when_filter_set(self) -> None:
        html = self._render(gate="deferred")
        # "Deferred" chip active; others not
        idx_deferred = html.find("Deferred")
        idx_active = html.rfind("wq-gate-chip--active", 0, idx_deferred)
        self.assertGreater(idx_deferred, 0)
        self.assertGreater(idx_active, 0)

    def test_gate_chips_link_include_gate_param(self) -> None:
        html = self._render()
        self.assertIn("gate=human", html)
        self.assertIn("gate=deferred", html)
        self.assertIn("gate=none", html)


class ApiGateFilterTest(unittest.TestCase):
    def setUp(self) -> None:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from worklane.task_server import router
        from worklane.trackers.sqlite import SQLiteTracker

        self._tmp = tempfile.TemporaryDirectory(prefix="wl_gate_api_")
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

    def test_gate_filter_deferred_excludes_others(self) -> None:
        t_free = self.tracker.create_task(title="Free ticket")
        t_human = self.tracker.create_task(title="Human gated")
        t_def = self.tracker.create_task(title="Deferred")
        self.tracker.update_task(t_human.id, gate_type="human", actor="wl-pool")
        self.tracker.update_task(t_def.id, gate_type="deferred", actor="wl-pool")

        r = self.client.get("/api/admin/tasks", params={"gate": "deferred", "product": "tradeos"})
        self.assertEqual(r.status_code, 200)
        ids = {task["id"] for task in r.json()["tasks"]}
        self.assertIn(f"t-{t_def.id}", ids)
        self.assertNotIn(f"t-{t_free.id}", ids)
        self.assertNotIn(f"t-{t_human.id}", ids)

    def test_gate_filter_human_excludes_others(self) -> None:
        t_free = self.tracker.create_task(title="Free ticket")
        t_human = self.tracker.create_task(title="Human gated")
        t_def = self.tracker.create_task(title="Deferred")
        self.tracker.update_task(t_human.id, gate_type="human", actor="wl-pool")
        self.tracker.update_task(t_def.id, gate_type="deferred", actor="wl-pool")

        r = self.client.get("/api/admin/tasks", params={"gate": "human", "product": "tradeos"})
        self.assertEqual(r.status_code, 200)
        ids = {task["id"] for task in r.json()["tasks"]}
        self.assertIn(f"t-{t_human.id}", ids)
        self.assertNotIn(f"t-{t_free.id}", ids)
        self.assertNotIn(f"t-{t_def.id}", ids)

    def test_gate_filter_none_shows_only_ungated(self) -> None:
        t_free = self.tracker.create_task(title="Free ticket")
        t_human = self.tracker.create_task(title="Human gated")
        self.tracker.update_task(t_human.id, gate_type="human", actor="wl-pool")

        r = self.client.get("/api/admin/tasks", params={"gate": "none", "product": "tradeos"})
        self.assertEqual(r.status_code, 200)
        ids = {task["id"] for task in r.json()["tasks"]}
        self.assertIn(f"t-{t_free.id}", ids)
        self.assertNotIn(f"t-{t_human.id}", ids)

    def test_no_gate_filter_shows_all(self) -> None:
        t_free = self.tracker.create_task(title="Free ticket")
        t_def = self.tracker.create_task(title="Deferred")
        self.tracker.update_task(t_def.id, gate_type="deferred", actor="wl-pool")

        r = self.client.get("/api/admin/tasks", params={"product": "tradeos"})
        self.assertEqual(r.status_code, 200)
        ids = {task["id"] for task in r.json()["tasks"]}
        self.assertIn(f"t-{t_free.id}", ids)
        self.assertIn(f"t-{t_def.id}", ids)


if __name__ == "__main__":
    unittest.main()
