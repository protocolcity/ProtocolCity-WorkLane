"""wl-359: route-event wake nudge — unit + HTTP create/label/gate/release paths."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from worklane.trackers.protocol import TaskStatus
from worklane.trackers.sqlite import SQLiteTracker
from worklane.wake_nudge import (
    DEBOUNCE_S,
    dispatchable_hand,
    maybe_wake_hand,
    post_wake,
    previous_hand_from_labels,
    reset_wake_debounce,
)


class DispatchableHandTest(unittest.TestCase):
    def test_lane_seat_backlog_ungated(self) -> None:
        self.assertEqual(
            dispatchable_hand(["worker:lili", "ship"], "backlog", None),
            "lili",
        )

    def test_worker_you_skipped(self) -> None:
        self.assertIsNone(
            dispatchable_hand(["worker:you", "you:host"], "backlog", None)
        )

    def test_gated_skipped(self) -> None:
        for gt in ("human", "timer", "deferred"):
            self.assertIsNone(
                dispatchable_hand(["worker:lili"], "backlog", gt),
                msg=gt,
            )

    def test_non_backlog_skipped(self) -> None:
        self.assertIsNone(
            dispatchable_hand(["worker:lili"], "in_progress", None)
        )

    def test_no_worker_skipped(self) -> None:
        self.assertIsNone(dispatchable_hand(["ship"], "backlog", None))

    def test_previous_hand_from_labels(self) -> None:
        self.assertEqual(previous_hand_from_labels(["worker:kc"]), "kc")
        self.assertIsNone(previous_hand_from_labels(["worker:you"]))
        self.assertIsNone(previous_hand_from_labels(["ship"]))


class PostWakeTest(unittest.TestCase):
    def setUp(self) -> None:
        reset_wake_debounce()
        self._saved = os.environ.pop("WL_WAKE_DISABLE", None)
        os.environ["WL_WORKFORCE_URL"] = "http://127.0.0.1:8797"

    def tearDown(self) -> None:
        if self._saved is not None:
            os.environ["WL_WAKE_DISABLE"] = self._saved
        else:
            os.environ.pop("WL_WAKE_DISABLE", None)
        reset_wake_debounce()

    def _mock_resp(self, status: int = 200):
        resp = mock.MagicMock()
        resp.status = status
        resp.__enter__ = lambda s: s
        resp.__exit__ = mock.MagicMock(return_value=False)
        return resp

    def test_kill_switch_skips_network(self) -> None:
        os.environ["WL_WAKE_DISABLE"] = "1"
        with mock.patch("urllib.request.urlopen") as m:
            self.assertTrue(post_wake("lili"))
            m.assert_not_called()

    def test_dispatch_posts_worker_json(self) -> None:
        with mock.patch(
            "urllib.request.urlopen", return_value=self._mock_resp(200)
        ) as m:
            self.assertTrue(post_wake("lili"))
            req = m.call_args[0][0]
            self.assertEqual(req.full_url, "http://127.0.0.1:8797/api/wake")
            self.assertEqual(req.data, b'{"worker": "lili"}')
            self.assertEqual(req.get_method(), "POST")

    def test_wake_down_returns_false_no_raise(self) -> None:
        with mock.patch(
            "urllib.request.urlopen", side_effect=OSError("refused")
        ):
            self.assertFalse(post_wake("lili"))


class MaybeWakeHandTest(unittest.TestCase):
    def setUp(self) -> None:
        reset_wake_debounce()
        self._saved = os.environ.pop("WL_WAKE_DISABLE", None)

    def tearDown(self) -> None:
        if self._saved is not None:
            os.environ["WL_WAKE_DISABLE"] = self._saved
        else:
            os.environ.pop("WL_WAKE_DISABLE", None)
        reset_wake_debounce()

    def test_create_with_seat_nudges_once(self) -> None:
        with mock.patch(
            "worklane.wake_nudge.post_wake", return_value=True
        ) as m:
            ok = maybe_wake_hand(
                ["worker:lili"],
                status="backlog",
                only_on_seat_change=True,
                previous_hand=None,
            )
            self.assertTrue(ok)
            m.assert_called_once_with("lili")

    def test_seat_unchanged_no_nudge(self) -> None:
        with mock.patch(
            "worklane.wake_nudge.post_wake", return_value=True
        ) as m:
            ok = maybe_wake_hand(
                ["worker:lili"],
                status="backlog",
                only_on_seat_change=True,
                previous_hand="lili",
            )
            self.assertFalse(ok)
            m.assert_not_called()

    def test_re_route_nudges_new_seat(self) -> None:
        with mock.patch(
            "worklane.wake_nudge.post_wake", return_value=True
        ) as m:
            ok = maybe_wake_hand(
                ["worker:kc"],
                status="backlog",
                only_on_seat_change=True,
                previous_hand="lili",
            )
            self.assertTrue(ok)
            m.assert_called_once_with("kc")

    def test_gated_no_nudge(self) -> None:
        with mock.patch(
            "worklane.wake_nudge.post_wake", return_value=True
        ) as m:
            ok = maybe_wake_hand(
                ["worker:lili"],
                status="backlog",
                gate_type="deferred",
                only_on_seat_change=False,
            )
            self.assertFalse(ok)
            m.assert_not_called()

    def test_release_ready_nudges(self) -> None:
        with mock.patch(
            "worklane.wake_nudge.post_wake", return_value=True
        ) as m:
            ok = maybe_wake_hand(
                ["worker:lili"],
                status="backlog",
                only_on_seat_change=False,
            )
            self.assertTrue(ok)
            m.assert_called_once_with("lili")

    def test_debounce_bulk_create(self) -> None:
        calls: List[str] = []

        def _record(wid: str) -> bool:
            calls.append(wid)
            return True

        with mock.patch(
            "worklane.wake_nudge.post_wake", side_effect=_record
        ):
            self.assertTrue(
                maybe_wake_hand(["worker:lili"], status="backlog")
            )
            self.assertFalse(
                maybe_wake_hand(["worker:lili"], status="backlog")
            )
            self.assertFalse(
                maybe_wake_hand(["worker:lili"], status="backlog")
            )
        self.assertEqual(calls, ["lili"])

    def test_debounce_window_expires(self) -> None:
        with mock.patch(
            "worklane.wake_nudge.post_wake", return_value=True
        ) as m:
            self.assertTrue(
                maybe_wake_hand(["worker:lili"], status="backlog")
            )
            # Force last-wake into the past beyond DEBOUNCE_S.
            import worklane.wake_nudge as wn

            with wn._lock:
                wn._last_wake["lili"] = wn.time.monotonic() - (DEBOUNCE_S + 1)
            self.assertTrue(
                maybe_wake_hand(["worker:lili"], status="backlog")
            )
            self.assertEqual(m.call_count, 2)


class WakeNudgeHttpTest(unittest.TestCase):
    """HTTP create / label / gate-clear paths with wake mocked."""

    def setUp(self) -> None:
        reset_wake_debounce()
        self._tmp = tempfile.TemporaryDirectory(prefix="wl_wake_")
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
                "WL_WORKFORCE_URL",
                "WL_WORKFORCE_ROSTER",
                "WORKFORCE_PREDIRTY",
                "WL_WAKE_DISABLE",
                "WL_WORKFORCE_NO_CITY_ROSTER",
            )
        }
        os.environ["WORKLANE_RUNTIME_DIR"] = str(self.root)
        os.environ["WORKLANE_DB"] = str(self.root / "data" / "worklane.db")
        os.environ.pop("TRADEOS_TRACKER_DB", None)
        os.environ["TRADEOS_TICKETS_SOURCE"] = "sqlite"
        os.environ["WL_DEFAULT_PRODUCT"] = "worklane"
        os.environ.pop("WL_DEFAULT_PROJECT", None)
        os.environ.pop("WL_PROJECT", None)
        os.environ.pop("WL_PRODUCT", None)
        # Pre-hire: no hired hands so create without hard-B rejection.
        os.environ["WL_WORKFORCE_URL"] = "http://127.0.0.1:1"
        os.environ.pop("WL_WORKFORCE_ROSTER", None)
        os.environ.pop("WORKFORCE_PREDIRTY", None)
        os.environ["WL_WORKFORCE_NO_CITY_ROSTER"] = "1"
        # Exercise real wake path (conftest kill switch off for this class).
        os.environ.pop("WL_WAKE_DISABLE", None)

        SQLiteTracker(
            db_path=self.root / "data" / "worklane.db",
            product_default="product:worklane",
        )

        from worklane.task_server import router

        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)

        self._wake_patch = mock.patch(
            "worklane.wake_nudge.post_wake", return_value=True
        )
        self.mock_wake = self._wake_patch.start()

    def tearDown(self) -> None:
        self._wake_patch.stop()
        for k, v in self._env_before.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()
        reset_wake_debounce()

    def _create(self, title: str, labels: List[str], desc: str = "wake test") -> Dict[str, Any]:
        r = self.client.post(
            "/api/admin/tasks",
            json={
                "title": title,
                "description": desc,
                "project": "worklane",
                "labels": labels,
                "author": "lili",
            },
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body.get("ok"), body)
        return body

    def test_create_with_seat_nudges(self) -> None:
        body = self._create("Wake create", ["worker:lili", "engine"])
        self.assertTrue(body.get("ok"))
        self.mock_wake.assert_called_with("lili")

    def test_create_worker_you_no_nudge(self) -> None:
        self._create("You seat", ["worker:you", "you:host"])
        self.mock_wake.assert_not_called()

    def test_label_reroute_nudges_new_seat(self) -> None:
        body = self._create("Reroute", ["worker:lili"])
        tid = body["task"]["id"]
        self.mock_wake.reset_mock()
        reset_wake_debounce()

        r = self.client.patch(
            f"/api/admin/tasks/{tid}/labels",
            json={"add": ["worker:kc"], "remove": ["worker:lili"]},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.mock_wake.assert_called_with("kc")

    def test_gated_label_no_nudge(self) -> None:
        # Gated ticket re-label must not wake (nothing dispatchable).
        body = self._create("Gate park", ["worker:lili"])
        tid = body["task"]["id"]
        self.client.patch(
            f"/api/admin/tasks/{tid}",
            json={"gate_type": "deferred", "gate_note": "park"},
        )
        self.mock_wake.reset_mock()
        reset_wake_debounce()
        r = self.client.patch(
            f"/api/admin/tasks/{tid}/labels",
            json={"add": ["worker:kc"], "remove": ["worker:lili"]},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.mock_wake.assert_not_called()

    def test_gate_clear_nudges(self) -> None:
        body = self._create("Gate clear", ["worker:lili"])
        tid = body["task"]["id"]
        self.client.patch(
            f"/api/admin/tasks/{tid}",
            json={"gate_type": "deferred", "gate_note": "park"},
        )
        self.mock_wake.reset_mock()
        reset_wake_debounce()
        r = self.client.patch(
            f"/api/admin/tasks/{tid}",
            json={"gate_type": ""},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.mock_wake.assert_called_with("lili")

    def test_status_backlog_release_nudges(self) -> None:
        body = self._create("Release wake", ["worker:lili"])
        tid = body["task"]["id"]
        self.client.patch(
            f"/api/admin/tasks/{tid}",
            json={"status": "in_progress", "author": "lili"},
        )
        self.mock_wake.reset_mock()
        reset_wake_debounce()
        r = self.client.patch(
            f"/api/admin/tasks/{tid}",
            json={"status": "backlog", "author": "lili"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.mock_wake.assert_called_with("lili")

    def test_wake_down_mutation_still_succeeds(self) -> None:
        self.mock_wake.return_value = False
        body = self._create(
            "Wake down",
            ["worker:lili"],
            desc="mutation must succeed when wake fails",
        )
        self.assertTrue(body.get("ok"))
        self.assertEqual(body["task"]["status"], TaskStatus.BACKLOG)


if __name__ == "__main__":
    unittest.main()
