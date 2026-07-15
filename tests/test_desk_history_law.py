"""wl-182: Desk HISTORY LAW — closeWO clears sticky ?open=.

Deep-link /admin/desk?open=<id> must not survive Esc/scrim/close: closeWO
strips the query param via history.replaceState so a refresh does not reopen
the work-order overlay (HISTORY LAW — overlays app-owned, no sticky half-state).
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from worklane.trackers.sqlite import SQLiteTracker


class DeskHistoryLawTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wl_desk_history_law_")
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
        SQLiteTracker(db_path=self.root / "data" / "tradeos.db")

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

    def test_close_wo_clears_open_query_param(self) -> None:
        body = self.client.get("/admin/desk").text
        self.assertIn("function closeWO", body)
        self.assertIn("HISTORY LAW", body)
        self.assertIn('searchParams.has("open")', body)
        self.assertIn('searchParams.delete("open")', body)
        self.assertIn("history.replaceState", body)
