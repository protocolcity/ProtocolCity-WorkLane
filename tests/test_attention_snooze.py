"""wl-251: per-ticket attention snooze — task scope set/match/expiry/unsnooze/coexist."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from worklane.trackers.sqlite import SQLiteTracker


class AttentionSnoozeTaskScopeTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wl_attention_snooze_")
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

        self.tracker = SQLiteTracker(db_path=self.root / "data" / "tradeos.db")
        t = self.tracker.create_task(title="tabled item", description="gated by human")
        self.tracker.update_task(t.id, gate_type="human")
        self.task_composite_id = f"t-{t.id}"
        self.prefs_path = self.root / "data" / "attention_prefs.json"

        from worklane.task_server import router
        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)

        import worklane.api.scene as _scene_api
        _scene_api._scene_cache_ts = 0.0
        _scene_api._scene_cache_payload = None

    def tearDown(self) -> None:
        for k, v in self._env_before.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    def _visible_ids(self) -> list:
        return [it["id"] for it in self.client.get("/api/dev/attention").json()["items"]]

    def test_task_snooze_set(self) -> None:
        r = self.client.post("/api/dev/attention/snooze", json={
            "task_id": self.task_composite_id,
            "until": "1d",
            "reason": "tabled",
        })
        self.assertEqual(r.status_code, 200)
        j = r.json()
        self.assertTrue(j["ok"])
        snooze = j["snooze"]
        self.assertEqual(snooze["scope"], "task")
        self.assertEqual(snooze["task_id"], self.task_composite_id)
        self.assertEqual(snooze["reason"], "tabled")
        self.assertTrue(any(s.get("task_id") == self.task_composite_id for s in j["snoozes"]))

    def test_task_snooze_mutes_item(self) -> None:
        self.assertIn(self.task_composite_id, self._visible_ids())
        self.client.post("/api/dev/attention/snooze", json={
            "task_id": self.task_composite_id,
            "until": "1d",
        })
        j = self.client.get("/api/dev/attention").json()
        self.assertNotIn(self.task_composite_id, [it["id"] for it in j["items"]])
        self.assertEqual(j["snoozed_count"], 1)

    def test_task_snooze_visible_with_include_snoozed(self) -> None:
        self.client.post("/api/dev/attention/snooze", json={
            "task_id": self.task_composite_id,
            "until": "1d",
        })
        j = self.client.get("/api/dev/attention?include_snoozed=1").json()
        self.assertIn(self.task_composite_id, [it["id"] for it in j["items"]])
        self.assertEqual(j["snoozed_count"], 1)

    def test_task_snooze_expiry_restores_item(self) -> None:
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        prefs = {"snoozes": [{
            "scope": "task",
            "task_id": self.task_composite_id,
            "product": "",
            "kind": "",
            "until": past,
            "reason": "expired",
            "created_at": past,
        }]}
        self.prefs_path.write_text(json.dumps(prefs) + "\n", encoding="utf-8")
        # expired entry is pruned; item returns to visible feed
        self.assertIn(self.task_composite_id, self._visible_ids())

    def test_task_unsnooze_restores_item(self) -> None:
        self.client.post("/api/dev/attention/snooze", json={
            "task_id": self.task_composite_id,
            "until": "1d",
        })
        self.assertNotIn(self.task_composite_id, self._visible_ids())

        r = self.client.post("/api/dev/attention/unsnooze", json={
            "task_id": self.task_composite_id,
        })
        self.assertTrue(r.json()["ok"])
        self.assertIn(self.task_composite_id, self._visible_ids())

    def test_product_and_task_snooze_coexist(self) -> None:
        # Second human-gated task in the same store.
        t2 = self.tracker.create_task(title="loud item", description="stays visible")
        self.tracker.update_task(t2.id, gate_type="human")
        t2_composite = f"t-{t2.id}"

        # Task snooze mutes only the first ticket; second stays visible.
        self.client.post("/api/dev/attention/snooze", json={
            "task_id": self.task_composite_id,
            "until": "1d",
        })
        ids = self._visible_ids()
        self.assertNotIn(self.task_composite_id, ids)
        self.assertIn(t2_composite, ids)

        # Add a product-level snooze on top; now t2 is muted too.
        self.client.post("/api/dev/attention/snooze", json={
            "product": "tradeos",
            "until": "1d",
        })
        j = self.client.get("/api/dev/attention").json()
        self.assertEqual(j["snoozed_count"], 2)
        self.assertEqual(len(j["items"]), 0)
        scopes = {s["scope"] for s in j["snoozes"]}
        self.assertIn("task", scopes)
        self.assertIn("product", scopes)
