"""Route-event wake nudges to WorkForce (wl-359).

When a ticket becomes dispatchable on a lane seat (``worker:<hand>``, not
``worker:you``), POST WorkForce ``/api/wake`` so the hand can fire within
seconds instead of waiting for its next clock.

Fire-and-forget discipline:
- Never raises into ticket write paths.
- Short timeout (~2s); connection failures are logged and ignored.
- Clock fire remains the guaranteed fallback when WorkForce is down.
- Debounce per hand (~10s) so bulk filing does not spam the roster.
- Kill switch ``WL_WAKE_DISABLE=1`` (set by the test suite) forces a no-op.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Iterable, Optional, Sequence
from urllib import request as urllib_request

from worklane.routing_labels import worker_ids_from_labels
from worklane.trackers.protocol import TaskStatus

_LOG = logging.getLogger("worklane.wake_nudge")

WAKE_TIMEOUT_S = 2.0
DEBOUNCE_S = 10.0

_lock = threading.Lock()
_last_wake: dict = {}  # worker_id -> monotonic timestamp of last accepted nudge


def reset_wake_debounce() -> None:
    """Clear debounce state (tests only)."""
    with _lock:
        _last_wake.clear()


def _workforce_base_url() -> str:
    return (
        os.environ.get("WL_WORKFORCE_URL")
        or os.environ.get("WL_WORKFORCE_URL")
        or "http://127.0.0.1:8797"
    ).rstrip("/")


def dispatchable_hand(
    labels: Optional[Iterable[str]],
    status: Optional[str] = None,
    gate_type: Optional[str] = None,
) -> Optional[str]:
    """Return the lane worker id to wake, or None when nothing is dispatchable.

    Dispatchable means:
    - status is backlog (ready pool)
    - no active gate (human / timer / deferred / tracking)
    - exactly one ``worker:*`` seat that is not ``worker:you``
    """
    st = (status or TaskStatus.BACKLOG).strip().lower()
    if st != TaskStatus.BACKLOG:
        return None
    gt = (gate_type or "").strip().lower()
    if gt in ("human", "timer", "deferred", "tracking"):
        return None
    ids = worker_ids_from_labels(labels or [])
    if len(ids) != 1:
        return None
    wid = ids[0]
    if wid == "you":
        return None
    return wid


def _debounce_accept(worker_id: str, now: Optional[float] = None) -> bool:
    """Return True if a nudge for *worker_id* should fire (and record it)."""
    t = time.monotonic() if now is None else now
    with _lock:
        last = _last_wake.get(worker_id)
        if last is not None and (t - last) < DEBOUNCE_S:
            return False
        _last_wake[worker_id] = t
        return True


def post_wake(worker_id: str) -> bool:
    """POST WorkForce /api/wake for *worker_id*. Never raises. Returns success."""
    if os.environ.get("WL_WAKE_DISABLE", "").strip() in ("1", "true", "yes"):
        _LOG.info("wake.dry_run(kill_switch) worker=%s", worker_id)
        return True

    url = _workforce_base_url() + "/api/wake"
    body = json.dumps({"worker": worker_id}).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    try:
        req = urllib_request.Request(url, data=body, headers=headers, method="POST")
        with urllib_request.urlopen(req, timeout=WAKE_TIMEOUT_S) as resp:
            ok = 200 <= int(getattr(resp, "status", 200) or 200) < 300
            if not ok:
                _LOG.warning(
                    "wake.dispatch_non_ok status=%s worker=%s",
                    getattr(resp, "status", None),
                    worker_id,
                )
            else:
                _LOG.info("wake.dispatched worker=%s", worker_id)
            return ok
    except Exception as exc:
        _LOG.info("wake.dispatch_failed worker=%s error=%s", worker_id, exc)
        return False


def maybe_wake_hand(
    labels: Optional[Iterable[str]],
    status: Optional[str] = None,
    gate_type: Optional[str] = None,
    *,
    previous_hand: Optional[str] = None,
    only_on_seat_change: bool = False,
    task_id: str = "",
) -> bool:
    """Wake the hand when the ticket is (newly) dispatchable on a lane seat.

    Parameters
    ----------
    only_on_seat_change:
        When True (create / label seat swap), skip if the dispatchable hand is
        unchanged from *previous_hand*. When False (release / reopen /
        gate-clear), wake whenever the ticket is currently dispatchable.
    previous_hand:
        Prior lane seat id (no ``worker:`` prefix), or None if none.
    """
    try:
        hand = dispatchable_hand(labels, status=status, gate_type=gate_type)
        if not hand:
            return False
        if only_on_seat_change and previous_hand is not None:
            if hand == previous_hand.strip().lower():
                return False
        if not _debounce_accept(hand):
            _LOG.info(
                "wake.debounced worker=%s task=%s",
                hand,
                task_id or "-",
            )
            return False
        return post_wake(hand)
    except Exception as exc:
        _LOG.warning(
            "wake.unexpected task=%s error=%s",
            task_id or "-",
            exc,
        )
        return False


def previous_hand_from_labels(labels: Optional[Sequence[str]]) -> Optional[str]:
    """Lane seat currently on the ticket (None for empty / worker:you only)."""
    ids = worker_ids_from_labels(labels or [])
    if len(ids) != 1:
        return None
    if ids[0] == "you":
        return None
    return ids[0]


def gate_of(task: Any) -> Optional[str]:
    """Best-effort gate_type from a Task-like object."""
    gt = getattr(task, "gate_type", None)
    if gt is None and isinstance(task, dict):
        gt = task.get("gate_type")
    if gt is None or gt == "":
        return None
    return str(gt)
