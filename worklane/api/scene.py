"""Desk scene API route extracted from task_server (wl-222)."""
from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from worklane.board import _claim_stale_minutes
from worklane.products import discover_products, product_trackers
from worklane.server_helpers import _activity_ts_sort_key
from worklane.trackers import TaskStatus

router = APIRouter()

_SCENE_WINDOW_HOURS = 24
_SCENE_TRANSITION_HOURS = _SCENE_WINDOW_HOURS
# wl-191: window must match _SCENE_WINDOW_HOURS; busy days truncate at this
# limit (newest first) — the tape links to Desk for the rest.
_SCENE_TRANSITION_LIMIT = 40

# Suite + WorkForce + citylens all hit /api/scene together. Without a short
# cache, concurrent sync handlers re-walk every store and pin the process at
# multi-core CPU — Map bootstrap then hangs (2026-07-25 dogfood).
# pc-881: 2s was too short vs Map soft-poll + compat + gate-count stampede;
# 8s still feels live for open badges while cutting rebuild thrash hard.
_SCENE_CACHE_TTL_S = 8.0
_scene_lock = threading.Condition()
_scene_cache_ts = 0.0
_scene_cache_payload: Optional[Dict[str, Any]] = None
_scene_inflight = False


def _closeout_authors(slug: str) -> Dict[str, str]:
    """task_id -> author of the latest 'Completed:' close-out comment, for
    one store (wl-165 sprite chips). Same signed-comment derivation as the
    Allocation view; keyed by both the raw and prefixed id so the scene's
    composite ids always match."""
    out: Dict[str, str] = {}
    pairs = product_trackers()
    if slug:
        pairs = [(s, t) for s, t in pairs if s.slug == slug]
    for spec, tracker in pairs:
        db_path = getattr(tracker, "_db_path", None)
        if db_path is None or not Path(db_path).exists():
            continue
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                rows = conn.execute(
                    "SELECT task_id, author FROM task_comments "
                    "WHERE body LIKE 'Completed:%' AND author != '' "
                    "ORDER BY created_at",
                ).fetchall()
            finally:
                conn.close()
        except Exception:
            continue
        for tid, author in rows:
            tid = str(tid)
            out[tid] = str(author)          # later rows win: latest close-out
            out[f"{spec.prefix}-{tid}"] = str(author)
    return out


def _scene_recent_transitions(
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """Status hops for board film (wl-168 / CITY_FLOW F1–F10).

    Includes:
    - ``status_change`` with from_status inferred from the prior status-bearing
      event on the same task
    - ``created`` (birth onto backlog) — intake never emits a status_change, so
      without this row Office/Desk paper-flyers never fire for new filings
      (founder dogfood 2026-07-16, pc-194/pc-195)
    """
    now = now or datetime.now(timezone.utc)
    cutoff_ts = (now - timedelta(hours=_SCENE_TRANSITION_HOURS)).timestamp()
    out: List[Dict[str, Any]] = []
    for spec, tracker in product_trackers():
        db_path = getattr(tracker, "_db_path", None)
        if db_path is None or not Path(db_path).exists():
            continue
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    """
                    SELECT e.id AS event_id,
                           e.event_type AS event_type,
                           COALESCE(t.ext_id, CAST(t.id AS TEXT)) AS task_id,
                           e.status AS to_status,
                           e.created_at AS ts,
                           (SELECT e2.status FROM task_events e2
                             WHERE e2.task_id = e.task_id
                               AND e2.id < e.id
                               AND e2.status IS NOT NULL
                               AND e2.status != ''
                             ORDER BY e2.id DESC LIMIT 1) AS from_status,
                           CASE WHEN e.event_type = 'created' THEN
                             /* Intake comment is written after the created
                                row — take the first non-empty author. */
                             (SELECT c.author FROM task_comments c
                               WHERE c.task_id = e.task_id
                                 AND c.author != ''
                               ORDER BY c.id ASC LIMIT 1)
                           ELSE
                             (SELECT c.author FROM task_comments c
                               WHERE c.task_id = e.task_id
                                 AND c.author != ''
                                 AND c.created_at <= e.created_at
                               ORDER BY c.created_at DESC LIMIT 1)
                           END AS author
                      FROM task_events e
                      JOIN tasks t ON t.id = e.task_id
                     WHERE e.event_type IN ('status_change', 'created')
                       AND e.status IS NOT NULL
                       AND e.status != ''
                     ORDER BY e.id DESC
                     LIMIT 80
                    """,
                ).fetchall()
            finally:
                conn.close()
        except Exception:
            continue
        for r in rows:
            to_st = str(r["to_status"] or "").strip()
            from_st = str(r["from_status"] or "").strip()
            et = str(r["event_type"] or "").strip()
            # Birth filings: no prior status — force empty from so film treats
            # them as F1/F3 (file onto Desk), not a no-op same-status hop.
            if et == "created":
                from_st = ""
            if not to_st or from_st == to_st:
                continue
            ts = r["ts"] or ""
            if _activity_ts_sort_key(ts) < cutoff_ts:
                continue
            raw_id = str(r["task_id"])
            composite = (
                raw_id if raw_id.startswith(f"{spec.prefix}-")
                else f"{spec.prefix}-{raw_id}"
            )
            out.append({
                "id": f"{spec.slug}:{r['event_id']}",
                "task_id": composite,
                "from_status": from_st,
                "to_status": to_st,
                "author": str(r["author"] or ""),
                "ts": ts,
                "store": spec.slug,
            })
    out.sort(key=lambda x: _activity_ts_sort_key(x.get("ts")), reverse=True)
    return out[:_SCENE_TRANSITION_LIMIT]


