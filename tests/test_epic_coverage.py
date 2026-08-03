"""wl-347: epic child-coverage guard (pc-978 gap)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from worklane.epic_coverage import (
    body_is_done_closeout,
    coverage_block_reason,
    extract_ticket_ids_from_line,
    is_epic_wrapper,
    parse_children_section,
    parent_label_forms,
)
from worklane.trackers.protocol import TaskStatus
from worklane.trackers.sqlite import SQLiteTracker


class ParseHelpersTest(unittest.TestCase):
    def test_is_wrapper_by_label(self) -> None:
        self.assertTrue(is_epic_wrapper(["umbrella", "worker:lili"]))
        self.assertTrue(is_epic_wrapper(["epic"]))
        self.assertFalse(is_epic_wrapper(["worker:lili", "process"]))

    def test_is_wrapper_by_gate_note(self) -> None:
        self.assertTrue(
            is_epic_wrapper([], gate_note="umbrella — not claimable")
        )
        self.assertFalse(is_epic_wrapper([], gate_note="thaw next week"))

    def test_extract_composite_and_hash(self) -> None:
        self.assertEqual(
            extract_ticket_ids_from_line("- [ ] wl-12: ship RUNNING copy"),
            ["wl-12"],
        )
        self.assertEqual(
            extract_ticket_ids_from_line("Depends on #44 and pc-9"),
            ["pc-9", "44"],
        )

    def test_parse_children_section_requires_ids(self) -> None:
        desc = (
            "## Glance\nparent epic\n\n"
            "## Children\n"
            "- [ ] wl-1: template\n"
            "- [ ] public RUNNING copy  # missing id\n"
            "- [x] #2 ladder\n\n"
            "## Done when\n- all green\n"
        )
        rows = parse_children_section(desc)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0][1], ["wl-1"])
        self.assertEqual(rows[1][1], [])
        self.assertEqual(rows[2][1], ["2"])

    def test_parse_no_section(self) -> None:
        self.assertEqual(parse_children_section("## Done when\n- ship it"), [])

    def test_parent_label_forms(self) -> None:
        forms = parent_label_forms("347", product_prefix="wl")
        self.assertIn("347", forms)
        self.assertIn("wl-347", forms)
        forms2 = parent_label_forms("wl-347")
        self.assertIn("wl-347", forms2)
        self.assertIn("347", forms2)

    def test_body_is_done_closeout(self) -> None:
        self.assertTrue(
            body_is_done_closeout(
                "Completed:\nx\n\nVerification:\nok\n\nLinks:\na\n\nFollow-ups:\nnone"
            )
        )
        self.assertFalse(body_is_done_closeout("Owner: lili\nPlan:\n- x"))


class CoverageBlockTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "t.db"
        self.tracker = SQLiteTracker(db_path=self.db)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_non_wrapper_never_blocks(self) -> None:
        t = self.tracker.create_task(
            title="ordinary",
            description="## Children\n- [ ] no id here\n",
            labels=["worker:lili"],
        )
        self.assertIsNone(coverage_block_reason(t, self.tracker, db_path=self.db))

    def test_idless_children_row_blocks(self) -> None:
        t = self.tracker.create_task(
            title="epic",
            description=(
                "## Children\n"
                "- [ ] wl-999: filed\n"
                "- [ ] public RUNNING.md copy never filed\n"
            ),
            labels=["umbrella", "epic"],
            status=TaskStatus.IN_PROGRESS,
        )
        # even with wl-999 missing, idless row is the first complaint
        err = coverage_block_reason(t, self.tracker, db_path=self.db)
        self.assertIsNotNone(err)
        assert err is not None
        self.assertIn("without a ticket id", err)

    def test_unknown_cited_id_blocks(self) -> None:
        t = self.tracker.create_task(
            title="epic",
            description="## Children\n- [ ] 9999: ghost child\n",
            labels=["umbrella"],
            status=TaskStatus.IN_PROGRESS,
        )
        err = coverage_block_reason(t, self.tracker, db_path=self.db)
        self.assertIsNotNone(err)
        assert err is not None
        self.assertIn("unknown", err)

    def test_open_labeled_child_blocks(self) -> None:
        parent = self.tracker.create_task(
            title="parent epic",
            labels=["umbrella"],
            status=TaskStatus.IN_PROGRESS,
        )
        child = self.tracker.create_task(
            title="child slice",
            labels=[f"parent:{parent.id}"],
            status=TaskStatus.BACKLOG,
        )
        err = coverage_block_reason(
            parent, self.tracker, db_path=self.db, product_prefix="wl"
        )
        self.assertIsNotNone(err)
        assert err is not None
        self.assertIn(str(child.id), err)
        self.assertIn("still open", err)

    def test_done_children_and_valid_section_allows(self) -> None:
        parent = self.tracker.create_task(
            title="parent epic",
            labels=["umbrella"],
            status=TaskStatus.IN_PROGRESS,
        )
        child = self.tracker.create_task(
            title="child slice",
            labels=[f"parent:{parent.id}"],
            status=TaskStatus.DONE,
        )
        # refresh parent description with Children citing the real child
        self.tracker.update_task(
            parent.id,
            description=f"## Children\n- [x] {child.id}: child slice\n",
        )
        parent = self.tracker.get_task(parent.id)
        assert parent is not None
        err = coverage_block_reason(
            parent, self.tracker, db_path=self.db, product_prefix="wl"
        )
        self.assertIsNone(err)

    def test_wrapper_without_children_allows(self) -> None:
        # free-form Done-when only — no ## Children, no parent: kids
        t = self.tracker.create_task(
            title="legacy epic",
            description="## Done when\n- ship law text\n- publish\n",
            labels=["umbrella"],
            status=TaskStatus.IN_PROGRESS,
        )
        self.assertIsNone(coverage_block_reason(t, self.tracker, db_path=self.db))


if __name__ == "__main__":
    unittest.main()
