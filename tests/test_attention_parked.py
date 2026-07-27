"""wl-257: parked human gates withhold ready but not For You gold."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from worklane.server_helpers import _human_gate_is_parked
from worklane.trackers.sqlite import SQLiteTracker


class ParkedHumanGateAttentionTest(unittest.TestCase):
    def test_helper_markers(self) -> None:
        self.assertTrue(_human_gate_is_parked("deferred:post-northstar — thaw later"))
        self.assertTrue(_human_gate_is_parked("umbrella — not claimable; withheld from ready"))
        self.assertTrue(_human_gate_is_parked("parked: after ship"))
        self.assertFalse(_human_gate_is_parked("Need You to pick brand name"))
        self.assertFalse(_human_gate_is_parked(""))
        self.assertFalse(_human_gate_is_parked(None))

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wl_att_parked_")
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

    def test_parked_human_gate_not_in_attention(self) -> None:
        act = self.tracker.create_task(title="need decision", description="pick A or B")
        self.tracker.update_task(act.id, gate_type="human", gate_note="Need You to pick A or B")
        park = self.tracker.create_task(title="later epic", description="post northstar")
        self.tracker.update_task(
            park.id,
            gate_type="human",
            gate_note="deferred:post-northstar — withheld from ready until north-star work clears.",
        )
        umb = self.tracker.create_task(title="umbrella epic", description="phases")
        self.tracker.update_task(
            umb.id,
            gate_type="human",
            gate_note="umbrella — not claimable; withheld from ready so default-lane metrics drain.",
        )
        j = self.client.get("/api/dev/attention").json()
        ids = [it["id"] for it in j["items"]]
        self.assertIn(f"t-{act.id}", ids)
        self.assertNotIn(f"t-{park.id}", ids)
        self.assertNotIn(f"t-{umb.id}", ids)
        # Ready still excludes all three human gates
        ready = self.client.get("/api/admin/tasks/ready?product=tradeos").json()
        ready_ids = [t["id"] for t in ready.get("tasks") or []]
        self.assertNotIn(f"t-{act.id}", ready_ids)
        self.assertNotIn(f"t-{park.id}", ready_ids)


if __name__ == "__main__":
    unittest.main()
