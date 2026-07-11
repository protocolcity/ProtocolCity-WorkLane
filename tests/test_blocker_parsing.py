"""Blocker-declaration parsing — regression suite for #834 false positives.

Only explicit declarations may freeze a ticket:

- heading sections titled with a blocker keyword,
- inline ``Depends on #N`` / ``Blocked by #N`` / line-leading ``Blockers: #N``.

Prose mentions ("requires" mid-sentence, ``Related:`` refs, ``epic:#N``
membership refs) must parse to zero blockers. Each incident test names the
tradeOS ticket that hit it live.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from worklane.trackers.protocol import TaskStatus
from worklane.trackers.sqlite import SQLiteTracker, _parse_blockers


class TestParseBlockers(unittest.TestCase):
    # ── declarations that must count ─────────────────────────────────

    def test_inline_depends_on(self) -> None:
        self.assertEqual(_parse_blockers("Depends on #12"), ["12"])

    def test_inline_depends_on_ref_run(self) -> None:
        self.assertEqual(
            _parse_blockers("Depends on #12, #13 and #14"), ["12", "13", "14"]
        )

    def test_inline_blocked_by(self) -> None:
        self.assertEqual(_parse_blockers("Blocked by #7."), ["7"])

    def test_line_leading_blockers_colon(self) -> None:
        self.assertEqual(_parse_blockers("Blockers: #3, #4"), ["3", "4"])

    def test_heading_section_sweeps_body(self) -> None:
        text = "## Dependencies\n#5 loader rework\n#6 schema bump\n## Notes\n#99"
        self.assertEqual(_parse_blockers(text), ["5", "6"])

    def test_seo_refs_supported(self) -> None:
        self.assertEqual(_parse_blockers("Depends on SEO-42"), ["SEO-42"])

    # ── prose that must NOT count ────────────────────────────────────

    def test_requires_in_prose_is_not_a_blocker(self) -> None:
        # tradeOS #820: froze on "requires" appearing mid-sentence.
        text = "This work requires #807 groundwork and touches #805 output."
        self.assertEqual(_parse_blockers(text), [])

    def test_single_paragraph_dependencies_prose_is_not_a_blocker(self) -> None:
        # tradeOS #809: "Dependencies: ..." mid-paragraph swept every ref,
        # including Related:/context mentions; epic froze three times.
        text = (
            "Concept work. Dependencies: #805 learn-loop (evidence base), "
            "#801 retune campaign, #803 (tuning MCP auth). "
            "Related: #388 AI pillar, #793."
        )
        self.assertEqual(_parse_blockers(text), [])

    def test_epic_membership_ref_is_not_a_blocker(self) -> None:
        # tradeOS #816: "epic:#808" counted as a blocker — deadlock, the
        # epic can never close before its phase tickets.
        self.assertEqual(_parse_blockers("Blocked by epic:#808"), [])
        self.assertEqual(_parse_blockers("Phase 2 of epic:#808. Depends on #810."), ["810"])

    def test_epic_ref_inside_heading_section_is_not_a_blocker(self) -> None:
        text = "## Dependencies\nepic:#808 umbrella\n#5 real dep"
        self.assertEqual(_parse_blockers(text), ["5"])

    def test_ref_run_stops_at_first_non_ref_token(self) -> None:
        # Annotated prose after the declared run stays out.
        text = "Depends on #805 learn-loop evidence, #801 retune campaign"
        self.assertEqual(_parse_blockers(text), ["805"])

    def test_related_and_context_refs_alone_never_block(self) -> None:
        self.assertEqual(_parse_blockers("Related: #1, #2. See also #3."), [])

    def test_empty_and_none(self) -> None:
        self.assertEqual(_parse_blockers(""), [])
        self.assertEqual(_parse_blockers(None), [])  # type: ignore[arg-type]


class TestGuardIntegration(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory(prefix="tracker_blockers_")
        self.tracker = SQLiteTracker(db_path=Path(self.tmpdir.name) / "tickets.db")

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_prose_mention_does_not_freeze_dependent(self) -> None:
        # The #809 shape end-to-end: claiming the mentioned ticket must not
        # state-flip the parked one.
        anchor = self.tracker.create_task(title="Retune campaign")
        parked = self.tracker.create_task(
            title="Parked epic",
            description=(
                f"Big vision. Dependencies: #{anchor.id} retune campaign "
                "(first manual reps). Related: #999."
            ),
        )
        self.tracker.update_status(anchor.id, TaskStatus.IN_PROGRESS)
        parked_now = self.tracker.get_task(parked.id)
        self.assertEqual(parked_now.status, TaskStatus.BACKLOG)
        self.assertNotIn("queue:frozen-dependency", parked_now.labels)

    def test_declared_dependent_still_freezes_and_thaws(self) -> None:
        anchor = self.tracker.create_task(title="Parent")
        child = self.tracker.create_task(
            title="Child", description=f"Depends on #{anchor.id}"
        )
        self.tracker.update_status(anchor.id, TaskStatus.IN_PROGRESS)
        self.assertEqual(
            self.tracker.get_task(child.id).status, TaskStatus.IN_REVIEW
        )
        self.tracker.update_status(anchor.id, TaskStatus.DONE)
        child_now = self.tracker.get_task(child.id)
        self.assertEqual(child_now.status, TaskStatus.BACKLOG)
        self.assertNotIn("queue:frozen-dependency", child_now.labels)

    def test_prose_frozen_ticket_thaws_after_fix(self) -> None:
        # Tickets frozen by the old parser carry the label but now parse to
        # zero blockers — the next lifecycle event must release them.
        victim = self.tracker.create_task(
            title="Frozen by old parser",
            description="This requires #777 context.",
        )
        with self.tracker._connect() as conn:  # simulate legacy freeze state
            with conn:
                conn.execute(
                    "UPDATE tasks SET status = ?, labels = ? WHERE id = ?",
                    (
                        TaskStatus.IN_REVIEW,
                        '["queue:frozen-dependency"]',
                        int(victim.id),
                    ),
                )
        other = self.tracker.create_task(title="Any lifecycle event")
        self.tracker.update_status(other.id, TaskStatus.IN_PROGRESS)
        victim_now = self.tracker.get_task(victim.id)
        self.assertEqual(victim_now.status, TaskStatus.BACKLOG)
        self.assertNotIn("queue:frozen-dependency", victim_now.labels)


if __name__ == "__main__":
    unittest.main()
