"""Ops-cockpit tracker and composite-id parsing."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Tuple

import worklane as _wl_pkg
from worklane.products import split_task_id

# Package peel: board.py used to live at worklane/board.py, so
# Path(__file__).parent was the worklane package root. Keep that root.
_WL_PKG_ROOT = Path(_wl_pkg.__file__).resolve().parent


def ops_tickets_db_path() -> Path:
    """SQLite file for Ops-scoped tickets.

    Local-first default keeps ticketing runtime under the protocol root:
    ``worklane/local/data/ops_tickets.db``.
    Override with ``OPS_TICKETS_DB`` when needed.
    """
    override = (os.environ.get("OPS_TICKETS_DB") or "").strip()
    if override:
        return Path(override)
    wl_root = _WL_PKG_ROOT
    default = wl_root / "local" / "data" / "ops_tickets.db"
    legacy_hidden = wl_root / ".local" / "data" / "ops_tickets.db"
    legacy_root = wl_root.parent / "local" / "data" / "ops_tickets.db"
    if default.exists():
        return default
    if legacy_hidden.exists():
        return legacy_hidden
    if legacy_root.exists():
        return legacy_root
    return default


def get_ops_ticket_tracker() -> Any:
    from worklane.trackers.sqlite import PRODUCT_LABEL_OPS, SQLiteTracker

    return SQLiteTracker(
        db_path=ops_tickets_db_path(),
        product_default=PRODUCT_LABEL_OPS,
    )


def parse_surface_task_id(task_id: str) -> Tuple[str, str]:
    """Composite id → (product slug, raw store id). Registry-driven."""
    return split_task_id(task_id)