def _build_desk_scene_payload() -> Dict[str, Any]:
    """Heavy path: per-store counts, attention, filed, transitions."""
    from worklane.server_helpers import (  # noqa: PLC0415
        _collect_founder_attention_items,
        _merged_ready_count,
        _merged_scope_tasks_for_filters,
        _parse_task_date_utc,
    )
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=_SCENE_WINDOW_HOURS)
    stores: List[Dict[str, Any]] = []
    filed: List[Dict[str, Any]] = []
    for spec in discover_products():
        tasks = _merged_scope_tasks_for_filters(spec.slug)
        closers = _closeout_authors(spec.slug)
        counts = {
            TaskStatus.BACKLOG: 0,
            TaskStatus.IN_PROGRESS: 0,
            TaskStatus.IN_REVIEW: 0,
        }
        done_total = 0
        for t in tasks:
            st = (t.status or "").strip()
            if st == TaskStatus.DONE:
                done_total += 1
                dt = _parse_task_date_utc(t.updated_at)
                if dt is not None and dt >= cutoff:
                    filed.append({
                        "id": t.id, "store": spec.slug, "title": t.title,
                        "closed_at": t.updated_at,
                        "author": closers.get(str(t.id), ""),
                    })
            elif st in counts:
                counts[st] += 1
        stores.append({
            "slug": spec.slug, "display": spec.display, "prefix": spec.prefix,
            "backlog": counts[TaskStatus.BACKLOG],
            "in_progress": counts[TaskStatus.IN_PROGRESS],
            "in_review": counts[TaskStatus.IN_REVIEW],
            "done_total": done_total,
            "ready": _merged_ready_count(spec.slug),
        })
    filed.sort(key=lambda f: _activity_ts_sort_key(f.get("closed_at")), reverse=True)
    return {
        "ok": True,
        "generated_at": now.isoformat(),
        "window_hours": _SCENE_WINDOW_HOURS,
        "stale_minutes": _claim_stale_minutes(),
        "stores": stores,
        "attention": _collect_founder_attention_items(now=now),
        "filed": filed[:60],
        "recent_transitions": _scene_recent_transitions(now=now),
    }


@router.get("/api/scene")
def api_desk_scene() -> JSONResponse:
    """The desk scene's facts in one call (wl-132): per-store ledger counts,
    the founder-attention tray (wl-135 collector, unchanged), and the window
    of FILED receipts. Computed from THIS engine's own stores — the scene
    never reads the city lens (engines compute their own facts).

    wl-168: also a recent_transitions[] window (task id, from_status,
    to_status, author, ts) so the paper line can animate status movement —
    /api/dev/activity only carries new_status, not old→new pairs.
    Birth filings use event_type=created (empty from_status → backlog).

    Short single-flight cache: concurrent suite/WF/citylens callers share one
    build so the process cannot CPU-spin on overlapping scene walks.
    """
    global _scene_cache_ts, _scene_cache_payload, _scene_inflight
    now_m = time.monotonic()
    with _scene_lock:
        if (
            _scene_cache_payload is not None
            and (now_m - _scene_cache_ts) < _SCENE_CACHE_TTL_S
        ):
            payload = _scene_cache_payload
            resp = JSONResponse(payload)
            resp.headers["Cache-Control"] = "no-store, max-age=0"
            resp.headers["X-Scene-Cache"] = "hit"
            return resp
        while _scene_inflight:
            _scene_lock.wait(timeout=8.0)
            now_m = time.monotonic()
            if (
                _scene_cache_payload is not None
                and (now_m - _scene_cache_ts) < _SCENE_CACHE_TTL_S
            ):
                payload = _scene_cache_payload
                resp = JSONResponse(payload)
                resp.headers["Cache-Control"] = "no-store, max-age=0"
                resp.headers["X-Scene-Cache"] = "coalesce"
                return resp
            # Stale or failed builder — try ourselves
            if not _scene_inflight:
                break
        _scene_inflight = True

    payload: Optional[Dict[str, Any]] = None
    err: Optional[BaseException] = None
    try:
        payload = _build_desk_scene_payload()
    except BaseException as exc:  # noqa: BLE001 — re-raise after unlock
        err = exc
    finally:
        with _scene_lock:
            if payload is not None:
                _scene_cache_payload = payload
                _scene_cache_ts = time.monotonic()
            _scene_inflight = False
            _scene_lock.notify_all()
    if err is not None:
        raise err
    assert payload is not None
    resp = JSONResponse(payload)
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    resp.headers["X-Scene-Cache"] = "miss"
    return resp
