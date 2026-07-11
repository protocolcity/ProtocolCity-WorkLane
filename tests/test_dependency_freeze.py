"""Dependency-freeze behavior for local SQLite tracker.

When a parent ticket is ``in_progress``, dependent backlog tickets are
auto-frozen into ``in_review`` to avoid premature pickup. They thaw back
to backlog when blockers resolve.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from worklane.trackers.protocol import TaskStatus
from worklane.trackers.sqlite import SQLiteTracker


class TestDependencyFreeze(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory(prefix="tracker_dep_freeze_")
        self.db_path = Path(self.tmpdir.name) / "tickets.db"
        self.tracker = SQLiteTracker(db_path=self.db_path)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_parent_in_progress_freezes_dependent(self) -> None:
        parent = self.tracker.create_task(title="Parent work")
        child = self.tracker.create_task(
            title="Child depends on parent",
            description=f"Depends on #{parent.id}",
        )

        self.tracker.update_status(parent.id, TaskStatus.IN_PROGRESS)
        child_now = self.tracker.get_task(child.id)
        self.assertIsNotNone(child_now)
        self.assertEqual(child_now.status, TaskStatus.IN_REVIEW)
        self.assertIn("queue:frozen-dependency", child_now.labels)

    def test_parent_done_thaws_dependency_frozen_ticket(self) -> None:
        parent = self.tracker.create_task(title="Parent work")
        child = self.tracker.create_task(
            title="Child depends on parent",
            description=f"Depends on #{parent.id}",
        )

        self.tracker.update_status(parent.id, TaskStatus.IN_PROGRESS)
        self.tracker.update_status(parent.id, TaskStatus.DONE)

        child_now = self.tracker.get_task(child.id)
        self.assertIsNotNone(child_now)
        self.assertEqual(child_now.status, TaskStatus.BACKLOG)
        self.assertNotIn("queue:frozen-dependency", child_now.labels)

    def test_claim_blocked_ticket_keeps_it_frozen(self) -> None:
        blocker = self.tracker.create_task(title="Blocking task")
        dep = self.tracker.create_task(
            title="Blocked task",
            description=f"Depends on #{blocker.id}",
        )

        dep_now = self.tracker.update_status(dep.id, TaskStatus.IN_PROGRESS)
        self.assertIsNotNone(dep_now)
        self.assertEqual(dep_now.status, TaskStatus.IN_REVIEW)
        self.assertIn("queue:frozen-dependency", dep_now.labels)

    def test_completion_handoff_comment_auto_moves_done(self) -> None:
        task = self.tracker.create_task(title="Finish me")
        self.tracker.update_status(task.id, TaskStatus.IN_PROGRESS)
        self.tracker.add_comment(
            task.id,
            (
                "Completed:\n"
                "- wired endpoint\n\n"
                "Verification:\n"
                "- tests pass\n"
            ),
            author="agent",
        )
        now_task = self.tracker.get_task(task.id)
        self.assertIsNotNone(now_task)
        self.assertEqual(now_task.status, TaskStatus.DONE)

    def test_blocked_handoff_comment_auto_requeues_backlog(self) -> None:
        task = self.tracker.create_task(title="Might stall")
        self.tracker.update_status(task.id, TaskStatus.IN_PROGRESS)
        self.tracker.add_comment(
            task.id,
            (
                "Blocked: waiting on API contract.\n"
                "Next step: update schema and retry."
            ),
            author="agent",
        )
        now_task = self.tracker.get_task(task.id)
        self.assertIsNotNone(now_task)
        self.assertEqual(now_task.status, TaskStatus.BACKLOG)

    def test_owner_marker_reserves_backlog_ticket_and_freezes_dependents(self) -> None:
        parent = self.tracker.create_task(title="Parent")
        child = self.tracker.create_task(
            title="Child depends on parent",
            description=f"Depends on #{parent.id}",
        )
        self.tracker.add_comment(
            parent.id,
            (
                "Owner: agent-1\n"
                "Start: 2026-04-16T01:30:00Z\n"
                "Plan: ship parser change"
            ),
            author="agent-1",
        )
        parent_now = self.tracker.get_task(parent.id)
        child_now = self.tracker.get_task(child.id)
        self.assertIsNotNone(parent_now)
        self.assertIsNotNone(child_now)
        # Owner/Start/Plan marker on a backlog ticket reserves it into
        # in_review (it leaves the free pool); the agent promotes to
        # in_progress explicitly when coding starts (PROCESS.md §2).
        self.assertEqual(parent_now.status, TaskStatus.IN_REVIEW)
        self.assertEqual(child_now.status, TaskStatus.IN_REVIEW)
        self.assertIn("queue:frozen-dependency", child_now.labels)


if __name__ == "__main__":
    unittest.main()
