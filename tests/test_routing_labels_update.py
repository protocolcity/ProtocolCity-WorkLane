"""wl-281: label mutations never leave a live ticket silently unrouted.

pc-603 incident (2026-07-28): ticket created WITH worker:carl (no stamp,
correct), label removed seconds later — ticket sat ready with neither a
worker:* seat nor needs:routing. The re-stamp only ran on create.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from worklane.routing_labels import (
    NEEDS_ROUTING_LABEL,
    check_mutation_foreign_seat,
    check_mutation_starve_guard,
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
        self.assertFalse(dropped)
        self.assertNotIn(NEEDS_ROUTING_LABEL, labs)

    def test_drops_stamp_when_not_live(self) -> None:
        """wl-439: terminal (not live) drops needs:routing residue."""
        labs, stamped, dropped = reconcile_routing_after_mutation(
            ["suite", NEEDS_ROUTING_LABEL], live=False
        )
        self.assertFalse(stamped)
        self.assertTrue(dropped)
        self.assertNotIn(NEEDS_ROUTING_LABEL, labs)
        self.assertIn("suite", labs)

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


class CheckMutationForeignSeatTest(unittest.TestCase):
    """wl-372: label mutations must not introduce a seat not hired here."""

    def test_add_foreign_seat_rejects(self) -> None:
        err = check_mutation_foreign_seat(
            ["worker:lili", "suite"],
            add=["worker:sylvester"],
            remove=["worker:lili"],
            hired_hands=["worker:lili"],
        )
        self.assertIsNotNone(err)
        self.assertIn("worker:sylvester", err or "")
        self.assertIn("not a hired seat", err or "")

    def test_add_local_seat_ok(self) -> None:
        err = check_mutation_foreign_seat(
            ["worker:lili"],
            add=["worker:drew"],
            remove=["worker:lili"],
            hired_hands=["worker:lili", "worker:drew"],
        )
        self.assertIsNone(err)

    def test_worker_you_never_foreign(self) -> None:
        err = check_mutation_foreign_seat(
            ["worker:lili"],
            add=["worker:you", "you:host"],
            remove=["worker:lili"],
            hired_hands=["worker:lili"],
        )
        self.assertIsNone(err)

    def test_pre_hire_skips(self) -> None:
        err = check_mutation_foreign_seat(
            ["suite"],
            add=["worker:ghost"],
            hired_hands=[],
        )
        self.assertIsNone(err)


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

    def test_status_done_strips_needs_routing(self) -> None:
        """wl-439: transition → done drops needs:routing in the same write."""
        t = self.tracker.create_task(
            title="unrouted close",
            description="x",
            labels=[NEEDS_ROUTING_LABEL, "suite"],
        )
        self.assertIn(NEEDS_ROUTING_LABEL, t.labels)
        updated = self.tracker.update_status(str(t.id), TaskStatus.DONE)
        assert updated is not None
        self.assertEqual(updated.status, TaskStatus.DONE)
        self.assertNotIn(NEEDS_ROUTING_LABEL, updated.labels)
        self.assertIn("suite", updated.labels)

    def test_status_canceled_strips_needs_routing(self) -> None:
        """wl-439: transition → canceled drops needs:routing in the same write."""
        t = self.tracker.create_task(
            title="unrouted cancel",
            description="x",
            labels=[NEEDS_ROUTING_LABEL, "hygiene"],
        )
        updated = self.tracker.update_status(str(t.id), TaskStatus.CANCELED)
        assert updated is not None
        self.assertEqual(updated.status, TaskStatus.CANCELED)
        self.assertNotIn(NEEDS_ROUTING_LABEL, updated.labels)
        self.assertIn("hygiene", updated.labels)

    def test_open_status_keeps_needs_routing(self) -> None:
        """wl-439: open transitions never strip needs:routing."""
        t = self.tracker.create_task(
            title="still open",
            description="x",
            labels=[NEEDS_ROUTING_LABEL, "suite"],
        )
        for status in (
            TaskStatus.IN_PROGRESS,
            TaskStatus.IN_REVIEW,
            TaskStatus.BACKLOG,
        ):
            updated = self.tracker.update_status(str(t.id), status)
            assert updated is not None
            self.assertIn(NEEDS_ROUTING_LABEL, updated.labels)

    def test_terminal_strip_keeps_foreign_worker_seat(self) -> None:
        """wl-439: status strip bypasses label guards; foreign seat stays.

        Doctor residue case (wf-193 / ts-2218): terminal ticket may still
        carry worker:<foreign> + needs:routing. Transition strip must drop
        only the stamp without going through foreign-seat mutation guards.
        """
        t = self.tracker.create_task(
            title="foreign residue",
            description="x",
            labels=["worker:kc", "suite"],
        )
        # create-path drops redundant needs:routing when worker present;
        # seed residue shape that doctor previously had to repair.
        with self.tracker._connect() as conn:
            conn.execute(
                "UPDATE tasks SET labels = ? WHERE id = ?",
                (
                    json.dumps(["worker:kc", NEEDS_ROUTING_LABEL, "suite"]),
                    int(t.id),
                ),
            )
            conn.commit()
        updated = self.tracker.update_status(str(t.id), TaskStatus.DONE)
        assert updated is not None
        self.assertEqual(updated.status, TaskStatus.DONE)
        self.assertNotIn(NEEDS_ROUTING_LABEL, updated.labels)
        self.assertIn("worker:kc", updated.labels)
        self.assertIn("suite", updated.labels)


class CheckMutationStarveGuardTest(unittest.TestCase):
    """wl-320: label mutations must not bypass the wl-315 starve guard."""

    _HIRED = ["worker:lili", "worker:terra"]

    def test_bare_worker_you_added_when_hired_is_error(self) -> None:
        err = check_mutation_starve_guard(
            ["worker:lili"],
            add=["worker:you"],
            remove=["worker:lili"],
            hired_hands=self._HIRED,
        )
        self.assertIsNotNone(err)
        self.assertIn("starves", (err or "").lower())

    def test_worker_you_with_you_kind_is_allowed(self) -> None:
        err = check_mutation_starve_guard(
            ["worker:lili"],
            add=["worker:you", "you:note"],
            remove=["worker:lili"],
            hired_hands=self._HIRED,
        )
        self.assertIsNone(err)

    def test_worker_you_with_founder_gate_is_allowed(self) -> None:
        err = check_mutation_starve_guard(
            ["worker:lili"],
            add=["worker:you", "gate:founder"],
            remove=["worker:lili"],
            hired_hands=self._HIRED,
        )
        self.assertIsNone(err)

    def test_no_hired_hands_no_error(self) -> None:
        err = check_mutation_starve_guard(
            [],
            add=["worker:you"],
            hired_hands=None,
        )
        self.assertIsNone(err)

    def test_removing_you_kind_from_worker_you_ticket_is_error(self) -> None:
        # Existing: worker:you + you:note. Remove you:note → bare worker:you.
        err = check_mutation_starve_guard(
            ["worker:you", "you:note"],
            remove=["you:note"],
            hired_hands=self._HIRED,
        )
        self.assertIsNotNone(err)
        self.assertIn("starves", (err or "").lower())

    def test_no_worker_you_no_error(self) -> None:
        err = check_mutation_starve_guard(
            ["worker:lili"],
            add=["worker:terra"],
            remove=["worker:lili"],
            hired_hands=self._HIRED,
        )
        self.assertIsNone(err)

    def test_already_classified_worker_you_retained(self) -> None:
        # worker:you + you:todo already present; add another label — no error.
        err = check_mutation_starve_guard(
            ["worker:you", "you:todo"],
            add=["priority:high"],
            hired_hands=self._HIRED,
        )
        self.assertIsNone(err)


if __name__ == "__main__":
    unittest.main()
