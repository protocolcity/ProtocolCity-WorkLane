"""Create-path: auto-stamp needs:routing when worker:* absent."""
from __future__ import annotations

import unittest

from worklane.routing_labels import (
    NEEDS_ROUTING_LABEL,
    ensure_create_labels,
    has_worker_label,
)


class EnsureCreateLabelsTest(unittest.TestCase):
    def test_stamps_needs_routing_when_empty(self) -> None:
        labs, stamped = ensure_create_labels([])
        self.assertTrue(stamped)
        self.assertEqual(labs, [NEEDS_ROUTING_LABEL])

    def test_stamps_when_only_area_labels(self) -> None:
        labs, stamped = ensure_create_labels(["suite", "map", "product:protocolcity"])
        self.assertTrue(stamped)
        self.assertIn(NEEDS_ROUTING_LABEL, labs)
        self.assertIn("suite", labs)

    def test_no_stamp_when_worker_present(self) -> None:
        labs, stamped = ensure_create_labels(["worker:trinity", "suite"])
        self.assertFalse(stamped)
        self.assertEqual(labs, ["worker:trinity", "suite"])
        self.assertTrue(has_worker_label(labs))

    def test_drops_needs_routing_when_worker_present(self) -> None:
        labs, stamped = ensure_create_labels(
            ["needs:routing", "worker:carl", "suite"]
        )
        self.assertFalse(stamped)
        self.assertNotIn(NEEDS_ROUTING_LABEL, labs)
        self.assertIn("worker:carl", labs)

    def test_idempotent_needs_routing(self) -> None:
        labs, stamped = ensure_create_labels([NEEDS_ROUTING_LABEL])
        self.assertFalse(stamped)  # already present
        self.assertEqual(labs, [NEEDS_ROUTING_LABEL])


if __name__ == "__main__":
    unittest.main()
