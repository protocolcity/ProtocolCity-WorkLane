"""Create-path routing matrix (wl-365 / wl-417 / ALWAYS_WORK §9).

One locked suite: HTTP POST /api/admin/tasks, MCP wl_create, CLI task create,
and import_jsonl each prove routing law under hired hands:

* bare create (no worker:*) → reject (HTTP/MCP/CLI/import hard-B default)
* create with worker:lili → accepted; seat preserved; no needs:routing
* import soft override (hard_when_hands=False) → needs:routing stamp only
  (archival restore path)

Unit helpers live in test_routing_labels_create.py; per-path detail lives in
test_create_task_routing_warning.py and test_portability.py. This file locks
the cross-entrypoint contract in one place.
"""
from __future__ import annotations

import argparse
import importlib
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from worklane.mcp.handlers import TPHandlers, ToolError
from worklane.trackers.sqlite import SQLiteTracker


PRODUCT = "worklane"
SEAT = "worker:lili"
AREA = "area:matrix"


def _load_direct_cli_task():
    """Direct-SQLite CLI (cli/task.py). Absent from public export (HTTP CLI only)."""
    for name in ("worklane.cli.task", "worklane.cli.task"):
        try:
            return importlib.import_module(name)
        except ImportError:
            continue
    return None


_CLI_TASK = _load_direct_cli_task()


def _workforce_response(workers: list) -> Any:
    body = json.dumps({"daemon": "running", "workers": workers}).encode("utf-8")
    mock_resp = MagicMock()
    mock_resp.read.return_value = body
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


def _lili_hand(product: str = PRODUCT) -> Dict[str, Any]:
    return {
        "name": "lili",
        "kind": "lane",
        "queue_url": (
            "http://127.0.0.1:8799/api/admin/tasks/ready"
            f"?product={product}&worker=lili"
        ),
    }


