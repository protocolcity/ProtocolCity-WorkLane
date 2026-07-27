"""Intake routing labels for hand queues (pc-498 / create-path law).

WorkForce scheduled hands drain ready feeds filtered by ``worker:<id>``.
A create path that omits that label produces silent ready work unless we
stamp a visible fallback.

Law (BluePrint cities + this engine, 2026-07-27):
- Prefer exactly one ``worker:<id>`` at create when a hand is known.
- If no ``worker:*`` is present, auto-stamp ``needs:routing`` so unrouted
  ready is countable (Map banner, ``wl_ready --label needs:routing``).
- Never invent a hand id; never block create.
"""

from __future__ import annotations

import re
from typing import Iterable, List, Sequence, Tuple

WORKER_LABEL_RE = re.compile(r"^worker:(.+)$", re.IGNORECASE)
NEEDS_ROUTING_LABEL = "needs:routing"


def worker_ids_from_labels(labels: Iterable[str]) -> List[str]:
    seen = set()
    ids: List[str] = []
    for lab in labels or []:
        m = WORKER_LABEL_RE.match(str(lab).strip())
        if not m:
            continue
        wid = m.group(1).strip().lower()
        if not wid or wid in seen:
            continue
        seen.add(wid)
        ids.append(wid)
    return ids


def has_worker_label(labels: Iterable[str]) -> bool:
    return bool(worker_ids_from_labels(labels))


def ensure_create_labels(labels: Sequence[str] | None) -> Tuple[List[str], bool]:
    """Normalize create-time labels.

    Returns ``(labels, stamped_needs_routing)``.
    - If any ``worker:*`` present → drop redundant ``needs:routing``, keep rest.
    - If no ``worker:*`` → ensure ``needs:routing`` is present (auto-stamp).
    """
    labs = [str(x).strip() for x in (labels or []) if str(x).strip()]
    if has_worker_label(labs):
        cleaned = [x for x in labs if x.lower() != NEEDS_ROUTING_LABEL]
        return cleaned, False
    if not any(x.lower() == NEEDS_ROUTING_LABEL for x in labs):
        labs = list(labs) + [NEEDS_ROUTING_LABEL]
        return labs, True
    return labs, False
