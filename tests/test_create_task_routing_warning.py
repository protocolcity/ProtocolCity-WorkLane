"""Create-path routing: stamp needs:routing + soft warning when worker:* absent.

2026-07-27: any create without worker:* auto-stamps ``needs:routing`` and
returns a non-blocking ``routing_warning`` (silent unlabeled ready is a bug
under WorkForce exclusive feeds). Hired-hand list is appended when known.
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
                "WL_WORKFORCE_ROSTER",
                "WORKFORCE_PREDIRTY",
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

    def test_reject_when_no_worker_label_and_hands_hired(self):
        """Hard B (wl-274): create without worker:* fails when hands hired."""
        mock_resp = _workforce_response([_worker_entry("tess", "worklane")])
        with patch("urllib.request.urlopen", return_value=mock_resp):
            r = self._post(labels=["intake"])
        self.assertEqual(r.status_code, 400)
        data = r.json()
        self.assertFalse(data.get("ok", True))
        err = data.get("error") or ""
        self.assertIn("worker:* required", err)
        self.assertIn("worker:tess", err)
        self.assertIn("worker:you", err)

    def test_no_warning_when_worker_label_present(self):
        """No warning when a worker:* label is already provided."""
        mock_resp = _workforce_response([_worker_entry("tess", "worklane")])
        with patch("urllib.request.urlopen", return_value=mock_resp):
            r = self._post(labels=["worker:tess", "intake"])
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["ok"])
        self.assertIsNone(data.get("routing_warning"))
        labs = data["task"].get("labels") or []
        self.assertIn("worker:tess", labs)
        self.assertNotIn("needs:routing", labs)

    def test_stamps_needs_routing_when_workforce_unreachable(self):
        """Still stamps needs:routing when WorkForce is down (no crash)."""
        import urllib.error
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
            r = self._post(labels=["intake"])
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["ok"])
        self.assertIsNotNone(data.get("routing_warning"))
        self.assertIn("needs:routing", data["task"].get("labels") or [])

    def test_stamps_when_no_workers_for_product(self):
        """Stamp even when hired hands are for another product."""
        other_worker = _worker_entry("carl", "tradeos")
        mock_resp = _workforce_response([other_worker])
        with patch("urllib.request.urlopen", return_value=mock_resp):
            r = self._post(labels=["intake"])
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["ok"])
        self.assertIsNotNone(data.get("routing_warning"))
        self.assertIn("needs:routing", data["task"].get("labels") or [])

    def test_stamps_when_no_labels_and_no_workers(self):
        """Empty labels still get needs:routing."""
        mock_resp = _workforce_response([])
        with patch("urllib.request.urlopen", return_value=mock_resp):
            r = self._post()
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["ok"])
        self.assertIsNotNone(data.get("routing_warning"))
        self.assertIn("needs:routing", data["task"].get("labels") or [])

    def test_job_kind_not_listed_as_hired_but_still_stamps(self):
        """Job-kind entries are not listed as hired hands; stamp still applies."""
        job_worker = _worker_entry("clerk", "worklane", kind="job")
        mock_resp = _workforce_response([job_worker])
        with patch("urllib.request.urlopen", return_value=mock_resp):
            r = self._post(labels=["intake"])
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["ok"])
        self.assertIsNotNone(data.get("routing_warning"))
        self.assertNotIn("clerk", data["routing_warning"] or "")
        self.assertIn("needs:routing", data["task"].get("labels") or [])


    def test_roster_fallback_rejects_when_api_down(self):
        """Roster fallback (wl-287): hard-B rejection when API fails but roster names a hand."""
        roster_file = self.root / "roster.json"
        roster_file.write_text(json.dumps({
            "workers": {
                "tess": {
                    "kind": "lane",
                    "queue_url": (
                        "http://127.0.0.1:8799/api/admin/tasks/ready"
                        "?product=worklane&label=worker:tess"
                    ),
                }
            }
        }))
        os.environ["WL_WORKFORCE_ROSTER"] = str(roster_file)
        import urllib.error
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
            r = self._post(labels=["intake"])
        self.assertEqual(r.status_code, 400)
        data = r.json()
        self.assertFalse(data.get("ok", True))
        err = data.get("error") or ""
        self.assertIn("worker:* required", err)
        self.assertIn("worker:tess", err)
        self.assertIn("worker:you", err)

    def test_roster_fallback_soft_stamp_when_no_hands_for_product(self):
        """Roster fallback: soft stamp when roster exists but has no hand for this store."""
        roster_file = self.root / "roster.json"
        roster_file.write_text(json.dumps({
            "workers": {
                "carl": {
                    "kind": "lane",
                    "queue_url": (
                        "http://127.0.0.1:8799/api/admin/tasks/ready"
                        "?product=tradeos&label=worker:carl"
                    ),
                }
            }
        }))
        os.environ["WL_WORKFORCE_ROSTER"] = str(roster_file)
        import urllib.error
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
            r = self._post(labels=["intake"])
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["ok"])
        self.assertIsNotNone(data.get("routing_warning"))
        self.assertIn("needs:routing", data["task"].get("labels") or [])

    def test_slow_api_roster_fallback_rejects(self):
        """wl-306: socket.timeout (slow API) falls to roster → hard B rejects."""
        import socket
        roster_file = self.root / "roster.json"
        roster_file.write_text(json.dumps({
            "workers": {
                "vera": {
                    "kind": "lane",
                    "queue_url": (
                        "http://127.0.0.1:8799/api/admin/tasks/ready"
                        "?product=worklane&label=worker:vera"
                    ),
                }
            }
        }))
        os.environ["WL_WORKFORCE_ROSTER"] = str(roster_file)
        with patch("urllib.request.urlopen", side_effect=socket.timeout("timed out")):
            r = self._post(labels=["intake"])
        self.assertEqual(r.status_code, 400)
        data = r.json()
        self.assertFalse(data.get("ok", True))
        err = data.get("error") or ""
        self.assertIn("worker:* required", err)
        self.assertIn("worker:vera", err)
        self.assertIn("worker:you", err)

    def test_city_roster_autodiscovery_rejects(self):
        """wl-306: city-root auto-discovery finds roster → hard B rejects without env vars."""
        import urllib.error
        city_roster = self.root / ".protocolcity" / "workforce" / "local" / "roster.json"
        city_roster.parent.mkdir(parents=True, exist_ok=True)
        city_roster.write_text(json.dumps({
            "workers": {
                "figaro": {
                    "kind": "lane",
                    "queue_url": (
                        "http://127.0.0.1:8799/api/admin/tasks/ready"
                        "?product=worklane&label=worker:figaro"
                    ),
                }
            }
        }))
        os.environ.pop("WL_WORKFORCE_NO_CITY_ROSTER", None)
        os.environ.pop("WL_WORKFORCE_ROSTER", None)
        os.environ.pop("WORKFORCE_PREDIRTY", None)
        old_cwd = os.getcwd()
        try:
            os.chdir(self.root)
            with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
                r = self._post(labels=["intake"])
        finally:
            os.chdir(old_cwd)
        self.assertEqual(r.status_code, 400)
        data = r.json()
        self.assertFalse(data.get("ok", True))
        err = data.get("error") or ""
        self.assertIn("worker:* required", err)
        self.assertIn("worker:figaro", err)
        self.assertIn("worker:you", err)


if __name__ == "__main__":
    unittest.main()
