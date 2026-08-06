"""wl-339 — per-project close-out check cite guard."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from worklane.closeout_checks import (
    CHECKS_HINT_PREFIX,
    CloseoutCheck,
    ProductCloseoutChecks,
    closeout_checks_violation,
    extract_verification_section,
    load_closeout_checks_registry,
    missing_check_cites,
    verification_checks_violation,
    verification_missing_result_signal,
)
from worklane.mcp.handlers import TPHandlers, ToolError
from worklane.trackers.sqlite import SQLiteTracker


class CloseoutChecksUnitTests(unittest.TestCase):
    def test_extract_verification_section(self) -> None:
        body = (
            "Completed:\n- x\n\nVerification:\n- pytest green\n\n"
            "Links:\n- abc1234\n\nFollow-ups:\nnone"
        )
        sec = extract_verification_section(body)
        self.assertIn("pytest", sec)
        self.assertNotIn("Links", sec)
        self.assertNotIn("Completed", sec)

    def test_missing_check_cites(self) -> None:
        checks = (
            CloseoutCheck(id="pytest", tokens=("pytest",)),
            CloseoutCheck(id="py_compile", tokens=("py_compile", "compile")),
        )
        self.assertEqual(
            missing_check_cites("- pytest tests/ -q → green", checks),
            ["py_compile"],
        )
        self.assertEqual(
            missing_check_cites(
                "- pytest green\n- py_compile clean", checks
            ),
            [],
        )

    def test_result_signal(self) -> None:
        self.assertTrue(verification_missing_result_signal("- ran pytest"))
        self.assertFalse(verification_missing_result_signal("- pytest green"))
        self.assertFalse(verification_missing_result_signal("12 passed"))
        self.assertFalse(verification_missing_result_signal("0 failed"))

    def test_no_registry_is_noop(self) -> None:
        body = (
            "Completed:\n- x\n\nVerification:\nok\n\n"
            "Links:\n- abc1234\n\nFollow-ups:\nnone"
        )
        self.assertIsNone(
            closeout_checks_violation(body, product="worklane", registry={})
        )

    def test_registered_requires_cite_and_result(self) -> None:
        entry = ProductCloseoutChecks(
            product="worklane",
            checks=(CloseoutCheck(id="pytest", tokens=("pytest",)),),
            exempt_labels=frozenset({"docs", "research"}),
        )
        reg = {"worklane": entry}
        bare = (
            "Completed:\n- x\n\nVerification:\nok\n\n"
            "Links:\n- abc1234\n\nFollow-ups:\nnone"
        )
        err = closeout_checks_violation(
            bare, product="worklane", registry=reg
        )
        self.assertIsNotNone(err)
        assert err is not None
        self.assertTrue(err.startswith(CHECKS_HINT_PREFIX))
        self.assertIn("pytest", err)

        good = (
            "Completed:\n- x\n\nVerification:\n- pytest tests/ -q green\n\n"
            "Links:\n- abc1234\n\nFollow-ups:\nnone"
        )
        self.assertIsNone(
            closeout_checks_violation(good, product="worklane", registry=reg)
        )

    def test_exempt_labels_skip(self) -> None:
        entry = ProductCloseoutChecks(
            product="worklane",
            checks=(CloseoutCheck(id="pytest", tokens=("pytest",)),),
            exempt_labels=frozenset({"docs"}),
        )
        reg = {"worklane": entry}
        body = (
            "Completed:\n- x\n\nVerification:\nprose only\n\n"
            "Links:\n- abc1234\n\nFollow-ups:\nnone"
        )
        self.assertIsNone(
            closeout_checks_violation(
                body, product="worklane", labels=["docs"], registry=reg
            )
        )

    def test_load_registry_from_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "closeout_checks.json"
            path.write_text(
                json.dumps(
                    {
                        "worklane": {
                            "checks": [
                                {
                                    "id": "pytest",
                                    "tokens": ["pytest"],
                                    "description": "suite",
                                }
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            reg = load_closeout_checks_registry(path)
            self.assertIn("worklane", reg)
            self.assertEqual(reg["worklane"].checks[0].id, "pytest")
            # default exempt set when omitted
            self.assertIn("docs", reg["worklane"].exempt_labels)

    def test_verification_field_helper(self) -> None:
        entry = ProductCloseoutChecks(
            product="tradeos",
            checks=(CloseoutCheck(id="pytest", tokens=()),),
            exempt_labels=frozenset(),
        )
        reg = {"tradeos": entry}
        err = verification_checks_violation(
            "just prose", product="tradeos", registry=reg
        )
        self.assertIsNotNone(err)
        self.assertIsNone(
            verification_checks_violation(
                "pytest 0 failed", product="tradeos", registry=reg
            )
        )


def _make_env(tmp: Path) -> None:
    (tmp / "data").mkdir(parents=True, exist_ok=True)
    (tmp / "config").mkdir(parents=True, exist_ok=True)
    os.environ["WORKLANE_RUNTIME_DIR"] = str(tmp)
    os.environ["WORKLANE_DB"] = str(tmp / "data" / "tradeos.db")
    os.environ.pop("TRADEOS_TRACKER_DB", None)
    os.environ["TRADEOS_TICKETS_SOURCE"] = "sqlite"
    os.environ["WL_DEFAULT_PRODUCT"] = "tradeos"
    os.environ.pop("WL_DEFAULT_PROJECT", None)
    os.environ.pop("WL_PRODUCT", None)
    os.environ.pop("WL_PROJECT", None)


def _write_checks(tmp: Path, product: str = "tradeos") -> None:
    path = tmp / "config" / "closeout_checks.json"
    path.write_text(
        json.dumps(
            {
                product: {
                    "checks": [
                        {
                            "id": "pytest",
                            "tokens": ["pytest"],
                        }
                    ]
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )


_ENV_KEYS = (
    "WORKLANE_RUNTIME_DIR",
    "WORKLANE_DB",
    "TRADEOS_TRACKER_DB",
    "TRADEOS_TICKETS_SOURCE",
    "WL_DEFAULT_PROJECT",
    "WL_DEFAULT_PRODUCT",
    "WL_PROJECT",
    "WL_PRODUCT",
)


class CloseoutChecksHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        from fastapi import FastAPI

        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._env_before = {k: os.environ.get(k) for k in _ENV_KEYS}
        _make_env(self.root)
        _write_checks(self.root)
        from worklane.task_server import router  # noqa: PLC0415

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

    def _mk_task(self, labels=None) -> str:
        payload = {
            "title": "closeout checks guard",
            "description": "Problem: x. Expected: y.",
            "author": "lili",
        }
        if labels is not None:
            payload["labels"] = labels
        r = self.client.post("/api/admin/tasks", json=payload)
        self.assertEqual(r.status_code, 200, msg=r.text)
        return r.json()["task"]["id"]

    def test_api_rejects_missing_check_cite(self) -> None:
        tid = self._mk_task()
        body = (
            "Completed:\n- x\n\nVerification:\nok\n\n"
            "Links:\n- abc1234 worklane/closeout_checks.py\n\nFollow-ups:\nnone"
        )
        r = self.client.post(
            f"/api/admin/tasks/{tid}/comments",
            json={"body": body, "author": "lili"},
        )
        self.assertEqual(r.status_code, 400, msg=r.text)
        self.assertIn("registered close-out check", r.json()["error"])

    def test_api_accepts_cited_check(self) -> None:
        tid = self._mk_task()
        body = (
            "Completed:\n- x\n\nVerification:\n- pytest -q green\n\n"
            "Links:\n- abc1234 worklane/closeout_checks.py\n\nFollow-ups:\nnone"
        )
        r = self.client.post(
            f"/api/admin/tasks/{tid}/comments",
            json={"body": body, "author": "lili"},
        )
        self.assertEqual(r.status_code, 200, msg=r.text)

    def test_api_docs_label_exempt(self) -> None:
        tid = self._mk_task(labels=["docs"])
        body = (
            "Completed:\n- taught PROCESS\n\nVerification:\nprose only\n\n"
            "Links:\n- abc1234 PROTOCOL.md\n\nFollow-ups:\nnone"
        )
        r = self.client.post(
            f"/api/admin/tasks/{tid}/comments",
            json={"body": body, "author": "lili"},
        )
        self.assertEqual(r.status_code, 200, msg=r.text)

    def test_api_no_file_unchanged(self) -> None:
        # Remove registry — must behave as pre-wl-339.
        (self.root / "config" / "closeout_checks.json").unlink()
        tid = self._mk_task()
        body = (
            "Completed:\n- x\n\nVerification:\nok\n\n"
            "Links:\n- abc1234\n\nFollow-ups:\nnone"
        )
        r = self.client.post(
            f"/api/admin/tasks/{tid}/comments",
            json={"body": body, "author": "lili"},
        )
        self.assertEqual(r.status_code, 200, msg=r.text)


class CloseoutChecksMcpTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._env_before = {k: os.environ.get(k) for k in _ENV_KEYS}
        _make_env(self.root)
        _write_checks(self.root)
        SQLiteTracker(
            db_path=self.root / "data" / "worklane.db"
        ).create_task(
            title="seed",
            description="bootstrap for discovery",
        )
        self.h = TPHandlers(author="lili", default_product="tradeos")

    def tearDown(self) -> None:
        for k, v in self._env_before.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    def test_tp_close_rejects_uncited(self) -> None:
        created = self.h.wl_create(
            title="uncited close",
            description="Problem: missing cite. Expected: reject.",
        )
        tid = created["task"]["id"]
        self.h.wl_claim(tid)
        with self.assertRaises(ToolError) as ctx:
            self.h.wl_close(
                tid,
                completed="- x",
                verification="- ran tests",
                links="- abc1234 worklane/closeout_checks.py",
            )
        self.assertIn("registered close-out check", ctx.exception.message)

    def test_tp_close_accepts_cite(self) -> None:
        created = self.h.wl_create(
            title="cited close",
            description="Problem: cite. Expected: accept.",
        )
        tid = created["task"]["id"]
        self.h.wl_claim(tid)
        closed = self.h.wl_close(
            tid,
            completed="- x",
            verification="- pytest tests/ -q green",
            links="- abc1234 worklane/closeout_checks.py",
        )
        self.assertTrue(closed["ok"])
        self.assertEqual(closed["task"]["status"], "done")


if __name__ == "__main__":
    unittest.main()
