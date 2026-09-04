"""Tracker registry: shipped adapters and unknown-name fallback (wl-498)."""

from __future__ import annotations

import importlib
import os
import unittest

from worklane.trackers.registry import (
    get_default_tracker,
    get_tracker,
    list_trackers,
)


class TrackerRegistryTest(unittest.TestCase):
    def test_shipped_adapter_is_sqlite_only(self) -> None:
        self.assertIn("sqlite", list_trackers())
        self.assertNotIn("linear", list_trackers())
        self.assertIsNone(get_tracker("linear"))

    def test_unknown_tradeos_tracker_falls_back_to_sqlite(self) -> None:
        previous = os.environ.get("TRADEOS_TRACKER")
        os.environ["TRADEOS_TRACKER"] = "linear"
        try:
            tracker = get_default_tracker()
            self.assertEqual(tracker.name, "sqlite")
        finally:
            if previous is None:
                os.environ.pop("TRADEOS_TRACKER", None)
            else:
                os.environ["TRADEOS_TRACKER"] = previous

    def test_linear_module_is_gone(self) -> None:
        with self.assertRaises(ModuleNotFoundError):
            importlib.import_module("worklane.trackers.linear")
