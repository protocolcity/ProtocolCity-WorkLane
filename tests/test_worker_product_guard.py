"""wl-296: cross-product mismatch guard — warn or reject when worker:* is
registered for a different product than the ticket's store.

Two layers:
- routing_labels.check_worker_product_mismatch() pure function
- HTTP create path (POST /api/admin/tasks) and label-add path (PATCH .../labels)
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from worklane.routing_labels import check_worker_product_mismatch
from worklane.trackers.sqlite import SQLiteTracker


# ── pure-function tests ───────────────────────────────────────────────────────

class CheckWorkerProductMismatchTest(unittest.TestCase):
    def test_returns_none_when_match(self) -> None:
        result = check_worker_product_mismatch(
            ["tom"], "protocolcity", {"tom": "protocolcity"}
        )
        self.assertIsNone(result)

    def test_returns_warning_when_mismatch(self) -> None:
        result = check_worker_product_mismatch(
            ["tom"], "worklane", {"tom": "protocolcity"}
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertIn("worker:tom", result)
        self.assertIn("protocolcity", result)
        self.assertIn("worklane", result)

    def test_returns_none_when_worker_absent_from_roster(self) -> None:
        # Unknown workers are never an error — roster absence = not wrong.
        result = check_worker_product_mismatch(["mystery"], "worklane", {})
        self.assertIsNone(result)

    def test_skips_worker_you(self) -> None:
        result = check_worker_product_mismatch(
            ["you"], "worklane", {"you": "protocolcity"}
        )
        self.assertIsNone(result)

    def test_multiple_workers_all_mismatch(self) -> None:
        result = check_worker_product_mismatch(
            ["tom", "nala"],
            "worklane",
            {"tom": "protocolcity", "nala": "tradeos"},
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertIn("worker:tom", result)
        self.assertIn("worker:nala", result)

    def test_partial_mismatch_only_names_bad_one(self) -> None:
        result = check_worker_product_mismatch(
            ["tom", "lili"],
            "worklane",
            {"tom": "protocolcity", "lili": "worklane"},
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertIn("worker:tom", result)
        self.assertNotIn("worker:lili", result)

    def test_case_insensitive_you_skip(self) -> None:
        result = check_worker_product_mismatch(
            ["You", "YOU"],
            "worklane",
            {"You": "protocolcity", "YOU": "tradeos"},
        )
        self.assertIsNone(result)


# ── HTTP layer helpers ────────────────────────────────────────────────────────

def _workforce_response(workers: list) -> Any:
    body = json.dumps({"daemon": "running", "workers": workers}).encode("utf-8")
    mock_resp = MagicMock()
    mock_resp.read.return_value = body
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


def _worker_entry(name: str, product: str, kind: str = "lane") -> Dict[str, Any]:
    return {
        "name": name,
        "kind": kind,
        "queue_url": (
            f"http://127.0.0.1:8799/api/admin/tasks/ready"
            f"?product={product}&worker={name}"
        ),
    }


class WorkerProductGuardHTTPTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wl_wp_guard_")
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
                "WL_WORKER_PRODUCT_HARD_REJECT",
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
        os.environ["WL_WORKFORCE_URL"] = "http://127.0.0.1:8797"
        os.environ.pop("WL_WORKFORCE_ROSTER", None)
        os.environ.pop("WORKFORCE_PREDIRTY", None)
        os.environ.pop("WL_WORKER_PRODUCT_HARD_REJECT", None)

        SQLiteTracker(
            db_path=self.root / "data" / "worklane.db",
            product_default="product:worklane",
        )

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

    def _post(self, labels=None, surface="worklane") -> Any:
        body: Dict[str, Any] = {
            "title": "test ticket",
            "description": "a description",
            "author": "lili",
            "surface": surface,
        }
        if labels is not None:
            body["labels"] = labels
        return self.client.post("/api/admin/tasks", json=body)

    # ── create path ───────────────────────────────────────────────────────────

    def test_create_wrong_product_emits_warning(self) -> None:
        """worker:tom registered for protocolcity, ticket on worklane → warning."""
        mock_resp = _workforce_response([_worker_entry("tom", "protocolcity")])
        with patch("urllib.request.urlopen", return_value=mock_resp):
            r = self._post(labels=["worker:tom", "intake"])
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["ok"])
        warn = data.get("routing_warning") or ""
        self.assertIn("worker:tom", warn)
        self.assertIn("protocolcity", warn)
        self.assertIn("worklane", warn)

    def test_create_correct_product_no_warning(self) -> None:
        """worker:lili registered for worklane, ticket on worklane → clean."""
        mock_resp = _workforce_response([_worker_entry("lili", "worklane")])
        with patch("urllib.request.urlopen", return_value=mock_resp):
            r = self._post(labels=["worker:lili", "intake"])
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["ok"])
        self.assertIsNone(data.get("routing_warning"))

    def test_create_wrong_product_hard_reject(self) -> None:
        """WL_WORKER_PRODUCT_HARD_REJECT=1 turns the warning into a 400."""
        os.environ["WL_WORKER_PRODUCT_HARD_REJECT"] = "1"
        mock_resp = _workforce_response([_worker_entry("tom", "protocolcity")])
        with patch("urllib.request.urlopen", return_value=mock_resp):
            r = self._post(labels=["worker:tom", "intake"])
        self.assertEqual(r.status_code, 400)
        data = r.json()
        self.assertFalse(data.get("ok", True))
        err = data.get("error") or ""
        self.assertIn("worker:tom", err)
        self.assertIn("mismatch", err)

    def test_create_worker_you_never_flagged(self) -> None:
        """worker:you is always a valid human seat — no cross-product check."""
        mock_resp = _workforce_response([_worker_entry("lili", "worklane")])
        with patch("urllib.request.urlopen", return_value=mock_resp):
            r = self._post(labels=["worker:you", "intake"])
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["ok"])
        self.assertIsNone(data.get("routing_warning"))

    def test_create_unknown_worker_no_warning(self) -> None:
        """A worker not in the roster is not flagged — absence ≠ wrong."""
        mock_resp = _workforce_response([_worker_entry("lili", "worklane")])
        with patch("urllib.request.urlopen", return_value=mock_resp):
            r = self._post(labels=["worker:ghost", "intake"])
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["ok"])
        self.assertIsNone(data.get("routing_warning"))

    def test_create_roster_fallback_warns_on_mismatch(self) -> None:
        """Local roster fallback (wl-287) also triggers the guard."""
        import urllib.error
        roster_file = self.root / "roster.json"
        roster_file.write_text(json.dumps({
            "workers": {
                "tom": {
                    "kind": "lane",
                    "queue_url": (
                        "http://127.0.0.1:8799/api/admin/tasks/ready"
                        "?product=protocolcity&worker=tom"
                    ),
                },
                "lili": {
                    "kind": "lane",
                    "queue_url": (
                        "http://127.0.0.1:8799/api/admin/tasks/ready"
                        "?product=worklane&worker=lili"
                    ),
                },
            }
        }))
        os.environ["WL_WORKFORCE_ROSTER"] = str(roster_file)
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
            r = self._post(labels=["worker:tom", "intake"])
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["ok"])
        warn = data.get("routing_warning") or ""
        self.assertIn("worker:tom", warn)
        self.assertIn("protocolcity", warn)

    # ── label update path ─────────────────────────────────────────────────────

    def _create_ticket(self, mock_resp: Any) -> str:
        """Helper: create a worklane ticket as lili and return its id."""
        with patch("urllib.request.urlopen", return_value=mock_resp):
            r = self._post(labels=["worker:lili"])
        self.assertEqual(r.status_code, 200)
        return r.json()["task"]["id"]

    def test_label_add_wrong_product_emits_warning(self) -> None:
        """Adding worker:tom (protocolcity) to a worklane ticket → warning."""
        lili_resp = _workforce_response([_worker_entry("lili", "worklane")])
        ticket_id = self._create_ticket(lili_resp)

        tom_resp = _workforce_response([
            _worker_entry("lili", "worklane"),
            _worker_entry("tom", "protocolcity"),
        ])
        with patch("urllib.request.urlopen", return_value=tom_resp):
            r = self.client.patch(
                f"/api/admin/tasks/{ticket_id}/labels",
                json={"add": ["worker:tom"], "remove": ["worker:lili"]},
            )
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["ok"])
        warn = data.get("routing_warning") or ""
        self.assertIn("worker:tom", warn)
        self.assertIn("protocolcity", warn)

    def test_label_add_correct_product_no_warning(self) -> None:
        """Swapping to another worklane worker → no warning."""
        lili_resp = _workforce_response([_worker_entry("lili", "worklane")])
        ticket_id = self._create_ticket(lili_resp)

        swap_resp = _workforce_response([
            _worker_entry("lili", "worklane"),
            _worker_entry("drew", "worklane"),
        ])
        with patch("urllib.request.urlopen", return_value=swap_resp):
            r = self.client.patch(
                f"/api/admin/tasks/{ticket_id}/labels",
                json={"add": ["worker:drew"], "remove": ["worker:lili"]},
            )
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["ok"])
        self.assertIsNone(data.get("routing_warning"))

    def test_label_add_wrong_product_hard_reject(self) -> None:
        """WL_WORKER_PRODUCT_HARD_REJECT=1 rejects the label update."""
        lili_resp = _workforce_response([_worker_entry("lili", "worklane")])
        ticket_id = self._create_ticket(lili_resp)

        os.environ["WL_WORKER_PRODUCT_HARD_REJECT"] = "1"
        tom_resp = _workforce_response([
            _worker_entry("lili", "worklane"),
            _worker_entry("tom", "protocolcity"),
        ])
        with patch("urllib.request.urlopen", return_value=tom_resp):
            r = self.client.patch(
                f"/api/admin/tasks/{ticket_id}/labels",
                json={"add": ["worker:tom"]},
            )
        self.assertEqual(r.status_code, 400)
        data = r.json()
        self.assertFalse(data.get("ok", True))
        self.assertIn("mismatch", data.get("error") or "")


if __name__ == "__main__":
    unittest.main()
