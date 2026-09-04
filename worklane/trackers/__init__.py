"""Project tracker abstraction (SEO-164 / SEO-171).

``ProjectTracker`` is the single interface that dev-dashboard code and
session tooling use to read/write work items. One adapter ships with the
repo:

* :class:`worklane.trackers.sqlite.SQLiteTracker` — the default. Stores product
  tasks under ``local/data/tradeos.db`` (override with ``TRADEOS_TRACKER_DB``).
  Distinct from product journal/backtest SQLite (ADR-019/021).

The active tracker is chosen by :func:`get_default_tracker`, which reads
``TRADEOS_TRACKER`` (``sqlite`` by default). Unknown names fall back to
sqlite.
"""

from __future__ import annotations

from worklane.trackers.protocol import (
    ProjectTracker,
    Task,
    TaskComment,
    TaskStatus,
    task_is_gated,
)
from worklane.trackers.registry import (
    get_default_tracker,
    get_tracker,
    list_trackers,
    register_tracker,
)

__all__ = [
    "ProjectTracker",
    "Task",
    "TaskComment",
    "TaskStatus",
    "get_default_tracker",
    "get_tracker",
    "list_trackers",
    "register_tracker",
    "task_is_gated",
]
