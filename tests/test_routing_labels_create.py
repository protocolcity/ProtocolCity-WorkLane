"""Create-path: hard B when hands exist; needs:routing only pre-hire."""
from __future__ import annotations

import unittest

from worklane.routing_labels import (
    NEEDS_ROUTING_LABEL,
    ensure_create_labels,
    has_worker_label,
)


class EnsureCreateLabelsTest(unittest.TestCase):
    def test_stamps_needs_routing_when_empty_pre_hire(self) -> None:
        labs, stamped, err = ensure_create_labels([])
        self.assertIsNone(err)
        self.assertTrue(stamped)
        self.assertEqual(labs, [NEEDS_ROUTING_LABEL])

    def test_stamps_when_only_area_labels_pre_hire(self) -> None:
        labs, stamped, err = ensure_create_labels(
            ["suite", "map", "product:protocolcity"]
        )
        self.assertIsNone(err)
        self.assertTrue(stamped)
        self.assertIn(NEEDS_ROUTING_LABEL, labs)
        self.assertIn("suite", labs)

    def test_no_stamp_when_worker_present(self) -> None:
        labs, stamped, err = ensure_create_labels(["worker:trinity", "suite"])
        self.assertIsNone(err)
        self.assertFalse(stamped)
        self.assertEqual(labs, ["worker:trinity", "suite"])
        self.assertTrue(has_worker_label(labs))

    def test_worker_you_is_valid_seat(self) -> None:
        labs, stamped, err = ensure_create_labels(
            ["worker:you", "you:note"],
            hired_hands=["worker:drew"],
        )
        self.assertIsNone(err)
        self.assertFalse(stamped)
        self.assertIn("worker:you", labs)

    def test_worker_you_host_ok_when_hands_exist(self) -> None:
        labs, stamped, err = ensure_create_labels(
            ["worker:you", "you:host", "area:routing"],
            hired_hands=["worker:lili"],
        )
        self.assertIsNone(err)
        self.assertIn("you:host", labs)

    def test_worker_you_founder_gate_ok(self) -> None:
        labs, stamped, err = ensure_create_labels(
            ["worker:you", "gate:founder", "publish"],
            hired_hands=["worker:blossom"],
        )
        self.assertIsNone(err)
        self.assertIn("worker:you", labs)

    def test_bare_worker_you_rejects_when_hands_exist(self) -> None:
        """wl-315: bare worker:you starves cron — require you-kind or founder gate."""
        labs, stamped, err = ensure_create_labels(
            ["worker:you", "suite"],
            hired_hands=["worker:drew", "worker:figaro"],
        )
        self.assertIsNotNone(err)
        self.assertIn("starves", (err or "").lower())
        self.assertIn("you:note", err or "")
        self.assertFalse(stamped)

    def test_bare_worker_you_ok_pre_hire(self) -> None:
        labs, stamped, err = ensure_create_labels(
            ["worker:you"],
            hired_hands=[],
        )
        self.assertIsNone(err)
        self.assertIn("worker:you", labs)

    def test_hard_b_rejects_when_hands_exist(self) -> None:
        labs, stamped, err = ensure_create_labels(
            ["suite"],
            hired_hands=["worker:drew", "worker:riley"],
        )
        self.assertIsNotNone(err)
        self.assertIn("worker:* required", err or "")
        self.assertIn("worker:drew", err or "")
        self.assertIn("worker:you", err or "")
        self.assertFalse(stamped)

    def test_hard_b_allows_worker_when_hands_exist(self) -> None:
        labs, stamped, err = ensure_create_labels(
            ["worker:drew", "suite"],
            hired_hands=["worker:drew"],
        )
        self.assertIsNone(err)
        self.assertFalse(stamped)
        self.assertIn("worker:drew", labs)

    def test_dual_worker_rejected(self) -> None:
        labs, stamped, err = ensure_create_labels(
            ["worker:drew", "worker:riley"]
        )
        self.assertIsNotNone(err)
        self.assertIn("exactly one", err or "")

    def test_drops_needs_routing_when_worker_present(self) -> None:
        labs, stamped, err = ensure_create_labels(
            ["needs:routing", "worker:carl", "suite"]
        )
        self.assertIsNone(err)
        self.assertFalse(stamped)
        self.assertNotIn(NEEDS_ROUTING_LABEL, labs)
        self.assertIn("worker:carl", labs)

    def test_idempotent_needs_routing_pre_hire(self) -> None:
        labs, stamped, err = ensure_create_labels([NEEDS_ROUTING_LABEL])
        self.assertIsNone(err)
        self.assertFalse(stamped)  # already present
        self.assertEqual(labs, [NEEDS_ROUTING_LABEL])

    # -- pc-621 regression: string labels must never be char-iterated --------

    def test_string_labels_with_worker_parsed_correctly(self) -> None:
        # Simulates an LLM tool call passing labels="a,b,worker:x" as a bare str.
        labs, stamped, err = ensure_create_labels(
            "a,b,worker:x",
            hired_hands=["worker:x"],
        )
        self.assertIsNone(err)
        self.assertFalse(stamped)
        self.assertEqual(labs, ["a", "b", "worker:x"])
        self.assertTrue(has_worker_label(labs))

    def test_string_labels_pre_hire_stamps_needs_routing(self) -> None:
        labs, stamped, err = ensure_create_labels("area,suite")
        self.assertIsNone(err)
        self.assertTrue(stamped)
        self.assertIn(NEEDS_ROUTING_LABEL, labs)
        self.assertIn("area", labs)
        self.assertIn("suite", labs)

    def test_string_labels_hard_b_rejects_without_seat(self) -> None:
        labs, stamped, err = ensure_create_labels(
            "area,suite",
            hired_hands=["worker:drew"],
        )
        self.assertIsNotNone(err)
        self.assertIn("worker:* required", err or "")

    def test_string_labels_comma_only_treated_as_empty(self) -> None:
        # A string of only commas/spaces → no valid labels → pre-hire stamp.
        labs, stamped, err = ensure_create_labels(", ,  ,")
        self.assertIsNone(err)
        self.assertTrue(stamped)
        self.assertEqual(labs, [NEEDS_ROUTING_LABEL])


if __name__ == "__main__":
    unittest.main()
