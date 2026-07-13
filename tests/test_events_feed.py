"""wl-101: poll-cursor change feed — GET /api/events?since=<cursor>.

Event ids are the task_events table's own autoincrement, so the cursor
is durable across restarts with no separate cursor-store to keep in
sync — a fresh SQLiteTracker instance against the same db_path (the
in-test stand-in for a server restart) must not replay or drop events.
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


class EventsFeedTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wl_events_")
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

    def test_create_emits_event(self) -> None:
        t = self.tracker.create_task(title="feed me", description="x")
        r = self.client.get("/api/events")
        self.assertEqual(r.status_code, 200)
        j = r.json()
        self.assertEqual(len(j["events"]), 1)
        ev = j["events"][0]
        self.assertEqual(ev["event_type"], "created")
        self.assertEqual(ev["task_id"], str(t.id))
        self.assertEqual(j["cursor"], ev["id"])

    def test_label_change_appears_in_feed(self) -> None:
        t = self.tracker.create_task(title="lane target", description="x")
        r0 = self.client.get("/api/events")
        cursor = r0.json()["cursor"]

        self.tracker.update_labels(t.id, add=["lane:grok"])

        r1 = self.client.get(f"/api/events?since={cursor}")
        j = r1.json()
        self.assertEqual(len(j["events"]), 1)
        ev = j["events"][0]
        self.assertEqual(ev["event_type"], "labels_changed")
        self.assertIn("lane:grok", ev["labels"])

    def test_status_change_appears_and_since_excludes_seen(self) -> None:
        t = self.tracker.create_task(title="status target", description="x")
        r0 = self.client.get("/api/events")
        cursor = r0.json()["cursor"]

        self.tracker.update_status(t.id, TaskStatus.IN_PROGRESS)

        r1 = self.client.get(f"/api/events?since={cursor}")
        j = r1.json()
        self.assertEqual(len(j["events"]), 1)
        self.assertEqual(j["events"][0]["event_type"], "status_change")
        self.assertEqual(j["events"][0]["status"], TaskStatus.IN_PROGRESS)

        # since= the new cursor: nothing left to replay
        r2 = self.client.get(f"/api/events?since={j['cursor']}")
        self.assertEqual(r2.json()["events"], [])

    def test_cursor_survives_restart_no_replay_no_drop(self) -> None:
        t = self.tracker.create_task(title="durable", description="x")
        r0 = self.client.get("/api/events")
        cursor = r0.json()["cursor"]

        # Simulate a server restart: fresh tracker instance, same db_path.
        restarted = SQLiteTracker(db_path=self.db_path)
        restarted.update_labels(t.id, add=["lane:grok"])

        r1 = self.client.get(f"/api/events?since={cursor}")
        j = r1.json()
        self.assertEqual(len(j["events"]), 1)
        self.assertEqual(j["events"][0]["event_type"], "labels_changed")

        # Nothing dropped either: replaying from 0 still returns both.
        r2 = self.client.get("/api/events?since=0")
        self.assertEqual(len(r2.json()["events"]), 2)


if __name__ == "__main__":
    unittest.main()
