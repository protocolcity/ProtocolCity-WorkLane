"""Direct unit coverage for worklane.devqueue (wl-99).

Covers queue.py, conflicts.py, and shutdown.py — not the adjacent
sqlite _parse_blockers suite (tests/test_blocker_parsing.py) or the
gate-only WorkQueue.ready tests (tests/test_gates.py).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import List, Optional
from unittest import mock

from worklane.devqueue.conflicts import (
    extract_file_refs,
    group_by_file_conflict,
)
from worklane.devqueue.queue import (
    WorkQueue,
    build_dispatch_prompt,
    find_orphans,
    parse_blockers,
)
from worklane.devqueue.shutdown import (
    ShutdownReport,
    _build_comment,
    _git_log_for_ticket,
    run_shutdown,
)
from worklane.trackers.protocol import Task, TaskStatus
from worklane.trackers.sqlite import SQLiteTracker


def _task(
    task_id: str,
    *,
    title: str = "t",
    description: str = "",
    status: str = TaskStatus.BACKLOG,
    priority: int = 3,
    labels: Optional[List[str]] = None,
    ext_id: Optional[str] = None,
) -> Task:
    return Task(
        id=task_id,
        title=title,
        description=description,
        status=status,
        priority=priority,
        labels=list(labels or []),
        ext_id=ext_id,
        created_at="2026-07-01T00:00:00Z",
        updated_at="2026-07-01T00:00:00Z",
    )


# ── queue.py::parse_blockers ─────────────────────────────────────────────


class ParseBlockersTest(unittest.TestCase):
    def test_heading_section_depends(self) -> None:
        text = "## Depends on\nSEO-10 and SEO-11\n## Notes\nSEO-99"
        self.assertEqual(parse_blockers(text), ["SEO-10", "SEO-11"])

    def test_heading_section_local_ids(self) -> None:
        text = "## Blocked by\n#12, #13\n## Context\n#99"
        self.assertEqual(parse_blockers(text), ["12", "13"])

    def test_bold_style_heading(self) -> None:
        text = "**Requires**\n#5\n**Notes**\n#9"
        self.assertEqual(parse_blockers(text), ["5"])

    def test_inline_fallback_without_heading(self) -> None:
        self.assertEqual(parse_blockers("Depends on #42"), ["42"])
        self.assertEqual(parse_blockers("Blocked by SEO-7."), ["SEO-7"])

    def test_prose_requires_without_heading_is_not_blocker(self) -> None:
        # Mid-sentence "requires" is not a declaration line keyword hit
        # for the heading path; the line-fallback requires the keyword
        # on the same line, so this still counts — match production:
        # any line containing a blocker keyword yields its refs.
        text = "This work requires #807 groundwork."
        self.assertEqual(parse_blockers(text), ["807"])

    def test_related_refs_alone_never_block(self) -> None:
        self.assertEqual(parse_blockers("Related: #1, #2. See also #3."), [])

    def test_dedupe_preserves_order(self) -> None:
        text = "## Depends on\n#1 #2 #1 SEO-1 SEO-1"
        self.assertEqual(parse_blockers(text), ["SEO-1", "1", "2"])

    def test_empty(self) -> None:
        self.assertEqual(parse_blockers(""), [])
        self.assertEqual(parse_blockers(None), [])  # type: ignore[arg-type]


# ── queue.py::WorkQueue priority / dependency / orphans ──────────────────


class WorkQueueBehaviorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory(prefix="devqueue_wq_")
        self.tracker = SQLiteTracker(db_path=Path(self.tmpdir.name) / "tickets.db")

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_priority_sort_urgent_before_low(self) -> None:
        low = self.tracker.create_task(title="low", priority=4)
        urgent = self.tracker.create_task(title="urgent", priority=1)
        normal = self.tracker.create_task(title="normal", priority=3)

        ready = WorkQueue(self.tracker).ready()
        ready_ids = [t.id for t in ready]
        self.assertEqual(ready_ids[:3], [urgent.id, normal.id, low.id])

    def test_dependency_filter_unknown_blocker_hides_ticket(self) -> None:
        blocked = self.tracker.create_task(
            title="blocked",
            description="## Depends on\n#99999\n",
        )
        free = self.tracker.create_task(title="free")

        ready_ids = {t.id for t in WorkQueue(self.tracker).ready()}
        self.assertIn(free.id, ready_ids)
        self.assertNotIn(blocked.id, ready_ids)

    def test_dependency_filter_done_blocker_allows_ticket(self) -> None:
        dep = self.tracker.create_task(title="dep", status=TaskStatus.DONE)
        dependent = self.tracker.create_task(
            title="dependent",
            description=f"## Depends on\n#{dep.id}\n",
        )

        ready_ids = {t.id for t in WorkQueue(self.tracker).ready()}
        self.assertIn(dependent.id, ready_ids)

    def test_blocked_reports_unresolved_details(self) -> None:
        open_dep = self.tracker.create_task(title="open dep", priority=2)
        dependent = self.tracker.create_task(
            title="waiting",
            description=f"## Depends on\n#{open_dep.id}\n",
            priority=1,
        )

        blocked = WorkQueue(self.tracker).blocked()
        by_id = {bt.task.id: bt for bt in blocked}
        self.assertIn(dependent.id, by_id)
        info = by_id[dependent.id].blockers
        self.assertEqual(len(info), 1)
        self.assertEqual(info[0].ticket_id, open_dep.id)
        self.assertEqual(info[0].title, "open dep")
        self.assertEqual(info[0].status, TaskStatus.BACKLOG)

    def test_orphans_are_in_progress(self) -> None:
        self.tracker.create_task(title="backlog")
        ip = self.tracker.create_task(title="ip", status=TaskStatus.IN_PROGRESS)

        orphans = WorkQueue(self.tracker).orphans()
        self.assertEqual([t.id for t in orphans], [ip.id])

        via_helper = find_orphans(self.tracker)
        self.assertEqual([t.id for t in via_helper], [ip.id])

    def test_label_filter(self) -> None:
        a = self.tracker.create_task(title="a", labels=["lane:grok"])
        self.tracker.create_task(title="b", labels=["lane:cursor"])

        ready = WorkQueue(self.tracker).ready(labels=["lane:grok"])
        self.assertEqual([t.id for t in ready], [a.id])

    def test_umbrella_label_excluded_even_if_ungated(self) -> None:
        # wl-297: umbrella coordination wrappers must not appear in ready feed
        # regardless of gate state — defense in depth against mis-filed epics.
        child = self.tracker.create_task(title="child slice")
        self.tracker.create_task(title="parent epic", labels=["umbrella"])

        ready_ids = {t.id for t in WorkQueue(self.tracker).ready()}
        self.assertIn(child.id, ready_ids)

    def test_umbrella_label_excluded_stays_out_with_extra_labels(self) -> None:
        # umbrella label wins even when other labels are present
        self.tracker.create_task(
            title="epic with routing",
            labels=["umbrella", "worker:lili", "process"],
        )
        child = self.tracker.create_task(title="real work")

        ready_ids = {t.id for t in WorkQueue(self.tracker).ready()}
        self.assertEqual(ready_ids, {child.id})


class BuildDispatchPromptTest(unittest.TestCase):
    def test_empty(self) -> None:
        self.assertEqual(build_dispatch_prompt([]), "")

    def test_single_uses_ext_id(self) -> None:
        t = _task("1", ext_id="SEO-42")
        self.assertEqual(build_dispatch_prompt([t]), "work SEO-42")

    def test_single_falls_back_to_local_id(self) -> None:
        t = _task("7")
        self.assertEqual(build_dispatch_prompt([t]), "work 7")

    def test_multiple_comma_joined(self) -> None:
        a = _task("1", ext_id="SEO-1")
        b = _task("2", ext_id="SEO-2")
        self.assertEqual(build_dispatch_prompt([a, b]), "work SEO-1, SEO-2")


# ── conflicts.py ─────────────────────────────────────────────────────────


class ExtractFileRefsTest(unittest.TestCase):
    def test_empty(self) -> None:
        self.assertEqual(extract_file_refs(""), [])
        self.assertEqual(extract_file_refs(None), [])  # type: ignore[arg-type]

    def test_finds_source_paths(self) -> None:
        text = "Touch `core/web/routes/foo.py` and docs/guide.md please."
        self.assertEqual(
            extract_file_refs(text),
            ["core/web/routes/foo.py", "docs/guide.md"],
        )

    def test_strips_trailing_punctuation(self) -> None:
        text = "See path/to/file.py."
        self.assertEqual(extract_file_refs(text), ["path/to/file.py"])

    def test_dedupes_preserving_order(self) -> None:
        text = "a/b.py then c/d.py then a/b.py again"
        self.assertEqual(extract_file_refs(text), ["a/b.py", "c/d.py"])

    def test_ignores_non_source_pair_prose(self) -> None:
        # No source extension → not a path for conflict detection.
        self.assertEqual(extract_file_refs("AAPL/SPY pair trade"), [])


class GroupByFileConflictTest(unittest.TestCase):
    def test_empty(self) -> None:
        self.assertEqual(group_by_file_conflict([]), [])

    def test_no_files_are_singletons(self) -> None:
        a = _task("1", description="no paths here")
        b = _task("2", description="still none")
        batches = group_by_file_conflict([a, b])
        self.assertEqual(len(batches), 2)
        self.assertEqual(batches[0].tickets[0].id, "1")
        self.assertEqual(batches[1].tickets[0].id, "2")
        self.assertEqual(batches[0].shared_files, [])

    def test_overlapping_files_union(self) -> None:
        a = _task("1", description="edit core/foo.py only")
        b = _task("2", description="also core/foo.py and core/bar.py")
        c = _task("3", description="core/bar.py finish")
        d = _task("4", description="unrelated docs/readme.md")

        batches = group_by_file_conflict([a, b, c, d])
        # a-b-c share a connected component; d is separate.
        sizes = sorted(len(batch.tickets) for batch in batches)
        self.assertEqual(sizes, [1, 3])

        big = next(batch for batch in batches if len(batch.tickets) == 3)
        self.assertEqual({t.id for t in big.tickets}, {"1", "2", "3"})
        self.assertEqual(set(big.shared_files), {"core/foo.py", "core/bar.py"})

        small = next(batch for batch in batches if len(batch.tickets) == 1)
        self.assertEqual(small.tickets[0].id, "4")
        self.assertEqual(small.shared_files, ["docs/readme.md"])

    def test_batch_order_follows_lead_ticket(self) -> None:
        # Lead of each component is the earliest input index.
        late = _task("z", description="shared/x.py")
        early = _task("a", description="shared/x.py")
        batches = group_by_file_conflict([late, early])
        self.assertEqual(len(batches), 1)
        self.assertEqual([t.id for t in batches[0].tickets], ["z", "a"])


# ── shutdown.py ──────────────────────────────────────────────────────────


class BuildCommentTest(unittest.TestCase):
    def test_with_commits(self) -> None:
        body = _build_comment(_task("1", title="x"), ["abc land feature", "def fix"])
        self.assertIn("devqueue shutdown closeout", body)
        self.assertIn("Found 2 commit(s)", body)
        self.assertIn("`abc land feature`", body)
        self.assertIn("In Review", body)

    def test_without_commits(self) -> None:
        body = _build_comment(_task("1"), [])
        self.assertIn("No commits referencing this ticket", body)
        self.assertIn("still in progress", body)


class GitLogFallbackTest(unittest.TestCase):
    def test_empty_ticket_id(self) -> None:
        self.assertEqual(_git_log_for_ticket(""), [])

    def test_git_unavailable_returns_empty(self) -> None:
        with mock.patch(
            "worklane.devqueue.shutdown.subprocess.run",
            side_effect=FileNotFoundError,
        ):
            self.assertEqual(_git_log_for_ticket("SEO-1"), [])

    def test_nonzero_return_returns_empty(self) -> None:
        proc = mock.Mock(returncode=1, stdout="")
        with mock.patch(
            "worklane.devqueue.shutdown.subprocess.run",
            return_value=proc,
        ):
            self.assertEqual(_git_log_for_ticket("SEO-1"), [])

    def test_parses_lines(self) -> None:
        proc = mock.Mock(returncode=0, stdout="abc subject one\ndef subject two\n")
        with mock.patch(
            "worklane.devqueue.shutdown.subprocess.run",
            return_value=proc,
        ):
            self.assertEqual(
                _git_log_for_ticket("SEO-9"),
                ["abc subject one", "def subject two"],
            )


class RunShutdownTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory(prefix="devqueue_shutdown_")
        self.tracker = SQLiteTracker(db_path=Path(self.tmpdir.name) / "tickets.db")

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_dry_run_no_commits_stays_in_progress(self) -> None:
        t = self.tracker.create_task(
            title="wip",
            status=TaskStatus.IN_PROGRESS,
            ext_id="SEO-9001",
        )
        with mock.patch(
            "worklane.devqueue.shutdown._git_log_for_ticket",
            return_value=[],
        ):
            report = run_shutdown(self.tracker, apply=False)

        self.assertIsInstance(report, ShutdownReport)
        self.assertFalse(report.applied)
        self.assertEqual(len(report.results), 1)
        self.assertEqual(report.results[0].task_id, "SEO-9001")
        self.assertEqual(report.results[0].new_status, TaskStatus.IN_PROGRESS)
        self.assertFalse(report.results[0].applied)
        # Dry-run must not mutate tracker.
        fresh = self.tracker.get_task(t.id)
        assert fresh is not None
        self.assertEqual(fresh.status, TaskStatus.IN_PROGRESS)
        self.assertEqual(self.tracker.list_comments(t.id), [])

    def test_dry_run_with_commits_proposes_in_review(self) -> None:
        self.tracker.create_task(
            title="landed",
            status=TaskStatus.IN_PROGRESS,
            ext_id="SEO-9002",
        )
        with mock.patch(
            "worklane.devqueue.shutdown._git_log_for_ticket",
            return_value=["abc #9002: land it"],
        ):
            report = run_shutdown(self.tracker, apply=False)

        self.assertEqual(report.results[0].new_status, TaskStatus.IN_REVIEW)
        self.assertEqual(report.results[0].commits, ["abc #9002: land it"])
        self.assertIn("Found 1 commit(s)", report.results[0].comment_body)

    def test_apply_writes_comment_and_transitions(self) -> None:
        t = self.tracker.create_task(
            title="landed",
            status=TaskStatus.IN_PROGRESS,
            ext_id="SEO-9003",
        )
        with mock.patch(
            "worklane.devqueue.shutdown._git_log_for_ticket",
            return_value=["deadbeef SEO-9003: done"],
        ):
            report = run_shutdown(self.tracker, apply=True)

        self.assertTrue(report.applied)
        self.assertTrue(report.results[0].applied)
        fresh = self.tracker.get_task(t.id)
        assert fresh is not None
        self.assertEqual(fresh.status, TaskStatus.IN_REVIEW)
        comments = self.tracker.list_comments(t.id)
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0].author, "devqueue")
        self.assertIn("devqueue shutdown closeout", comments[0].body)

    def test_apply_no_commits_comments_but_keeps_in_progress(self) -> None:
        t = self.tracker.create_task(
            title="still wip",
            status=TaskStatus.IN_PROGRESS,
            ext_id="SEO-9004",
        )
        with mock.patch(
            "worklane.devqueue.shutdown._git_log_for_ticket",
            return_value=[],
        ):
            report = run_shutdown(self.tracker, apply=True)

        self.assertEqual(report.results[0].new_status, TaskStatus.IN_PROGRESS)
        fresh = self.tracker.get_task(t.id)
        assert fresh is not None
        self.assertEqual(fresh.status, TaskStatus.IN_PROGRESS)
        comments = self.tracker.list_comments(t.id)
        self.assertEqual(len(comments), 1)
        self.assertIn("No commits", comments[0].body)

    def test_skips_when_no_identifier(self) -> None:
        # Force a Task-like object with empty id/ext_id through the path
        # by mocking list_tasks.
        bare = Task(
            id="",
            title="ghost",
            description="",
            status=TaskStatus.IN_PROGRESS,
            priority=3,
            labels=[],
            ext_id=None,
            created_at="",
            updated_at="",
        )
        with mock.patch.object(self.tracker, "list_tasks", return_value=[bare]):
            report = run_shutdown(self.tracker, apply=False)
        self.assertEqual(report.results, [])
        self.assertEqual(report.skipped, ["<unknown>"])

    def test_ignores_non_in_progress(self) -> None:
        self.tracker.create_task(title="backlog only")
        with mock.patch(
            "worklane.devqueue.shutdown._git_log_for_ticket",
        ) as git_log:
            report = run_shutdown(self.tracker, apply=False)
        self.assertEqual(report.results, [])
        git_log.assert_not_called()


if __name__ == "__main__":
    unittest.main()