class _MatrixEnv(unittest.TestCase):
    """Hermetic runtime + WorkForce roster hiring lili for worklane."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wl_route_matrix_")
        self.root = Path(self._tmp.name)
        (self.root / "data").mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "data" / "worklane.db"

        self._env_keys = (
            "WORKLANE_RUNTIME_DIR",
            "WORKLANE_RUNTIME_DIR",
            "WORKLANE_DB",
            "WORKLANE_DB",
            "TRADEOS_TRACKER_DB",
            "TRADEOS_TICKETS_SOURCE",
            "WL_DEFAULT_PROJECT",
            "WL_DEFAULT_PRODUCT",
            "WL_PROJECT",
            "WL_PRODUCT",
            "WL_PROJECT",
            "WL_PRODUCT",
            "WL_WORKFORCE_URL",
            "WL_WORKFORCE_ROSTER",
            "WORKFORCE_PREDIRTY",
            "WL_WORKFORCE_NO_CITY_ROSTER",
            "WL_AGENT_ID",
            "WL_AGENT_ID",
        )
        self._env_before = {k: os.environ.get(k) for k in self._env_keys}

        os.environ["WORKLANE_RUNTIME_DIR"] = str(self.root)
        os.environ["WORKLANE_DB"] = str(self.db_path)
        os.environ.pop("WORKLANE_RUNTIME_DIR", None)
        os.environ.pop("WORKLANE_DB", None)
        os.environ.pop("TRADEOS_TRACKER_DB", None)
        os.environ["TRADEOS_TICKETS_SOURCE"] = "sqlite"
        os.environ["WL_DEFAULT_PRODUCT"] = PRODUCT
        os.environ.pop("WL_DEFAULT_PROJECT", None)
        os.environ["WL_PRODUCT"] = PRODUCT
        os.environ.pop("WL_PROJECT", None)
        os.environ.pop("WL_PROJECT", None)
        os.environ.pop("WL_PRODUCT", None)
        os.environ["WL_WORKFORCE_URL"] = "http://127.0.0.1:8797"
        os.environ.pop("WL_WORKFORCE_ROSTER", None)
        os.environ.pop("WORKFORCE_PREDIRTY", None)
        os.environ["WL_WORKFORCE_NO_CITY_ROSTER"] = "1"
        os.environ["WL_AGENT_ID"] = "lili"
        os.environ.pop("WL_AGENT_ID", None)

        # Materialize the store so product discovery sees worklane.
        SQLiteTracker(db_path=self.db_path, product_default="product:worklane")

        self._hired_patch = patch(
            "urllib.request.urlopen",
            return_value=_workforce_response([_lili_hand()]),
        )
        self._hired_patch.start()

    def tearDown(self) -> None:
        self._hired_patch.stop()
        for k, v in self._env_before.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    def _assert_seated(self, labels: Optional[List[str]]) -> None:
        labs = list(labels or [])
        self.assertIn(SEAT, labs)
        self.assertNotIn("needs:routing", labs)


class HttpCreatePathMatrix(_MatrixEnv):
    def setUp(self) -> None:
        super().setUp()
        from worklane.task_server import router

        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)

    def _post(self, labels: Optional[List[str]]) -> Any:
        body: Dict[str, Any] = {
            "title": "matrix http",
            "description": "HTTP create-path routing matrix case",
            "author": "lili",
            "surface": PRODUCT,
        }
        if labels is not None:
            body["labels"] = labels
        return self.client.post("/api/admin/tasks", json=body)

    def test_bare_under_hired_hands_rejected(self) -> None:
        r = self._post(labels=[AREA])
        self.assertEqual(r.status_code, 400, r.text)
        data = r.json()
        self.assertFalse(data.get("ok", True))
        err = data.get("error") or ""
        self.assertIn("worker:* required", err)
        self.assertIn(SEAT, err)

    def test_worker_lili_accepted_seat_preserved(self) -> None:
        r = self._post(labels=[SEAT, AREA])
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertTrue(data["ok"])
        self._assert_seated(data["task"].get("labels"))


class McpCreatePathMatrix(_MatrixEnv):
    def setUp(self) -> None:
        super().setUp()
        self.h = TPHandlers(author="lili", default_product=PRODUCT)

    def test_bare_under_hired_hands_rejected(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            self.h.wl_create(
                title="matrix mcp bare",
                description="MCP create-path routing matrix bare case",
                labels=[AREA],
            )
        msg = str(ctx.exception)
        self.assertIn("worker:* required", msg)
        self.assertIn(SEAT, msg)

    def test_worker_lili_accepted_seat_preserved(self) -> None:
        out = self.h.wl_create(
            title="matrix mcp seated",
            description="MCP create-path routing matrix seated case",
            labels=[SEAT, AREA],
        )
        self.assertTrue(out["ok"])
        self._assert_seated(out["task"].get("labels"))


@unittest.skipUnless(
    _CLI_TASK is not None,
    "cli/task.py not in public export — HTTP CLI delegates routing to API "
    "(covered by HttpCreatePathMatrix)",
)
class CliCreatePathMatrix(_MatrixEnv):
    """Direct-tracker CLI create (worklane/cli/task.py).

    Public export ships only the HTTP CLI (wl.py); bare-create hard-B there is
    enforced server-side and already locked by HttpCreatePathMatrix.
    """

    def _run_create(self, labels: Optional[List[str]]) -> int:
        assert _CLI_TASK is not None
        args = argparse.Namespace(
            title="matrix cli",
            description="CLI create-path routing matrix case",
            description_file=None,
            priority=3,
            label=labels,
            author="lili",
            intake="cli",
        )
        buf_out = io.StringIO()
        buf_err = io.StringIO()
        try:
            with redirect_stdout(buf_out), redirect_stderr(buf_err):
                _CLI_TASK.cmd_create(args)
            self._cli_out = buf_out.getvalue()
            self._cli_err = buf_err.getvalue()
            return 0
        except SystemExit as exc:
            self._cli_out = buf_out.getvalue()
            self._cli_err = buf_err.getvalue()
            code = exc.code
            if code is None:
                return 0
            return int(code) if not isinstance(code, int) else code

    def test_bare_under_hired_hands_rejected(self) -> None:
        code = self._run_create([AREA])
        self.assertEqual(code, 1, msg=self._cli_err)
        self.assertIn("worker:* required", self._cli_err)
        self.assertIn(SEAT, self._cli_err)

    def test_worker_lili_accepted_seat_preserved(self) -> None:
        code = self._run_create([SEAT, AREA])
        self.assertEqual(code, 0, msg=self._cli_err + self._cli_out)
        self.assertIn("Created #", self._cli_out)
        tasks = SQLiteTracker(db_path=self.db_path).list_tasks()
        self.assertEqual(len(tasks), 1)
        self._assert_seated(list(tasks[0].labels or []))


class ImportCreatePathMatrix(_MatrixEnv):
    """Import hard-B default (wl-417); soft override for archival restore."""

    def _import_one(
        self,
        labels: List[str],
        *,
        hard_when_hands: bool = True,
        hired_hands: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        from worklane import portability

        tr = SQLiteTracker(db_path=self.db_path, product_default="")
        line = json.dumps(
            {
                "id": "src-1",
                "ext_id": None,
                "title": "matrix import",
                "description": "import create-path routing matrix case",
                "status": "backlog",
                "priority": 3,
                "labels": labels,
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "comments": [],
            },
            separators=(",", ":"),
        )
        if hired_hands is None and hard_when_hands:
            hired_hands = [SEAT]
        report = portability.import_jsonl(
            [line],
            PRODUCT,
            tracker=tr,
            hard_when_hands=hard_when_hands,
            hired_hands=hired_hands,
        )
        self.assertEqual(len(report.created), 1)
        tasks = tr.list_tasks()
        self.assertEqual(len(tasks), 1)
        return tasks[0].__dict__

    def test_bare_under_hired_hands_hard_rejects(self) -> None:
        from worklane import portability

        tr = SQLiteTracker(db_path=self.db_path, product_default="")
        line = json.dumps(
            {
                "id": "src-bare",
                "ext_id": None,
                "title": "matrix import bare",
                "description": "hard-B bare seat",
                "status": "backlog",
                "priority": 3,
                "labels": [AREA],
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "comments": [],
            },
            separators=(",", ":"),
        )
        with self.assertRaises(portability.PortabilityError) as ctx:
            portability.import_jsonl(
                [line],
                PRODUCT,
                tracker=tr,
                hard_when_hands=True,
                hired_hands=[SEAT],
            )
        self.assertIn("worker:* required", str(ctx.exception))
        self.assertEqual(tr.list_tasks(), [])

    def test_soft_override_stamps_needs_routing(self) -> None:
        # Archival restore path — hard_when_hands=False never rejects.
        task = self._import_one(
            [AREA], hard_when_hands=False, hired_hands=[SEAT]
        )
        labs = list(task["labels"] or [])
        self.assertIn("needs:routing", labs)
        self.assertNotIn(SEAT, labs)

    def test_worker_lili_seat_preserved(self) -> None:
        task = self._import_one([SEAT, AREA])
        self._assert_seated(list(task["labels"] or []))


if __name__ == "__main__":
    unittest.main()
