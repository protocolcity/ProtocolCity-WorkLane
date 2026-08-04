"""wl-101 + wl-348: poll-cursor change feed and SSE store-stamped payloads.

Event ids are the task_events table's own autoincrement, so the cursor
is durable across restarts with no separate cursor-store to keep in
sync — a fresh SQLiteTracker instance against the same db_path (the
in-test stand-in for a server restart) must not replay or drop events.

wl-348: every event carries ``store`` + composite ``task_id``; unscoped
stream aggregates all registered stores (never silent single-default).
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from worklane.trackers.protocol import TaskStatus
from worklane.trackers.sqlite import SQLiteTracker


def _make_env(tmp: Path) -> None:
    (tmp / "data").mkdir(parents=True, exist_ok=True)
    os.environ["WORKLANE_RUNTIME_DIR"] = str(tmp)
    os.environ["WORKLANE_DB"] = str(tmp / "data" / "tradeos.db")
    os.environ.pop("TRADEOS_TRACKER_DB", None)
    os.environ["TRADEOS_TICKETS_SOURCE"] = "sqlite"
    os.environ["WL_DEFAULT_PRODUCT"] = "tradeos"
    os.environ.pop("WL_DEFAULT_PROJECT", None)
    os.environ.pop("WL_PRODUCT", None)
    os.environ.pop("WL_PROJECT", None)


class EventsFeedTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wl_events_")
        self.root = Path(self._tmp.name)
        self._env_before = {
            k: os.environ.get(k)
            for k in (
                "WORKLANE_RUNTIME_DIR",
                "WORKLANE_DB",
                "TRADEOS_TRACKER_DB",
                "TRADEOS_TICKETS_SOURCE",
                "WL_DEFAULT_PRODUCT",
                "WL_DEFAULT_PROJECT",
                "WL_PRODUCT",
                "WL_PROJECT",
            )
        }
        _make_env(self.root)
        self.db_path = self.root / "data" / "tradeos.db"
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
        # wl-348: composite task_id + store slug
        self.assertEqual(ev["task_id"], "t-%s" % t.id)
        self.assertEqual(ev["store"], "tradeos")
        self.assertEqual(j["cursor"], ev["id"])

    def test_label_change_appears_in_feed(self) -> None:
        t = self.tracker.create_task(title="lane target", description="x")
        r0 = self.client.get("/api/events")
        cursor = r0.json()["cursor"]

        self.tracker.update_labels(t.id, add=["lane:grok"])

        r1 = self.client.get("/api/events?since=%s" % cursor)
        j = r1.json()
        self.assertEqual(len(j["events"]), 1)
        ev = j["events"][0]
        self.assertEqual(ev["event_type"], "labels_changed")
        self.assertIn("lane:grok", ev["labels"])
        self.assertEqual(ev["store"], "tradeos")
        self.assertEqual(ev["task_id"], "t-%s" % t.id)

    def test_status_change_appears_and_since_excludes_seen(self) -> None:
        t = self.tracker.create_task(title="status target", description="x")
        r0 = self.client.get("/api/events")
        cursor = r0.json()["cursor"]

        self.tracker.update_status(t.id, TaskStatus.IN_PROGRESS)

        r1 = self.client.get("/api/events?since=%s" % cursor)
        j = r1.json()
        self.assertEqual(len(j["events"]), 1)
        self.assertEqual(j["events"][0]["event_type"], "status_change")
        self.assertEqual(j["events"][0]["status"], TaskStatus.IN_PROGRESS)
        self.assertEqual(j["events"][0]["store"], "tradeos")

        # since= the new cursor: nothing left to replay
        r2 = self.client.get("/api/events?since=%s" % j["cursor"])
        self.assertEqual(r2.json()["events"], [])

    def test_cursor_survives_restart_no_replay_no_drop(self) -> None:
        t = self.tracker.create_task(title="durable", description="x")
        r0 = self.client.get("/api/events")
        cursor = r0.json()["cursor"]

        # Simulate a server restart: fresh tracker instance, same db_path.
        restarted = SQLiteTracker(db_path=self.db_path)
        restarted.update_labels(t.id, add=["lane:grok"])

        r1 = self.client.get("/api/events?since=%s" % cursor)
        j = r1.json()
        self.assertEqual(len(j["events"]), 1)
        self.assertEqual(j["events"][0]["event_type"], "labels_changed")

        # Nothing dropped either: replaying from 0 still returns both.
        r2 = self.client.get("/api/events?since=0")
        self.assertEqual(len(r2.json()["events"]), 2)

    def test_scoped_project_stamps_composite(self) -> None:
        """project=worklane stamps wl- composite + store=worklane."""
        wl_db = self.root / "data" / "worklane.db"
        wl = SQLiteTracker(db_path=wl_db)
        t = wl.create_task(title="wl only", description="x")
        wl.update_status(t.id, TaskStatus.IN_PROGRESS)

        r = self.client.get("/api/events?project=worklane")
        self.assertEqual(r.status_code, 200)
        events = r.json()["events"]
        self.assertTrue(events, "expected events from worklane store")
        for ev in events:
            self.assertEqual(ev["store"], "worklane")
            self.assertTrue(
                str(ev["task_id"]).startswith("wl-"),
                "composite task_id, got %r" % (ev["task_id"],),
            )
        # tradeos events must not leak into scoped worklane feed
        tradeos_t = self.tracker.create_task(title="tradeos only", description="x")
        self.tracker.update_status(tradeos_t.id, TaskStatus.IN_PROGRESS)
        r2 = self.client.get("/api/events?project=worklane")
        for ev in r2.json()["events"]:
            self.assertEqual(ev["store"], "worklane")
            self.assertNotEqual(ev["task_id"], "t-%s" % tradeos_t.id)

    def test_unscoped_aggregates_all_stores(self) -> None:
        """Unscoped /api/events merges every registered store (wl-348)."""
        wl = SQLiteTracker(db_path=self.root / "data" / "worklane.db")
        t_trade = self.tracker.create_task(title="t side", description="x")
        t_wl = wl.create_task(title="wl side", description="x")
        self.tracker.update_status(t_trade.id, TaskStatus.IN_PROGRESS)
        wl.update_status(t_wl.id, TaskStatus.IN_PROGRESS)

        r = self.client.get("/api/events?since=0")
        self.assertEqual(r.status_code, 200)
        events = r.json()["events"]
        stores = {ev["store"] for ev in events}
        self.assertIn("tradeos", stores)
        self.assertIn("worklane", stores)
        task_ids = {ev["task_id"] for ev in events}
        self.assertIn("t-%s" % t_trade.id, task_ids)
        self.assertIn("wl-%s" % t_wl.id, task_ids)

    def test_unknown_project_returns_empty_not_default(self) -> None:
        """Unknown project= must not silently fall back to tradeos (wl-348)."""
        self.tracker.create_task(title="should not appear", description="x")
        r = self.client.get("/api/events?project=does-not-exist")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["events"], [])

    def test_stamp_event_composite_and_store(self) -> None:
        """_stamp_event is the SSE/poll payload contract (wl-348)."""
        from worklane.api.events_stream import _stamp_event
        from worklane.products import get_product

        # Ensure worklane store exists for discovery.
        SQLiteTracker(db_path=self.root / "data" / "worklane.db").create_task(
            title="seed"
        )
        spec = get_product("worklane")
        self.assertIsNotNone(spec)
        assert spec is not None
        stamped = _stamp_event(
            {
                "id": 9,
                "task_id": "12",
                "event_type": "status_change",
                "status": "in_progress",
                "labels": ["worker:lili"],
                "created_at": "2026-08-03T00:00:00+00:00",
            },
            spec,
        )
        self.assertEqual(stamped["store"], "worklane")
        self.assertEqual(stamped["task_id"], "wl-12")
        self.assertEqual(stamped["id"], 9)
        self.assertEqual(stamped["event_type"], "status_change")
        # Already-prefixed ids are not double-prefixed.
        stamped2 = _stamp_event({"id": 1, "task_id": "wl-12"}, spec)
        self.assertEqual(stamped2["task_id"], "wl-12")

    def test_stream_sources_scoped_unscoped_unknown(self) -> None:
        """Unscoped aggregates; unknown project is empty (no silent default)."""
        from worklane.api.events_stream import _stream_sources

        SQLiteTracker(db_path=self.root / "data" / "worklane.db").create_task(
            title="seed"
        )
        unscoped_slugs = [s.slug for s, _ in _stream_sources("")]
        self.assertIn("tradeos", unscoped_slugs)
        self.assertIn("worklane", unscoped_slugs)

        scoped = _stream_sources("worklane")
        self.assertEqual(len(scoped), 1)
        self.assertEqual(scoped[0][0].slug, "worklane")

        self.assertEqual(_stream_sources("does-not-exist"), [])

    def test_sse_stream_one_cycle_payload(self) -> None:
        """Drive one SSE generator cycle via helpers (no open-stream hang)."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock, patch

        from worklane.api import events_stream as es

        t = self.tracker.create_task(title="sse target", description="x")
        self.tracker.update_status(t.id, TaskStatus.IN_PROGRESS)

        request = MagicMock()
        # First check False so we enter the loop; second True so we exit after one sleep.
        request.is_disconnected = AsyncMock(side_effect=[False, True])

        frames: list = []

        async def _collect() -> None:
            # Patch sleep so the cycle returns immediately.
            with patch.object(es.asyncio, "sleep", new=AsyncMock()):
                resp = await es.api_events_stream(
                    request, project="tradeos", since=0, interval=0.5
                )
                async for chunk in resp.body_iterator:
                    if isinstance(chunk, bytes):
                        frames.append(chunk.decode("utf-8"))
                    else:
                        frames.append(str(chunk))

        asyncio.run(_collect())
        joined = "".join(frames)
        self.assertIn("retry:", joined)
        data_lines = [
            line[5:].strip()
            for line in joined.split("\n")
            if line.startswith("data:")
        ]
        self.assertTrue(data_lines, "expected data frames; got %r" % joined)
        payload = json.loads(data_lines[0])
        self.assertEqual(payload["store"], "tradeos")
        self.assertEqual(payload["task_id"], "t-%s" % t.id)
        self.assertEqual(payload["event_type"], "status_change")

    def test_sse_stream_unscoped_one_cycle_multi_store(self) -> None:
        """Unscoped SSE one cycle emits both stores stamped (wl-348)."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock, patch

        from worklane.api import events_stream as es

        wl = SQLiteTracker(db_path=self.root / "data" / "worklane.db")
        t_trade = self.tracker.create_task(title="sse trade", description="x")
        t_wl = wl.create_task(title="sse wl", description="x")
        self.tracker.update_status(t_trade.id, TaskStatus.IN_PROGRESS)
        wl.update_status(t_wl.id, TaskStatus.IN_PROGRESS)

        request = MagicMock()
        request.is_disconnected = AsyncMock(side_effect=[False, True])
        frames: list = []

        async def _collect() -> None:
            with patch.object(es.asyncio, "sleep", new=AsyncMock()):
                resp = await es.api_events_stream(
                    request, project="", since=0, interval=0.5
                )
                async for chunk in resp.body_iterator:
                    if isinstance(chunk, bytes):
                        frames.append(chunk.decode("utf-8"))
                    else:
                        frames.append(str(chunk))

        asyncio.run(_collect())
        joined = "".join(frames)
        payloads = []
        for line in joined.split("\n"):
            if line.startswith("data:"):
                payloads.append(json.loads(line[5:].strip()))
        stores = {p["store"] for p in payloads}
        self.assertIn("tradeos", stores)
        self.assertIn("worklane", stores)
        task_ids = {p["task_id"] for p in payloads}
        self.assertIn("t-%s" % t_trade.id, task_ids)
        self.assertIn("wl-%s" % t_wl.id, task_ids)

    def test_dev_activity_stamps_composite_and_store(self) -> None:
        """/api/dev/activity matches wl-348 wire contract (wl-387).

        Non-default store entries must carry composite task_id + store slug
        for both comment rows and inferred status_change (sc-*) rows — the
        Map comment-theater poller keys dig-in on these fields.
        """
        career_db = self.root / "data" / "career.db"
        career = SQLiteTracker(db_path=career_db)
        t = career.create_task(title="career activity", description="x")
        career.add_comment(t.id, body="label note", author="lili")
        career.update_status(t.id, TaskStatus.IN_PROGRESS)

        r = self.client.get("/api/dev/activity?project=career&limit=50")
        self.assertEqual(r.status_code, 200)
        entries = r.json()["entries"]
        self.assertTrue(entries, "expected activity entries from career store")

        comments = [e for e in entries if e.get("entry_type") == "comment"]
        statuses = [e for e in entries if e.get("entry_type") == "status_change"]
        self.assertTrue(comments, "expected comment entry")
        self.assertTrue(statuses, "expected status_change entry")

        expected_tid = "career-%s" % t.id
        for e in comments + statuses:
            self.assertEqual(
                e.get("store"),
                "career",
                "store slug missing/wrong on %r" % (e,),
            )
            self.assertEqual(
                e.get("task_id"),
                expected_tid,
                "composite task_id missing/wrong on %r" % (e,),
            )
            # Must not emit bare internal numeric ids (the Map dig-in 404 bug).
            self.assertFalse(
                str(e.get("task_id", "")).isdigit(),
                "bare numeric task_id still present: %r" % (e,),
            )

        # status synthetic id stays store-local sc-<rowid>; task_id is composite.
        sc = statuses[0]
        self.assertEqual(sc["id"], "sc-%s" % t.id)
        self.assertEqual(sc["task_id"], expected_tid)

        # Default-store activity also stamps (tradeos prefix t-).
        t_def = self.tracker.create_task(title="default activity", description="x")
        self.tracker.add_comment(t_def.id, body="on default", author="lili")
        r2 = self.client.get("/api/dev/activity?limit=50")
        self.assertEqual(r2.status_code, 200)
        default_comments = [
            e
            for e in r2.json()["entries"]
            if e.get("entry_type") == "comment" and e.get("task_id") == "t-%s" % t_def.id
        ]
        self.assertTrue(default_comments)
        self.assertEqual(default_comments[0]["store"], "tradeos")


if __name__ == "__main__":
    unittest.main()
