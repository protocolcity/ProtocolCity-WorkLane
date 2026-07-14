"""wl-104: claim age + staleness hint on in_progress/in_review cards.

The board byline (wl-54) already shows *who* claimed a ticket. This adds
*when* (age since the winning Owner: marker's own timestamp, not just the
latest comment) and a staleness hint when no comment has landed since that
claim past a configurable threshold — signal for a stale claim, not a claim
about process state.
"""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone

from worklane.board import (
    _claim_stale_minutes,
    _extract_owner_claim,
    _render_task_card,
    _owner_claim_html,
)
from worklane.trackers.protocol import Task, TaskStatus


class _Comment:
    def __init__(self, body: str, created_at: str) -> None:
        self.body = body
        self.created_at = created_at


class ExtractOwnerClaimTest(unittest.TestCase):
    def test_claim_timestamp_is_the_owner_comments_own_timestamp(self) -> None:
        comments = [
            _Comment("Owner: grok (grok-4)\nStart: x", "2026-07-01T00:00:00+00:00"),
            _Comment("progress update, no marker", "2026-07-02T00:00:00+00:00"),
        ]
        owner, claimed_at = _extract_owner_claim(comments)
        self.assertEqual(owner, "grok")
        self.assertEqual(claimed_at, "2026-07-01T00:00:00+00:00")

    def test_no_marker_returns_empty_pair(self) -> None:
        self.assertEqual(
            _extract_owner_claim([_Comment("hi", "2026-07-01T00:00:00+00:00")]),
            ("", ""),
        )


class OwnerClaimHtmlTest(unittest.TestCase):
    NOW = datetime(2026, 7, 13, 12, 0, 0, tzinfo=timezone.utc)

    def test_backlog_and_done_get_no_age_or_stale(self) -> None:
        old = (self.NOW - timedelta(hours=5)).isoformat()
        preview = {"owner": "grok", "author": "grok", "owner_claimed_at": old,
                   "created_at": old}
        for status in (TaskStatus.BACKLOG, TaskStatus.DONE):
            html = _owner_claim_html(
                Task(id="1", title="t", status=status), preview, now=self.NOW
            )
            self.assertIn("grok", html)
            self.assertNotIn("tb-card-claim-age", html)
            self.assertNotIn("tb-card-stale", html)

    def test_inflight_recent_claim_shows_age_not_stale(self) -> None:
        recent = (self.NOW - timedelta(minutes=5)).isoformat()
        preview = {"owner": "grok", "author": "grok", "owner_claimed_at": recent,
                   "created_at": recent}
        html = _owner_claim_html(
            Task(id="1", title="t", status=TaskStatus.IN_PROGRESS),
            preview, now=self.NOW,
        )
        self.assertIn("tb-card-claim-age", html)
        self.assertNotIn("tb-card-stale", html)

    def test_inflight_old_claim_with_no_comment_since_is_stale(self) -> None:
        old = (self.NOW - timedelta(minutes=120)).isoformat()
        preview = {"owner": "grok", "author": "grok", "owner_claimed_at": old,
                   "created_at": old}
        html = _owner_claim_html(
            Task(id="1", title="t", status=TaskStatus.IN_REVIEW),
            preview, now=self.NOW,
        )
        self.assertIn("tb-card-stale", html)
        self.assertIn("stale claim", html)

    def test_inflight_old_claim_with_activity_since_is_not_stale(self) -> None:
        old = (self.NOW - timedelta(minutes=120)).isoformat()
        newer = (self.NOW - timedelta(minutes=1)).isoformat()
        preview = {"owner": "grok", "author": "grok", "owner_claimed_at": old,
                   "created_at": newer}
        html = _owner_claim_html(
            Task(id="1", title="t", status=TaskStatus.IN_PROGRESS),
            preview, now=self.NOW,
        )
        self.assertIn("tb-card-claim-age", html)
        self.assertNotIn("tb-card-stale", html)

    def test_no_owner_no_html(self) -> None:
        html = _owner_claim_html(
            Task(id="1", title="t", status=TaskStatus.IN_PROGRESS),
            {"owner": "", "author": "", "created_at": ""}, now=self.NOW,
        )
        self.assertEqual(html, "")


class ClaimStaleMinutesTest(unittest.TestCase):
    def setUp(self) -> None:
        self._prev = os.environ.get("TICKETING_CLAIM_STALE_MINUTES")

    def tearDown(self) -> None:
        if self._prev is None:
            os.environ.pop("TICKETING_CLAIM_STALE_MINUTES", None)
        else:
            os.environ["TICKETING_CLAIM_STALE_MINUTES"] = self._prev

    def test_default_is_90(self) -> None:
        os.environ.pop("TICKETING_CLAIM_STALE_MINUTES", None)
        self.assertEqual(_claim_stale_minutes(), 90)

    def test_env_override(self) -> None:
        os.environ["TICKETING_CLAIM_STALE_MINUTES"] = "30"
        self.assertEqual(_claim_stale_minutes(), 30)

    def test_invalid_env_falls_back_to_default(self) -> None:
        os.environ["TICKETING_CLAIM_STALE_MINUTES"] = "not-a-number"
        self.assertEqual(_claim_stale_minutes(), 90)


class CardStaleBadgeTest(unittest.TestCase):
    def test_card_renders_stale_badge_for_dead_claim(self) -> None:
        # _render_task_card doesn't thread a `now` through to
        # _owner_claim_html, so it resolves the real wall-clock — anchor
        # the fixture to actual now rather than a fixed fictional date.
        old = (datetime.now(timezone.utc) - timedelta(minutes=200)).isoformat()
        task = Task(id="7", title="t", status=TaskStatus.IN_PROGRESS)
        html = _render_task_card(
            task, {"owner": "grok", "author": "grok", "owner_claimed_at": old,
                   "created_at": old},
        )
        self.assertIn("tb-card-stale", html)


if __name__ == "__main__":
    unittest.main()
