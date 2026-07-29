"""wl-281: label mutations never leave a live ticket silently unrouted.

pc-603 incident (2026-07-28): ticket created WITH worker:carl (no stamp,
correct), label removed seconds later — ticket sat ready with neither a
worker:* seat nor needs:routing. The re-stamp only ran on create.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from worklane.routing_labels import (
    NEEDS_ROUTING_LABEL,
    reconcile_routing_after_mutation,
)
from worklane.trackers.protocol import TaskStatus
from worklane.trackers.sqlite import SQLiteTracker


class ReconcileRoutingAfterMutationTest(unittest.TestCase):
    def test_stamps_when_live_and_no_worker(self) -> None:
        labs, stamped, dropped = reconcile_routing_after_mutation(["suite"])
        self.assertTrue(stamped)
        self.assertFalse(dropped)
        self.assertIn(NEEDS_ROUTING_LABEL, labs)

    def test_idempotent_when_stamp_already_present(self) -> None:
        labs, stamped, dropped = reconcile_routing_after_mutation(
            ["suite", NEEDS_ROUTING_LABEL]
        )
        self.assertFalse(stamped)
        self.assertFalse(dropped)
        self.assertEqual(labs.count(NEEDS_ROUTING_LABEL), 1)

    def test_drops_stamp_when_worker_present(self) -> None:
        labs, stamped, dropped = reconcile_routing_after_mutation(
            ["worker:carl", NEEDS_ROUTING_LABEL, "suite"]
        )
        self.assertFalse(stamped)
        self.assertTrue(dropped)
        self.assertNotIn(NEEDS_ROUTING_LABEL, labs)
        self.assertIn("worker:carl", labs)

    def test_no_stamp_when_not_live(self) -> None:
        labs, stamped, dropped = reconcile_routing_after_mutation(
            ["suite"], live=False
        )
        self.assertFalse(stamped)
        self.assertNotIn(NEEDS_ROUTING_LABEL, labs)

    # -- pc-621 regression: string input must never be char-iterated ---------

    def test_string_labels_with_worker_drops_stamp(self) -> None:
        labs, stamped, dropped = reconcile_routing_after_mutation(
            "worker:carl,suite," + NEEDS_ROUTING_LABEL
        )
        self.assertFalse(stamped)
        self.assertTrue(dropped)
        self.assertIn("worker:carl", labs)
        self.assertNotIn(NEEDS_ROUTING_LABEL, labs)

    def test_string_labels_no_worker_stamps(self) -> None:
        labs, stamped, dropped = reconcile_routing_after_mutation("area,suite")
        self.assertTrue(stamped)
        self.assertFalse(dropped)
        self.assertIn(NEEDS_ROUTING_LABEL, labs)


class UpdateLabelsRestampTest(unittest.TestCase):
    """Tracker-level regression: the pc-603 create-with-worker → remove path."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wl_routing_update_")
        db = Path(self._tmp.name) / "data" / "tradeos.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        self.tracker = SQLiteTracker(db_path=db)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_removing_last_worker_restamps_needs_routing(self) -> None:
        # pc-603 repro: created with a seat, seat stripped later.
        t = self.tracker.create_task(
            title="soak umbrella",
            description="x",
            labels=["worker:carl", "suite"],
        )
        updated = self.tracker.update_labels(
            str(t.id), remove=["worker:carl"], actor="test"
        )
        assert updated is not None
        self.assertNotIn("worker:carl", updated.labels)
        self.assertIn(NEEDS_ROUTING_LABEL, updated.labels)

    def test_adding_worker_drops_needs_routing(self) -> None:
        t = self.tracker.create_task(
            title="unrouted",
            description="x",
            labels=[NEEDS_ROUTING_LABEL, "suite"],
        )
        updated = self.tracker.update_labels(
            str(t.id), add=["worker:carl"], actor="test"
        )
        assert updated is not None
        self.assertIn("worker:carl", updated.labels)
        self.assertNotIn(NEEDS_ROUTING_LABEL, updated.labels)

    def test_swapping_worker_never_stamps(self) -> None:
        t = self.tracker.create_task(
            title="reroute",
            description="x",
            labels=["worker:carl"],
        )
        updated = self.tracker.update_labels(
            str(t.id), add=["worker:tess"], remove=["worker:carl"], actor="test"
        )
        assert updated is not None
        self.assertIn("worker:tess", updated.labels)
        self.assertNotIn(NEEDS_ROUTING_LABEL, updated.labels)

    def test_stripping_stamp_from_unrouted_live_ticket_is_noop(self) -> None:
        t = self.tracker.create_task(
            title="unrouted",
            description="x",
            labels=[NEEDS_ROUTING_LABEL],
        )
        updated = self.tracker.update_labels(
            str(t.id), remove=[NEEDS_ROUTING_LABEL], actor="test"
        )
        assert updated is not None
        self.assertIn(NEEDS_ROUTING_LABEL, updated.labels)

    def test_done_ticket_not_stamped_on_label_removal(self) -> None:
        t = self.tracker.create_task(
            title="shipped",
            description="x",
            labels=["worker:carl"],
        )
        self.tracker.update_status(str(t.id), TaskStatus.DONE)
        updated = self.tracker.update_labels(
            str(t.id), remove=["worker:carl"], actor="test"
        )
        assert updated is not None
        self.assertNotIn(NEEDS_ROUTING_LABEL, updated.labels)


if __name__ == "__main__":
    unittest.main()
