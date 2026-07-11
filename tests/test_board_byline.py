"""wl-54: board card byline — worker identity on every column.

The chip used to render only on in_progress cards, keyed off the latest
comment author. Now the newest ``Owner:`` marker wins, signed-but-unknown
ids render verbatim, and the SSR card shows the byline in every status.
"""

from __future__ import annotations

import unittest

from worklane.board import (
    _detect_worker,
    _extract_owner,
    _render_task_card,
)
from worklane.trackers.protocol import Task, TaskStatus


class _Comment:
    def __init__(self, body: str, created_at: str) -> None:
        self.body = body
        self.created_at = created_at


class ExtractOwnerTest(unittest.TestCase):
    def test_newest_owner_marker_wins(self) -> None:
        comments = [
            _Comment("Owner: cursor\nStart: x", "2026-07-01T00:00:00Z"),
            _Comment("Owner: work-pool (claude-fable-5)\nPlan:\n- x",
                     "2026-07-02T00:00:00Z"),
            _Comment("Completed:\n- done", "2026-07-03T00:00:00Z"),
        ]
        self.assertEqual(_extract_owner(comments), "work-pool")

    def test_model_parenthetical_stripped(self) -> None:
        comments = [_Comment("Owner: grok (grok-4)", "2026-07-01T00:00:00Z")]
        self.assertEqual(_extract_owner(comments), "grok")

    def test_no_marker(self) -> None:
        self.assertEqual(_extract_owner([_Comment("hi", "2026-07-01")]), "")


class DetectWorkerTest(unittest.TestCase):
    """wl-84: every identity renders verbatim from store data — the byline
    carries no baked-in agent roster or per-agent decoration."""

    def test_owner_beats_latest_author(self) -> None:
        got = _detect_worker(
            {"owner": "agent-a", "author": "agent-b", "body": "Completed:"}
        )
        self.assertEqual(got, ("·", "agent-a"))

    def test_signed_author_when_no_owner(self) -> None:
        got = _detect_worker({"owner": "", "author": "agent-b", "body": ""})
        self.assertEqual(got, ("·", "agent-b"))

    def test_unsigned_owner_line_fallback(self) -> None:
        got = _detect_worker(
            {"owner": "", "author": "", "body": "Owner: agent-c (model-x)\nPlan:"}
        )
        self.assertEqual(got, ("·", "agent-c"))

    def test_nothing_detected(self) -> None:
        self.assertIsNone(_detect_worker({"owner": "", "author": "", "body": "hi"}))


class CardBylineTest(unittest.TestCase):
    def _card(self, status: str) -> str:
        task = Task(id="7", title="t", status=status)
        return _render_task_card(
            task, {"owner": "grok", "author": "grok", "body": "Completed:"}
        )

    def test_byline_on_backlog_and_done(self) -> None:
        for status in (TaskStatus.BACKLOG, TaskStatus.DONE,
                       TaskStatus.IN_PROGRESS, TaskStatus.IN_REVIEW):
            self.assertIn("tb-card-worker", self._card(status), status)


if __name__ == "__main__":
    unittest.main()
