"""Intake routing labels for hand queues (pc-498 / create-path law).

WorkForce scheduled hands drain ready feeds filtered by ``worker:<id>``.

Law (BluePrint cities + this engine):
- Prefer exactly one ``worker:<id>`` at create when a hand is known.
- ``worker:you`` is a valid human seat (personal list — not AI cron).
- **Hard B (wl-274, 2026-07-28):** when the product has ≥1 hired *lane*
  hand, create **requires** a ``worker:*`` seat — reject, do not soft-stamp
  as steady state. Pre-hire (no hired lanes): stamp ``needs:routing``.
- **Foreign seat (wl-372, 2026-08-04):** when hired lanes exist, a
  ``worker:<hand>`` that is not among those seats is rejected with the
  same valid-seat list as the missing-seat error. Stops dead seats
  (hand hired for another store only) from sitting READY while every
  lane probe misses them. ``worker:you`` is always allowed.
- **Starve guard (wl-315, 2026-08-01):** bare ``worker:you`` (no ``you:kind``
  and no founder gate label) is rejected when hands are hired — it parks
  implement work on a seat cron never drains. Require ``you:note|remind|todo|host``
  or a founder/publish gate label, or route to ``worker:<persona>``.
- Never invent a hand id.
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

WORKER_LABEL_RE = re.compile(r"^worker:(.+)$", re.IGNORECASE)
YOU_KIND_RE = re.compile(r"^you:(note|remind|todo|host)$", re.IGNORECASE)
NEEDS_ROUTING_LABEL = "needs:routing"
WORKER_YOU = "worker:you"
# Founder / publish park on You is intentional — not starve of implement work.
_FOUNDER_GATE_LABELS = frozenset(
    {
        "gate:founder",
        "gate:publish",
        "gate:human",
        "founder",
        "publish",
    }
)


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


def has_you_kind(labels: Iterable[str]) -> bool:
    """True when a you:note|remind|todo|host kind is present."""
    for lab in labels or []:
        if YOU_KIND_RE.match(str(lab).strip()):
            return True
    return False


def has_founder_gate_label(labels: Iterable[str]) -> bool:
    """True when a founder/publish gate label parks work on You intentionally."""
    for lab in labels or []:
        s = str(lab).strip().lower()
        if s in _FOUNDER_GATE_LABELS or s.startswith("gate:founder"):
            return True
    return False


def worker_you_is_classified(labels: Iterable[str]) -> bool:
    """worker:you is OK only when classified (you-kind or founder gate)."""
    labs = list(labels or [])
    if "you" not in worker_ids_from_labels(labs):
        return True
    return has_you_kind(labs) or has_founder_gate_label(labs)


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


def foreign_seat_error(
    worker_ids: Sequence[str],
    hired_hands: Optional[Sequence[str]] = None,
) -> Optional[str]:
    """Return an error when a lane seat is not hired for this product (wl-372).

    ``worker:you`` is never foreign. Pre-hire (empty hired list) returns None
    — membership cannot be checked without a seat roster for the store.
    """
    hired = normalize_hired_hands(hired_hands)
    if not hired:
        return None
    hired_set = set(hired)
    for wid in worker_ids:
        w = str(wid).strip().lower()
        if not w or w == "you":
            continue
        seat = "worker:" + w
        if seat not in hired_set:
            return (
                seat
                + " is not a hired seat for this product. "
                + format_seat_help(hired)
            )
    return None


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
    - If a ``worker:<hand>`` is not among hired seats (hard_when_hands) → error
      (wl-372 foreign seat).
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
    hired = normalize_hired_hands(hired_hands)
    if has_worker_label(labs):
        cleaned = [x for x in labs if x.lower() != NEEDS_ROUTING_LABEL]
        # wl-315: bare worker:you starves ready when hands exist — classify it.
        if (
            hard_when_hands
            and hired
            and "you" in ids
            and not worker_you_is_classified(cleaned)
        ):
            return (
                cleaned,
                False,
                "worker:you without you:note|remind|todo|host (or founder/publish gate) "
                "starves hand queues — cron never drains You. "
                "Route implement work to a hired seat, or classify the human park: "
                + format_seat_help(hired)
                + " + you:note|you:host|you:todo|you:remind",
            )
        # wl-372: reject seats not hired for this product (same UX as omit).
        if hard_when_hands and hired:
            foreign = foreign_seat_error(ids, hired)
            if foreign:
                return cleaned, False, foreign
        return cleaned, False, None

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
    - done/canceled (not live) → drop ``needs:routing`` (wl-439: terminal
      residue must not accumulate; transition-time strip, no separate repair).
    """
    labs = _coerce_labels(labels)
    if has_worker_label(labs):
        cleaned = [x for x in labs if x.lower() != NEEDS_ROUTING_LABEL]
        return cleaned, False, len(cleaned) != len(labs)
    if not live:
        # wl-439: terminal tickets are not a queue — drop the stamp so
        # doctor/repair does not have to sweep residue after close/cancel.
        cleaned = [x for x in labs if x.lower() != NEEDS_ROUTING_LABEL]
        return cleaned, False, len(cleaned) != len(labs)
    if any(x.lower() == NEEDS_ROUTING_LABEL for x in labs):
        return labs, False, False
    return labs + [NEEDS_ROUTING_LABEL], True, False


def _labels_after_mutation(
    current_labels: Sequence[str],
    *,
    add: Optional[Sequence[str]] = None,
    remove: Optional[Sequence[str]] = None,
) -> List[str]:
    """Apply add/remove to a copy of current labels (mutation preview)."""
    labs = list(_coerce_labels(current_labels))
    for lb in add or []:
        lb_s = str(lb).strip()
        if lb_s and lb_s not in labs:
            labs.append(lb_s)
    for lb in remove or []:
        lb_s = str(lb).strip()
        if lb_s in labs:
            labs.remove(lb_s)
    return labs


def check_mutation_starve_guard(
    current_labels: Sequence[str],
    *,
    add: Optional[Sequence[str]] = None,
    remove: Optional[Sequence[str]] = None,
    hired_hands: Optional[Sequence[str]] = None,
) -> Optional[str]:
    """wl-320: starve guard for label *mutations*.

    Returns an error string when the post-mutation label set would result in
    bare ``worker:you`` (no you-kind, no founder gate) while hired hands exist.
    Returns None when the mutation is safe.
    """
    hired = normalize_hired_hands(hired_hands)
    if not hired:
        return None  # pre-hire: no guard applies

    labs = _labels_after_mutation(current_labels, add=add, remove=remove)

    if not worker_you_is_classified(labs):
        return (
            "worker:you without you:note|remind|todo|host (or founder/publish gate) "
            "starves hand queues — cron never drains You. "
            "Route implement work to a hired seat, or classify the human park: "
            + format_seat_help(hired)
            + " + you:note|you:host|you:todo|you:remind"
        )
    return None


def check_mutation_foreign_seat(
    current_labels: Sequence[str],
    *,
    add: Optional[Sequence[str]] = None,
    remove: Optional[Sequence[str]] = None,
    hired_hands: Optional[Sequence[str]] = None,
) -> Optional[str]:
    """wl-372: foreign-seat guard for label *mutations*.

    Returns an error when the post-mutation set would carry a ``worker:<hand>``
    that is not hired for this product. ``worker:you`` is always allowed.
    Pre-hire (no hired hands) returns None.
    """
    hired = normalize_hired_hands(hired_hands)
    if not hired:
        return None
    labs = _labels_after_mutation(current_labels, add=add, remove=remove)
    return foreign_seat_error(worker_ids_from_labels(labs), hired)


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
