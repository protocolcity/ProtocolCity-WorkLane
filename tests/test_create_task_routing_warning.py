"""wl-256: soft routing warning on POST /api/admin/tasks when worker:* absent.

The create endpoint emits a non-blocking ``routing_warning`` string when:
- the ticket carries no ``worker:*`` label, AND
- WorkForce reports at least one lane worker hired for the target product.

The warning is omitted when worker:* is present, when WorkForce is unreachable,
or when no workers are hired for the product.
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

from worklane.trackers.sqlite import SQLiteTracker


def _workforce_response(workers: list) -> Any:
    """Build a mock urllib.request.urlopen context manager returning given workers."""
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
        "queue_url": f"http://127.0.0.1:8799/api/admin/tasks/ready?product={product}&worker={name}",
    }


class CreateTaskRoutingWarningTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wl_routing_warn_")
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

        SQLiteTracker(db_path=self.root / "data" / "worklane.db", product_default="product:worklane")

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

    def _post(self, labels=None, surface="worklane"):
        body: Dict[str, Any] = {
            "title": "test ticket",
            "description": "a description",
            "author": "tess",
            "surface": surface,
        }
        if labels is not None:
            body["labels"] = labels
        return self.client.post("/api/admin/tasks", json=body)

    # ── cases ────────────────────────────────────────────────────────────────

    def test_warning_when_no_worker_label_and_hands_hired(self):
        """Warning appears when no worker:* label and WorkForce has hired hands."""
        mock_resp = _workforce_response([_worker_entry("tess", "worklane")])
        with patch("urllib.request.urlopen", return_value=mock_resp):
            r = self._post(labels=["intake"])
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["ok"])
        self.assertIsNotNone(data.get("routing_warning"))
        self.assertIn("worker:tess", data["routing_warning"])

    def test_no_warning_when_worker_label_present(self):
        """No warning when a worker:* label is already provided."""
        mock_resp = _workforce_response([_worker_entry("tess", "worklane")])
        with patch("urllib.request.urlopen", return_value=mock_resp):
            r = self._post(labels=["worker:tess", "intake"])
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["ok"])
        self.assertIsNone(data.get("routing_warning"))

    def test_no_warning_when_workforce_unreachable(self):
        """No warning (and no error) when WorkForce is down."""
        import urllib.error
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
            r = self._post(labels=["intake"])
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["ok"])
        self.assertIsNone(data.get("routing_warning"))

    def test_no_warning_when_no_workers_for_product(self):
        """No warning when WorkForce has workers but none for this product."""
        other_worker = _worker_entry("carl", "tradeos")
        mock_resp = _workforce_response([other_worker])
        with patch("urllib.request.urlopen", return_value=mock_resp):
            r = self._post(labels=["intake"])
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["ok"])
        self.assertIsNone(data.get("routing_warning"))

    def test_no_warning_when_no_labels_and_no_workers(self):
        """No warning when no labels and no workers hired for the product."""
        mock_resp = _workforce_response([])
        with patch("urllib.request.urlopen", return_value=mock_resp):
            r = self._post()
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["ok"])
        self.assertIsNone(data.get("routing_warning"))

    def test_job_kind_worker_not_counted_as_hired_hand(self):
        """Job-kind entries are not counted as hired hands (only lane kind)."""
        job_worker = _worker_entry("clerk", "worklane", kind="job")
        mock_resp = _workforce_response([job_worker])
        with patch("urllib.request.urlopen", return_value=mock_resp):
            r = self._post(labels=["intake"])
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["ok"])
        self.assertIsNone(data.get("routing_warning"))


if __name__ == "__main__":
    unittest.main()
