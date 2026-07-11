"""Project tracker registry (SEO-164).

Mirrors the broker registry pattern (``core/brokers/registry.py``): a
module-level dict keyed by adapter name, populated at import time with
the shipped defaults, and mutated at runtime via :func:`register_tracker`
for tests and out-of-tree adapters.

``get_default_tracker()`` is the entry point every caller should use.
Selection honors ``TRADEOS_TRACKER`` (default: ``sqlite``).
"""

from __future__ import annotations

import os
from typing import Callable, Dict, List, Optional

from worklane.trackers.protocol import ProjectTracker


# Factories return a *fresh* tracker instance on each call so tests can
# swap the backing DB path (SQLiteTracker) or API key (LinearTracker)
# without dragging stale connections between cases.
TrackerFactory = Callable[[], ProjectTracker]

_REGISTRY: Dict[str, TrackerFactory] = {}


def register_tracker(name: str, factory: TrackerFactory) -> None:
    """Register a tracker adapter under ``name`` (case-insensitive)."""
    _REGISTRY[name.strip().lower()] = factory


def list_trackers() -> List[str]:
    """Return registered adapter names in sorted order."""
    return sorted(_REGISTRY.keys())


def get_tracker(name: str) -> Optional[ProjectTracker]:
    """Return a fresh instance of the named adapter, or ``None``."""
    factory = _REGISTRY.get((name or "").strip().lower())
    if factory is None:
        return None
    return factory()


def get_default_tracker() -> ProjectTracker:
    """Return the tracker selected by ``TRADEOS_TRACKER`` (default: sqlite).

    Falls back to ``sqlite`` if the requested adapter is not registered —
    the local DB is always available and never fails because of missing
    credentials, which keeps the dev dashboard functional on a fresh
    clone.
    """
    requested = (os.environ.get("TRADEOS_TRACKER") or "sqlite").strip().lower()
    tracker = get_tracker(requested)
    if tracker is None:
        tracker = get_tracker("sqlite")
    if tracker is None:
        raise RuntimeError(
            "No ProjectTracker adapter registered — worklane.trackers bootstrap failed"
        )
    return tracker


def _register_defaults() -> None:
    # Import lazily so module import doesn't pay for adapters that are
    # never used in this process.
    from worklane.trackers.sqlite import SQLiteTracker
    from worklane.trackers.linear import LinearTracker

    register_tracker("sqlite", SQLiteTracker)
    register_tracker("linear", LinearTracker)


_register_defaults()
