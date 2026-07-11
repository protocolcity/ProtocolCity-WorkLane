"""wl-9: one card renderer — poll fragments match SSR board cards.

The board poll used to re-implement card markup in JS (adminBoardRenderCard).
That dual path drifted (wl-8). Now /api/admin/tasks returns card_html from
_render_task_card — the same function the SSR board embeds — so first paint
and poll repaint are identical by construction.
"""

from __future__ import annotations

import os
import re
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from worklane.trackers.sqlite import SQLiteTracker


def _make_env(tmp: Path) -> None:
    (tmp / "data").mkdir(parents=True, exist_ok=True)
    os.environ["WORKLANE_RUNTIME_DIR"] = str(tmp)
    os.environ["WORKLANE_DB"] = str(tmp / "data" / "tradeos.db")
    os.environ.pop("TRADEOS_TRACKER_DB", None)
    os.environ["TRADEOS_TICKETS_SOURCE"] = "sqlite"
    os.environ["WL_DEFAULT_PRODUCT"] = "tradeos"
    os.environ.pop("WL_PRODUCT", None)


def _extract_card(html: str, task_id: str) -> str:
    """Pull one <article class='tb-card…'>…</article> by data-task-id."""
    # Non-greedy match; cards do not nest articles.
    pat = (
        r"<article class='tb-card[^']*'[^>]*"
        r"data-task-id='" + re.escape(task_id) + r"'.*?</article>"
    )
    m = re.search(pat, html, flags=re.DOTALL)
    if not m:
        raise AssertionError(
            "card for %r not found in HTML (len=%d)" % (task_id, len(html))
        )
    return m.group(0)


class CardRendererConformanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wl_card_conf_")
        self.root = Path(self._tmp.name)
        self._env_before = {
            k: os.environ.get(k)
            for k in (
                "WORKLANE_RUNTIME_DIR",
                "WORKLANE_DB",
                "TRADEOS_TRACKER_DB",
                "TRADEOS_TICKETS_SOURCE",
                "WL_DEFAULT_PRODUCT",
                "WL_PRODUCT",
            )
        }
        _make_env(self.root)
        self.db_path = self.root / "data" / "tradeos.db"
        self.tracker = SQLiteTracker(db_path=self.db_path)

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

    def test_poll_card_html_equals_ssr_board_card(self) -> None:
        t = self.tracker.create_task(
            title="Conformance card fixture",
            description="wl-9 dual-path guard",
            labels=["area:board", "product:tradeos", "lane:grok", "needs:decision"],
            priority=2,
        )
        self.tracker.add_comment(
            t.id,
            "Owner: grok\nPlan:\n- ship one renderer",
            author="grok",
        )
        composite_id = "t-%s" % t.id

        board = self.client.get("/admin/tickets/tradeos", params={"view": "board"})
        self.assertEqual(board.status_code, 200)
        ssr_card = _extract_card(board.text, composite_id)

        poll = self.client.get(
            "/api/admin/tasks",
            params={
                "with_preview": "1",
                "product": "tradeos",
                "limit": "50",
            },
        )
        self.assertEqual(poll.status_code, 200)
        payload = poll.json()
        self.assertTrue(payload.get("ok"))
        tasks = payload.get("tasks") or []
        match = next((row for row in tasks if row.get("id") == composite_id), None)
        self.assertIsNotNone(match, "fixture task missing from poll payload")
        assert match is not None
        card_html = match.get("card_html") or ""
        self.assertTrue(card_html.strip(), "poll task lacks card_html")
        self.assertEqual(
            card_html,
            ssr_card,
            "poll fragment must equal SSR board card byte-for-byte",
        )

    def test_card_html_present_without_preview(self) -> None:
        t = self.tracker.create_task(title="No preview card", description="x")
        composite_id = "t-%s" % t.id
        r = self.client.get(
            "/api/admin/tasks",
            params={"product": "tradeos", "limit": "20"},
        )
        self.assertEqual(r.status_code, 200)
        row = next(
            (x for x in r.json().get("tasks") or [] if x.get("id") == composite_id),
            None,
        )
        self.assertIsNotNone(row)
        assert row is not None
        self.assertIn("card_html", row)
        self.assertIn("tb-card", row["card_html"])
        self.assertIn(composite_id, row["card_html"])


if __name__ == "__main__":
    unittest.main()
