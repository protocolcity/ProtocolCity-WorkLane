"""Board constants: ticket paths, vocab labels, prefixes, chip facets."""
from __future__ import annotations

from typing import Dict, List

from worklane.trackers import TaskStatus

TICKETS_APP_ALL = "/admin/tickets/all"
TICKETS_APP_TRADEOS = "/admin/tickets/tradeos"
TICKETS_APP_OPS = "/admin/tickets/ops"

_WORK_QUEUE_PATH = "/admin/work-queue"


# ── vocab → labels ────────────────────────────────────────────────────────
_STATUS_LABELS = {
    TaskStatus.BACKLOG:      "Backlog",
    TaskStatus.IN_PROGRESS:  "In Progress",
    TaskStatus.IN_REVIEW:    "In Review",
    TaskStatus.DONE:         "Done",
    TaskStatus.CANCELED:     "Canceled",
}

_STATUS_TIERS = {
    TaskStatus.BACKLOG:      "neutral",
    TaskStatus.IN_PROGRESS:  "info",
    TaskStatus.IN_REVIEW:    "warning",
    TaskStatus.DONE:         "positive",
    TaskStatus.CANCELED:     "neutral",
}

_PRIORITY_LABELS: Dict[int, str] = {1: "Urgent", 2: "High", 3: "Normal", 4: "Low", 0: "—"}
_PRIORITY_TIERS: Dict[int, str] = {1: "critical", 2: "warning", 3: "neutral", 4: "neutral", 0: "neutral"}

# wl-10: named facets get their own chip row (top-N + "more"); everything
# else (parent:, epic:, size:, needs:, one-off composites) falls into the
# "other" bucket, collapsed by default behind its own toggle.
_CHIP_FACET_PREFIXES = ("area:", "sys:", "product:", "lane:", "type:")
_CHIP_TOP_N = 6

# Ticket work area — orthogonal to Dev vs Work queue chrome.
PRODUCT_LABEL_TRADEOS = "product:tradeos"
PRODUCT_LABEL_OPS = "product:ops"

# Composite task ids in merged views (``t-`` = tradeOS repo DB, ``o-`` = Ops Cockpit DB).
TASK_ID_PREFIX_TRADEOS = "t"
TASK_ID_PREFIX_OPS = "o"

# Kanban columns — Canceled is omitted from the board.
_BOARD_COLUMNS: List[str] = [
    TaskStatus.BACKLOG,
    TaskStatus.IN_REVIEW,
    TaskStatus.IN_PROGRESS,
    TaskStatus.DONE,
]

# Byline icon for any claim-owner identity. Identities come from the store's
# signed comments and render verbatim — no baked-in agent roster (wl-84):
# which identities exist is the host deployment's business, not the product's.
OWNER_BYLINE_ICON = "·"
