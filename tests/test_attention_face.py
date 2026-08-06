"""wl-405: attention face (decide|read|watch) + band counts on /api/dev/attention."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from worklane.server_helpers import (
    _attention_band_counts,
    _derive_attention_face,
    _is_inbox_report,
)
from worklane.trackers.sqlite import SQLiteTracker


class AttentionFaceHelpersTest(unittest.TestCase):
    def test_is_inbox_report_labels(self) -> None:
        self.assertTrue(_is_inbox_report(["inbox-report"]))
        self.assertTrue(_is_inbox_report(["report", "inbox-report:tradeos:desk-brief:2026-08-06"]))
        self.assertFalse(_is_inbox_report(["report", "worker:you"]))
        self.assertFalse(_is_inbox_report([]))
        self.assertFalse(_is_inbox_report(None))

    def test_derive_face_matrix(self) -> None:
        self.assertEqual(
            _derive_attention_face("human_gate", ["inbox-report:tradeos:desk-brief:x"]),
            "read",
        )
        self.assertEqual(_derive_attention_face("human_gate", ["FOUNDER", "publish"]), "decide")
        self.assertEqual(_derive_attention_face("founder_decision", ["needs:founder-decision"]), "decide")
        self.assertEqual(_derive_attention_face("stalled", []), "watch")
        self.assertEqual(_derive_attention_face("embargo", []), "watch")
        # Read wins over kind even if kind were watch-shaped (labels are source of truth for reports)
        self.assertEqual(_derive_attention_face("stalled", ["inbox-report"]), "read")

    def test_band_counts_sum(self) -> None:
        items = [
            {"kind": "human_gate", "face": "decide"},
            {"kind": "human_gate", "face": "read"},
            {"kind": "stalled", "face": "watch"},
            {"kind": "embargo", "face": "watch"},
        ]
        bands = _attention_band_counts(items)
        self.assertEqual(bands["act_now_count"], 1)
        self.assertEqual(bands["read_count"], 1)
        self.assertEqual(bands["watch_count"], 2)
        self.assertEqual(
            bands["act_now_count"] + bands["read_count"] + bands["watch_count"],
            len(items),
        )


class AttentionFaceApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wl_att_face_")
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
        import worklane.api.tasks as _tasks_api
        _tasks_api._invalidate_attention_cache()

    def tearDown(self) -> None:
        for k, v in self._env_before.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    def test_payload_face_and_band_counts(self) -> None:
        decide = self.tracker.create_task(
            title="FOUNDER · pick brand",
            description="act now",
            labels=["FOUNDER", "worker:you"],
        )
        self.tracker.update_task(
            decide.id, gate_type="human", gate_note="Need You to pick brand name"
        )
        report = self.tracker.create_task(
            title="FOUNDER · Desk brief",
            description="inbox report",
            labels=["inbox-report:tradeos:desk-brief:2026-08-06", "report", "worker:you"],
        )
        self.tracker.update_task(
            report.id, gate_type="human", gate_note="Desk brief — mark read when done"
        )
        import worklane.api.tasks as _tasks_api
        _tasks_api._invalidate_attention_cache()
        j = self.client.get("/api/dev/attention").json()
        by_id = {it["id"]: it for it in j["items"]}
        self.assertIn(f"t-{decide.id}", by_id)
        self.assertIn(f"t-{report.id}", by_id)
        self.assertEqual(by_id[f"t-{decide.id}"]["kind"], "human_gate")
        self.assertEqual(by_id[f"t-{decide.id}"]["face"], "decide")
        self.assertEqual(by_id[f"t-{report.id}"]["kind"], "human_gate")
        self.assertEqual(by_id[f"t-{report.id}"]["face"], "read")
        # Band counts sum to visible_count; act_now excludes Read
        self.assertEqual(
            j["act_now_count"] + j["read_count"] + j["watch_count"],
            j["visible_count"],
        )
        self.assertGreaterEqual(j["act_now_count"], 1)
        self.assertGreaterEqual(j["read_count"], 1)
        # kind strings on items unchanged (no new gate_type / kind rename)
        self.assertNotEqual(by_id[f"t-{report.id}"]["kind"], "inbox_report")


if __name__ == "__main__":
    unittest.main()
