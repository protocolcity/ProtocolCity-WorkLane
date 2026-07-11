"""wl-77: lane pulse — per dispatch-lane last-activity + throughput +
workable backlog, surfaced via GET /api/admin/agents/pulse and the Pool
header strip.
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

from worklane.trackers.protocol import TaskStatus
from worklane.trackers.sqlite import SQLiteTracker


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


class LanePulseTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wl_lane_pulse_")
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
            )
        }
        os.environ["WORKLANE_RUNTIME_DIR"] = str(self.root)
        os.environ["WORKLANE_DB"] = str(self.db_path)
        os.environ.pop("TRADEOS_TRACKER_DB", None)
        os.environ["TRADEOS_TICKETS_SOURCE"] = "sqlite"

        self.tracker = SQLiteTracker(db_path=self.db_path)

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

    def _comment(self, task_id: str, body: str, author: str, *, age_hours: float = 0.0):
        c = self.tracker.add_comment(task_id, body, author=author)
        if age_hours:
            self._backdate_comment(c.id, age_hours)
        return c

    def _backdate_comment(self, comment_id: str, age_hours: float) -> None:
        conn = sqlite3.connect(str(self.db_path))
        try:
            dt = datetime.now(timezone.utc) - timedelta(hours=age_hours)
            conn.execute(
                "UPDATE task_comments SET created_at = ? WHERE id = ?",
                (_iso(dt), int(comment_id)),
            )
            conn.commit()
        finally:
            conn.close()

    def _claim(self, task_id: str, author: str, *, age_hours: float = 0.0):
        self.tracker.update_status(task_id, TaskStatus.IN_PROGRESS)
        body = f"Owner: {author}\nWorkdir: /tmp\nStart: 2026-01-01T00:00:00Z\nPlan:\n- x"
        return self._comment(task_id, body, author, age_hours=age_hours)

    def _close(self, task_id: str, author: str, *, age_hours: float = 0.0):
        self.tracker.update_status(task_id, TaskStatus.DONE)
        body = "Completed: x\nVerification: y\nLinks: z\nFollow-ups: none"
        return self._comment(task_id, body, author, age_hours=age_hours)

    def test_pulse_math_claim_and_close(self) -> None:
        t = self.tracker.create_task(title="grok work", description="x")
        self._claim(t.id, "grok", age_hours=2)
        self._close(t.id, "grok", age_hours=1)

        stats = {s["id"]: s for s in self.client.get("/api/admin/agents/pulse").json()["agents"]}
        grok = stats["grok"]
        self.assertIsNotNone(grok["last_claim_at"])
        self.assertIsNotNone(grok["last_close_at"])
        self.assertEqual(grok["closes_7d"], 1)
        self.assertEqual(grok["closes_today"], 1)
        # last_comment_at is the newest of the two (the close, 1h ago)
        self.assertEqual(grok["last_comment_at"], grok["last_close_at"])

    def test_closes_today_excludes_older_than_today(self) -> None:
        t1 = self.tracker.create_task(title="a", description="x")
        t2 = self.tracker.create_task(title="b", description="x")
        self._claim(t1.id, "codex")
        self._close(t1.id, "codex")  # today
        self._claim(t2.id, "codex", age_hours=30)
        self._close(t2.id, "codex", age_hours=30)  # yesterday, still within 7d

        stats = {s["id"]: s for s in self.client.get("/api/admin/agents/pulse").json()["agents"]}
        codex = stats["codex"]
        self.assertEqual(codex["closes_7d"], 2)
        self.assertEqual(codex["closes_today"], 1)

    def test_non_lane_authors_excluded(self) -> None:
        t = self.tracker.create_task(title="x", description="x")
        self._claim(t.id, "founder-terminal")
        self._close(t.id, "founder-terminal")

        agents = self.client.get("/api/admin/agents/pulse").json()["agents"]
        ids = {a["id"] for a in agents}
        self.assertNotIn("founder-terminal", ids)
        self.assertEqual(ids, {"claude-tradeos", "claude-worklane", "cursor", "grok", "codex"})

    def test_workable_backlog_excludes_gated(self) -> None:
        open_t = self.tracker.create_task(
            title="open", description="x", labels=["lane:grok"]
        )
        gated_t = self.tracker.create_task(
            title="gated", description="x", labels=["lane:grok"]
        )
        self.tracker.update_task(gated_t.id, gate_type="human", actor="founder-terminal")

        stats = {s["id"]: s for s in self.client.get("/api/admin/agents/pulse").json()["agents"]}
        self.assertEqual(stats["grok"]["workable_backlog"], 1)

    def test_default_lane_agents_have_no_backlog_metric(self) -> None:
        stats = {s["id"]: s for s in self.client.get("/api/admin/agents/pulse").json()["agents"]}
        self.assertIsNone(stats["claude-tradeos"]["workable_backlog"])
        self.assertIsNone(stats["claude-tradeos"]["stale"])
        self.assertIsNone(stats["claude-worklane"]["workable_backlog"])

    def test_stale_flag_fires_on_starved_lane(self) -> None:
        self.tracker.create_task(title="starved", description="x", labels=["lane:grok"])
        t = self.tracker.create_task(title="claimed once", description="x")
        self._claim(t.id, "grok", age_hours=5)  # last claim 5h ago, > 3h threshold

        stats = {s["id"]: s for s in self.client.get("/api/admin/agents/pulse").json()["agents"]}
        self.assertTrue(stats["grok"]["stale"])

    def test_no_stale_flag_when_backlog_empty(self) -> None:
        # No lane:grok backlog tickets at all — idle-because-empty is healthy,
        # not stale, even with no recent claim.
        stats = {s["id"]: s for s in self.client.get("/api/admin/agents/pulse").json()["agents"]}
        self.assertEqual(stats["grok"]["workable_backlog"], 0)
        self.assertFalse(stats["grok"]["stale"])

    def test_no_stale_flag_when_claim_recent(self) -> None:
        self.tracker.create_task(title="fresh", description="x", labels=["lane:grok"])
        t = self.tracker.create_task(title="claimed", description="x")
        self._claim(t.id, "grok", age_hours=0.1)

        stats = {s["id"]: s for s in self.client.get("/api/admin/agents/pulse").json()["agents"]}
        self.assertFalse(stats["grok"]["stale"])

    def test_board_renders_lane_pulse_strip(self) -> None:
        r = self.client.get("/admin/tickets/all?view=board")
        self.assertEqual(r.status_code, 200)
        self.assertIn("lane-pulse-strip", r.text)
        self.assertIn("lane-pulse-id'>grok<", r.text)

        r_scoped = self.client.get("/admin/tickets/tradeos?view=board")
        self.assertEqual(r_scoped.status_code, 200)
        self.assertIn("lane-pulse-strip", r_scoped.text)

        r_table = self.client.get("/admin/tickets/all?view=table")
        self.assertEqual(r_table.status_code, 200)
        self.assertIn("lane-pulse-strip", r_table.text)


if __name__ == "__main__":
    unittest.main()
