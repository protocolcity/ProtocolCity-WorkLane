"""Structured relations + ready/explain + backfill (wl-20).

Fixture SQLite DBs only — never the live product stores / tasks.db.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from worklane import relations as relmod
from worklane.trackers.protocol import TaskStatus
from worklane.trackers.sqlite import SQLiteTracker


class RelationsCRUDTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wl_relations_")
        self.db = Path(self._tmp.name) / "fixture.db"
        self.tracker = SQLiteTracker(db_path=self.db, product_default="")
        self.a = self.tracker.create_task(title="A", description="anchor")
        self.b = self.tracker.create_task(title="B", description="dependent")
        self.c = self.tracker.create_task(title="C", description="third")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_create_list_delete_round_trip(self) -> None:
        r = relmod.create_relation(
            self.db, self.a.id, self.b.id, "blocks"
        )
        self.assertEqual(r.from_id, str(self.a.id))
        self.assertEqual(r.to_id, str(self.b.id))
        self.assertEqual(r.relation_type, "blocks")
        self.assertTrue(r.id)

        listed = relmod.list_relations(self.db, task_id=self.b.id)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0].id, r.id)

        # Touch either endpoint.
        self.assertEqual(len(relmod.list_relations(self.db, task_id=self.a.id)), 1)

        ok = relmod.delete_relation(self.db, r.id)
        self.assertTrue(ok)
        self.assertEqual(relmod.list_relations(self.db), [])

    def test_duplicate_rejected(self) -> None:
        relmod.create_relation(self.db, self.a.id, self.b.id, "related")
        with self.assertRaises(relmod.RelationError):
            relmod.create_relation(self.db, self.a.id, self.b.id, "related")

    def test_unknown_type_rejected(self) -> None:
        with self.assertRaises(relmod.RelationError):
            relmod.create_relation(self.db, self.a.id, self.b.id, "depends-on")

    def test_missing_task_rejected(self) -> None:
        with self.assertRaises(relmod.RelationError):
            relmod.create_relation(self.db, self.a.id, "99999", "blocks")

    def test_parent_child_and_discovered(self) -> None:
        p = relmod.create_relation(
            self.db, self.a.id, self.b.id, "parent-child"
        )
        d = relmod.create_relation(
            self.db, self.a.id, self.c.id, "discovered-from"
        )
        self.assertEqual(p.relation_type, "parent-child")
        self.assertEqual(d.relation_type, "discovered-from")


class CycleDetectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wl_rel_cycle_")
        self.db = Path(self._tmp.name) / "fixture.db"
        self.tracker = SQLiteTracker(db_path=self.db, product_default="")
        self.a = self.tracker.create_task(title="A")
        self.b = self.tracker.create_task(title="B")
        self.c = self.tracker.create_task(title="C")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_direct_cycle_rejected(self) -> None:
        relmod.create_relation(self.db, self.a.id, self.b.id, "blocks")
        with self.assertRaises(relmod.RelationError) as ctx:
            relmod.create_relation(self.db, self.b.id, self.a.id, "blocks")
        self.assertIn("cycle", str(ctx.exception).lower())

    def test_transitive_cycle_rejected(self) -> None:
        relmod.create_relation(self.db, self.a.id, self.b.id, "blocks")
        relmod.create_relation(self.db, self.b.id, self.c.id, "blocks")
        with self.assertRaises(relmod.RelationError):
            relmod.create_relation(self.db, self.c.id, self.a.id, "blocks")

    def test_parent_child_participates_in_cycles(self) -> None:
        relmod.create_relation(self.db, self.a.id, self.b.id, "parent-child")
        relmod.create_relation(self.db, self.b.id, self.c.id, "parent-child")
        with self.assertRaises(relmod.RelationError):
            relmod.create_relation(self.db, self.c.id, self.a.id, "parent-child")

    def test_related_does_not_cycle_check(self) -> None:
        # related is informational — mutual related is fine.
        relmod.create_relation(self.db, self.a.id, self.b.id, "related")
        r2 = relmod.create_relation(self.db, self.b.id, self.a.id, "related")
        self.assertEqual(r2.relation_type, "related")

    def test_self_edge_blocks_rejected(self) -> None:
        with self.assertRaises(relmod.RelationError):
            relmod.create_relation(self.db, self.a.id, self.a.id, "blocks")

    def test_would_create_cycle_pure(self) -> None:
        edges = {"1": {"2"}, "2": {"3"}}
        self.assertTrue(relmod.would_create_cycle(edges, "3", "1"))
        self.assertFalse(relmod.would_create_cycle(edges, "1", "3"))
        self.assertTrue(relmod.would_create_cycle(edges, "1", "1"))


class ReadyExplainTest(unittest.TestCase):
    def test_no_relations_backlog_is_ready(self) -> None:
        status = {"1": "backlog", "2": "backlog"}
        explained = relmod.explain_ready(["1", "2"], status, [])
        self.assertTrue(explained["1"].ready)
        self.assertTrue(explained["2"].ready)
        self.assertEqual(explained["1"].blocked_by, [])

    def test_blocked_until_blocker_done(self) -> None:
        rels = [
            relmod.Relation(
                id="1", from_id="1", to_id="2", relation_type="blocks"
            )
        ]
        status = {"1": "in_progress", "2": "backlog"}
        explained = relmod.explain_ready(["1", "2"], status, rels)
        # in_progress is not a dispatch-ready status even with no blockers
        self.assertFalse(explained["1"].ready)
        self.assertEqual(explained["1"].blocked_by, [])
        self.assertFalse(explained["2"].ready)
        self.assertEqual(explained["2"].blocked_by, ["1"])

        status["1"] = "done"
        explained = relmod.explain_ready(["1", "2"], status, rels)
        self.assertTrue(explained["2"].ready)
        self.assertEqual(explained["2"].blocked_by, [])

    def test_canceled_blocker_counts_as_resolved(self) -> None:
        rels = [
            relmod.Relation(
                id="1", from_id="1", to_id="2", relation_type="blocks"
            )
        ]
        status = {"1": "canceled", "2": "backlog"}
        explained = relmod.explain_ready(["2"], status, rels)
        self.assertTrue(explained["2"].ready)

    def test_unknown_blocker_still_blocks(self) -> None:
        rels = [
            relmod.Relation(
                id="1", from_id="99", to_id="2", relation_type="blocks"
            )
        ]
        status = {"2": "backlog"}
        explained = relmod.explain_ready(["2"], status, rels)
        self.assertFalse(explained["2"].ready)
        self.assertEqual(explained["2"].blocked_by, ["99"])

    def test_parent_child_does_not_block_ready(self) -> None:
        rels = [
            relmod.Relation(
                id="1",
                from_id="1",
                to_id="2",
                relation_type="parent-child",
            )
        ]
        status = {"1": "backlog", "2": "backlog"}
        explained = relmod.explain_ready(["2"], status, rels)
        self.assertTrue(explained["2"].ready)

    def test_non_backlog_not_ready_even_if_unblocked(self) -> None:
        status = {"1": "in_progress"}
        explained = relmod.explain_ready(["1"], status, [])
        self.assertFalse(explained["1"].ready)

    def test_ready_task_ids_helper(self) -> None:
        rels = [
            relmod.Relation(
                id="1", from_id="1", to_id="2", relation_type="blocks"
            )
        ]
        status = {"1": "backlog", "2": "backlog", "3": "backlog"}
        ready = relmod.ready_task_ids(["1", "2", "3"], status, rels)
        self.assertEqual(ready, ["1", "3"])


class BackfillParserTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wl_rel_bf_")
        self.db = Path(self._tmp.name) / "fixture.db"
        self.tracker = SQLiteTracker(db_path=self.db, product_default="")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_depends_on_becomes_blocks(self) -> None:
        anchor = self.tracker.create_task(title="Parent")
        child = self.tracker.create_task(
            title="Child",
            description=f"Depends on #{anchor.id}",
        )
        planned = relmod.plan_backfill_from_tasks(
            self.tracker.list_tasks(limit=None)
        )
        blocks = [
            p
            for p in planned
            if p.relation_type == "blocks" and p.to_id == str(child.id)
        ]
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].from_id, str(anchor.id))
        self.assertEqual(blocks[0].source, "depends-on")

    def test_parent_and_slice_of_labels(self) -> None:
        parent = self.tracker.create_task(title="Epic-ish")
        child = self.tracker.create_task(
            title="Slice",
            labels=[f"parent:{parent.id}", "area:api"],
        )
        slice_child = self.tracker.create_task(
            title="Slice-of",
            labels=[f"slice-of:#{parent.id}"],
        )
        planned = relmod.plan_backfill_from_tasks(
            [child, slice_child]
        )
        types = {(p.from_id, p.to_id, p.relation_type) for p in planned}
        self.assertIn(
            (str(parent.id), str(child.id), "parent-child"), types
        )
        self.assertIn(
            (str(parent.id), str(slice_child.id), "parent-child"), types
        )

    def test_numeric_epic_label_only(self) -> None:
        epic = self.tracker.create_task(title="Epic ticket")
        phase = self.tracker.create_task(
            title="Phase",
            labels=[f"epic:{epic.id}", "epic:wl-18"],
        )
        planned = relmod.plan_backfill_from_tasks([phase])
        # epic:wl-18 is a membership tag, not a task id — only numeric epic.
        self.assertEqual(len(planned), 1)
        self.assertEqual(planned[0].from_id, str(epic.id))
        self.assertEqual(planned[0].source, "label:epic")

    def test_prose_requires_is_not_a_blocker_edge(self) -> None:
        # Mirrors test_blocker_parsing: mid-sentence "requires" is not a dep.
        t = self.tracker.create_task(
            title="Victim",
            description="This work requires #807 groundwork and touches #805.",
        )
        planned = relmod.plan_backfill_from_tasks([t])
        self.assertEqual(planned, [])

    def test_related_prose_not_backfilled(self) -> None:
        t = self.tracker.create_task(
            title="Context",
            description="Related: #1, #2. See also #3.",
        )
        planned = relmod.plan_backfill_from_tasks([t])
        self.assertEqual(planned, [])

    def test_dry_run_apply_does_not_write(self) -> None:
        anchor = self.tracker.create_task(title="A")
        self.tracker.create_task(
            title="B", description=f"Depends on #{anchor.id}"
        )
        report = relmod.apply_backfill(self.db, dry_run=True)
        self.assertTrue(report.dry_run)
        self.assertEqual(len(report.planned), 1)
        self.assertEqual(report.applied, [])
        self.assertEqual(relmod.list_relations(self.db), [])

    def test_apply_is_idempotent(self) -> None:
        anchor = self.tracker.create_task(title="A")
        self.tracker.create_task(
            title="B", description=f"Blocked by #{anchor.id}."
        )
        r1 = relmod.apply_backfill(self.db, dry_run=False)
        self.assertEqual(len(r1.applied), 1)
        self.assertEqual(len(relmod.list_relations(self.db)), 1)

        r2 = relmod.apply_backfill(self.db, dry_run=False)
        self.assertEqual(len(r2.applied), 0)
        self.assertEqual(len(r2.skipped_existing), 1)
        self.assertEqual(len(relmod.list_relations(self.db)), 1)

    def test_apply_skips_missing_targets(self) -> None:
        self.tracker.create_task(
            title="Orphan dep",
            description="Depends on #99999",
        )
        report = relmod.apply_backfill(self.db, dry_run=False)
        self.assertEqual(report.applied, [])
        self.assertEqual(len(report.skipped_missing), 1)

    def test_end_to_end_ready_after_backfill(self) -> None:
        anchor = self.tracker.create_task(title="A", status=TaskStatus.BACKLOG)
        child = self.tracker.create_task(
            title="B",
            description=f"Depends on #{anchor.id}",
            status=TaskStatus.BACKLOG,
        )
        relmod.apply_backfill(self.db, dry_run=False)
        status = relmod.load_status_map(self.db)
        edges = relmod.list_relations(self.db)
        explained = relmod.explain_ready(
            [str(anchor.id), str(child.id)], status, edges
        )
        self.assertTrue(explained[str(anchor.id)].ready)
        self.assertFalse(explained[str(child.id)].ready)

        self.tracker.update_status(str(anchor.id), TaskStatus.DONE)
        status = relmod.load_status_map(self.db)
        explained = relmod.explain_ready(
            [str(child.id)], status, edges
        )
        self.assertTrue(explained[str(child.id)].ready)


if __name__ == "__main__":
    unittest.main()
