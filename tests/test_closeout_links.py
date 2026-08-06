"""wl-396 / wf-171 — Links must cite a landing commit SHA on close-out."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from worklane.closeout_links import (
    LINKS_SHA_HINT,
    closeout_links_violation,
    extract_links_section,
    find_commit_shas,
    links_missing_landing_sha,
)
from worklane.mcp.handlers import TPHandlers, ToolError
from worklane.trackers.sqlite import SQLiteTracker


class CloseoutLinksUnitTests(unittest.TestCase):
    def test_find_commit_shas(self) -> None:
        text = "see abc1234 and deadbeefcafe0123456789abcdef01234567"
        shas = find_commit_shas(text)
        self.assertEqual(shas[0], "abc1234")
        self.assertIn("deadbeefcafe0123456789abcdef01234567", shas)
        # 6-hex too short; ticket ids not hex-only
        self.assertEqual(find_commit_shas("abc123 path/to/file wl-396"), [])

    def test_extract_links_section(self) -> None:
        body = (
            "Completed:\n- x\n\nVerification:\nok\n\n"
            "Links:\n- abc1234 worklane/closeout_links.py\n\n"
            "Follow-ups:\nnone"
        )
        sec = extract_links_section(body)
        self.assertIn("abc1234", sec)
        self.assertNotIn("Follow-ups", sec)
        self.assertNotIn("Completed", sec)

    def test_links_missing_landing_sha(self) -> None:
        self.assertIsNone(links_missing_landing_sha("- abc1234 PROTOCOL.md"))
        err = links_missing_landing_sha("- PROTOCOL.md only")
        self.assertIsNotNone(err)
        assert err is not None
        self.assertIn("landing commit SHA", err)

    def test_closeout_links_violation(self) -> None:
        good = (
            "Completed:\n- x\n\nVerification:\nok\n\n"
            "Links:\n- abc1234\n\nFollow-ups:\nnone"
        )
        self.assertIsNone(closeout_links_violation(good))
        bad = (
            "Completed:\n- x\n\nVerification:\nok\n\n"
            "Links:\n- worklane/foo.py\n\nFollow-ups:\nnone"
        )
        self.assertEqual(closeout_links_violation(bad), LINKS_SHA_HINT)
        self.assertIsNone(closeout_links_violation("Owner: lili\nPlan:\n- x"))


def _make_env(tmp: Path) -> None:
    (tmp / "data").mkdir(parents=True, exist_ok=True)
    os.environ["WORKLANE_RUNTIME_DIR"] = str(tmp)
    os.environ["WORKLANE_DB"] = str(tmp / "data" / "tradeos.db")
    os.environ.pop("TRADEOS_TRACKER_DB", None)
    os.environ["TRADEOS_TICKETS_SOURCE"] = "sqlite"
    os.environ["WL_DEFAULT_PRODUCT"] = "tradeos"
    os.environ.pop("WL_DEFAULT_PROJECT", None)
    os.environ.pop("WL_PRODUCT", None)
    os.environ.pop("WL_PROJECT", None)


class CloseoutLinksHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        from fastapi import FastAPI

        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
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
            )
        }
        _make_env(self.root)
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

    def _mk_task(self) -> str:
        r = self.client.post(
            "/api/admin/tasks",
            json={
                "title": "closeout sha guard",
                "description": "Problem: x. Expected: y.",
                "author": "lili",
            },
        )
        self.assertEqual(r.status_code, 200, msg=r.text)
        return r.json()["task"]["id"]

    def test_api_rejects_path_only_links(self) -> None:
        tid = self._mk_task()
        body = (
            "Completed:\n- x\n\nVerification:\nok\n\n"
            "Links:\n- worklane/closeout_links.py\n\nFollow-ups:\nnone"
        )
        r = self.client.post(
            f"/api/admin/tasks/{tid}/comments",
            json={"body": body, "author": "lili"},
        )
        self.assertEqual(r.status_code, 400, msg=r.text)
        self.assertIn("landing commit SHA", r.json()["error"])

    def test_api_accepts_sha_in_links(self) -> None:
        tid = self._mk_task()
        body = (
            "Completed:\n- x\n\nVerification:\nok\n\n"
            "Links:\n- abc1234 worklane/closeout_links.py\n\nFollow-ups:\nnone"
        )
        r = self.client.post(
            f"/api/admin/tasks/{tid}/comments",
            json={"body": body, "author": "lili"},
        )
        self.assertEqual(r.status_code, 200, msg=r.text)


class CloseoutLinksMcpTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._env_before = {
            k: os.environ.get(k)
            for k in (
                "WORKLANE_RUNTIME_DIR",
                "WORKLANE_DB",
                "TRADEOS_TRACKER_DB",
                "TRADEOS_TICKETS_SOURCE",
                "WL_PROJECT",
                "WL_PRODUCT",
            )
        }
        _make_env(self.root)
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

    def test_tp_close_rejects_path_only(self) -> None:
        created = self.h.wl_create(
            title="path only close",
            description="Problem: path. Expected: reject.",
        )
        tid = created["task"]["id"]
        self.h.wl_claim(tid)
        with self.assertRaises(ToolError) as ctx:
            self.h.wl_close(
                tid,
                completed="- x",
                verification="- y",
                links="- worklane/closeout_links.py",
            )
        self.assertIn("landing commit SHA", ctx.exception.message)

    def test_tp_close_accepts_sha(self) -> None:
        created = self.h.wl_create(
            title="sha close",
            description="Problem: sha. Expected: accept.",
        )
        tid = created["task"]["id"]
        self.h.wl_claim(tid)
        closed = self.h.wl_close(
            tid,
            completed="- x",
            verification="- y",
            links="- abc1234 worklane/closeout_links.py",
        )
        self.assertTrue(closed["ok"])
        self.assertEqual(closed["task"]["status"], "done")


if __name__ == "__main__":
    unittest.main()
