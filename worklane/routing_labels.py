"""Intake routing labels for hand queues (pc-498 / create-path law).

WorkForce scheduled hands drain ready feeds filtered by ``worker:<id>``.

Law (BluePrint cities + this engine):
- Prefer exactly one ``worker:<id>`` at create when a hand is known.
- ``worker:you`` is a valid human seat (personal list — not AI cron).
- **Hard B (wl-274, 2026-07-28):** when the product has ≥1 hired *lane*
  hand, create **requires** a ``worker:*`` seat — reject, do not soft-stamp
  as steady state. Pre-hire (no hired lanes): stamp ``needs:routing``.
- Never invent a hand id.
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

WORKER_LABEL_RE = re.compile(r"^worker:(.+)$", re.IGNORECASE)
YOU_KIND_RE = re.compile(r"^you:(note|remind|todo|host)$", re.IGNORECASE)
NEEDS_ROUTING_LABEL = "needs:routing"
WORKER_YOU = "worker:you"


def _coerce_labels(raw: object) -> List[str]:
    """Return a clean list of stripped, non-empty label strings from any input.

    Guards the pc-621 incident: an LLM tool call may pass labels as a
    comma-joined string ("ship,service,worker:you").  Iterating a bare str
    char-wise destroys the worker seat silently.  A str is always comma-split
    here; any other iterable has its elements str-coerced normally.
    """
    if isinstance(raw, str):
        return [s.strip() for s in raw.split(",") if s.strip()]
    return [str(x).strip() for x in (raw or []) if str(x).strip()]


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


def normalize_hired_hands(hired: Optional[Sequence[str]]) -> List[str]:
    """Return sorted unique ``worker:<id>`` strings from roster names or labels."""
    out: List[str] = []
    seen = set()
    for h in hired or []:
        s = str(h).strip().lower()
        if not s:
            continue
        if s.startswith("worker:"):
            s = s[7:]
        if not s or s in seen:
            continue
        # Jobs are not seats for hard-B (caller should filter kind=lane)
        if s in ("clerk", "marshal", "correspondent"):
            continue
        seen.add(s)
        out.append("worker:" + s)
    out.sort()
    return out


def format_seat_help(hired: Sequence[str]) -> str:
    """Error / warning trailer listing valid seats including worker:you."""
    seats = list(normalize_hired_hands(hired))
    if WORKER_YOU not in seats:
        seats = seats + [WORKER_YOU]
    return (
        "Use exactly one hand seat: "
        + ", ".join(seats)
        + ". Example: --label worker:you  or  --label worker:<persona>"
    )


def ensure_create_labels(
    labels: Sequence[str] | None,
    *,
    hired_hands: Optional[Sequence[str]] = None,
    hard_when_hands: bool = True,
) -> Tuple[List[str], bool, Optional[str]]:
    """Normalize create-time labels under hard-B law.

    Returns ``(labels, stamped_needs_routing, error)``.
    - ``error`` set → caller must reject create (do not persist).
    - If any ``worker:*`` present → drop redundant ``needs:routing``.
    - If dual ``worker:*`` → error.
    - If no ``worker:*`` and hired lanes exist and hard_when_hands → error (B).
    - If no ``worker:*`` and no hired lanes → stamp ``needs:routing`` (pre-hire).
    """
    labs = _coerce_labels(labels)
    ids = worker_ids_from_labels(labs)
    if len(ids) > 1:
        return (
            labs,
            False,
            "exactly one worker:<id> label allowed — got "
            + ", ".join("worker:" + i for i in ids),
        )
    if has_worker_label(labs):
        cleaned = [x for x in labs if x.lower() != NEEDS_ROUTING_LABEL]
        return cleaned, False, None

    hired = normalize_hired_hands(hired_hands)
    if hard_when_hands and hired:
        return (
            labs,
            False,
            "worker:* required when hands are hired for this product. "
            + format_seat_help(hired),
        )

    # Pre-hire soft path
    if not any(x.lower() == NEEDS_ROUTING_LABEL for x in labs):
        labs = list(labs) + [NEEDS_ROUTING_LABEL]
        return labs, True, None
    return labs, False, None


def reconcile_routing_after_mutation(
    labels: Sequence[str],
    *,
    live: bool = True,
) -> Tuple[List[str], bool, bool]:
    """Post-mutation invariant (wl-281): a live ticket never sits silently
    unrouted.

    Mirrors the create-path stamp for label *mutations* — the pc-603
    incident: created with ``worker:carl`` (no stamp, correct), label
    removed seconds later, ticket sat ready with neither a seat nor
    ``needs:routing``.

    Returns ``(labels, stamped_needs_routing, dropped_needs_routing)``:
    - any ``worker:*`` present → drop redundant ``needs:routing``;
    - live ticket with zero ``worker:*`` → ensure ``needs:routing``;
    - done/canceled ticket without a seat → leave as-is (not a queue).
    """
    labs = _coerce_labels(labels)
    if has_worker_label(labs):
        cleaned = [x for x in labs if x.lower() != NEEDS_ROUTING_LABEL]
        return cleaned, False, len(cleaned) != len(labs)
    if not live:
        return labs, False, False
    if any(x.lower() == NEEDS_ROUTING_LABEL for x in labs):
        return labs, False, False
    return labs + [NEEDS_ROUTING_LABEL], True, False


def check_worker_product_mismatch(
    worker_ids: Iterable[str],
    ticket_product: str,
    all_worker_products: Dict[str, str],
) -> Optional[str]:
    """Warn when a worker:<id> is registered for a different product (wl-296).

    Returns a warning string listing each mismatch, or None when all workers
    match or are absent from the roster (roster absence = unknown, not wrong).
    ``worker:you`` is always skipped — it is a personal human seat, not a
    roster lane, so no product comparison applies.
    """
    mismatches: List[Tuple[str, str]] = []
    for wid in worker_ids:
        if wid.lower() == "you":
            continue
        registered = all_worker_products.get(wid)
        if registered and registered != ticket_product:
            mismatches.append((wid, registered))
    if not mismatches:
        return None
    parts = [
        f"worker:{wid} is registered for product={registered!r}, not {ticket_product!r}"
        for wid, registered in mismatches
    ]
    return (
        "worker/product mismatch — this ticket will not appear in the hand's ready feed. "
        + "; ".join(parts)
        + ". Move the ticket to the correct store or re-route it."
    )
