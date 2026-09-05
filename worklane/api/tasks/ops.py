"""Ops health, dev API, attention cache, queue, and identity routes."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from worklane.api.tasks._router import router
from worklane.board import (
    TICKETS_APP_ALL,
    _claim_stale_minutes,
    ops_tickets_db_path,
)
from worklane.devqueue import (
    WorkQueue,
    build_dispatch_prompt,
    group_by_file_conflict,
    run_shutdown,
)
from worklane.products import (
    default_product_slug,
    get_product,
    product_tracker,
    product_trackers,
)
from worklane.server_helpers import (
    _FOUNDER_ID_RE,
    _activity_ts_sort_key,
    _allocation_author_rows,
    _allocation_lane_rows,
    _attention_band_counts,
    _collect_founder_attention_items,
    _identity_config,
    _identity_config_path,
    _load_attention_prefs,
    _merged_in_flight_tasks,
    _merged_ready_count,
    _parse_task_date_utc,
    _parse_until_iso,
    _partition_attention_items,
    _save_attention_prefs,
)
from worklane.trackers import TaskStatus, get_default_tracker

@router.get("/api/ops/tickets-health")
def api_ops_tickets_health() -> JSONResponse:
    """Verify dual ticket DB paths and row counts (standalone WorkLane server)."""
    from worklane.trackers.sqlite import DEFAULT_DB_PATH  # noqa: PLC0415

    def _count_tasks(db_path: object) -> int:
        p = db_path
        if not p.exists():
            return -1
        try:
            conn = sqlite3.connect(str(p))
            try:
                row = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()
                return int(row[0]) if row else 0
            finally:
                conn.close()
        except Exception:
            return -2

    to_path = DEFAULT_DB_PATH
    op_path = ops_tickets_db_path()
    return JSONResponse(
        {
            "ok": True,
            "implementation": "tradeOS.worklane.task_server",
            "tradeos_db": {
                "path": str(to_path.resolve()),
                "task_rows": _count_tasks(to_path),
            },
            "ops_cockpit_db": {
                "path": str(op_path.resolve()),
                "task_rows": _count_tasks(op_path),
            },
        }
    )


# ── Dev API ─────────────────────────────────────────────────────────────────

@router.get("/api/dev/tasks")
def api_dev_tasks(status: str = "", label: str = "", limit: int = 200) -> JSONResponse:
    """JSON view of the work queue."""
    tracker = get_default_tracker()
    tasks = tracker.list_tasks(
        status=status or None,
        label=label or None,
        limit=limit,
    )
    return JSONResponse({
        "tracker": tracker.name,
        "tasks": [t.to_dict() for t in tasks],
    })


def _activity_store_context(project: str) -> Tuple[Any, str, str]:
    """Resolve (tracker, store_slug, prefix) for /api/dev/activity (wl-387).

    Matches the historical degrade rule: omitted or unknown ``project``
    falls back to the server default tracker rather than erroring. Stamp
    values always describe the store the feed is actually reading so
    wire consumers (Map comment-theater, cross-store pollers) get the same
    composite task id + store slug contract as ``/api/events`` (wl-348).
    """
    prod = (project or "").strip().lower()
    spec = get_product(prod) if prod else None
    if spec is not None:
        return product_tracker(spec), spec.slug, spec.prefix
    tracker = get_default_tracker()
    slug = default_product_slug() or "tradeos"
    default_spec = get_product(slug)
    if default_spec is not None:
        return tracker, default_spec.slug, default_spec.prefix
    return tracker, slug, "t"


@router.get("/api/dev/activity")
def api_dev_activity(limit: int = 30, project: str = "") -> JSONResponse:
    """Recent comments + status changes across all tasks, newest first.

    Returns a unified feed mixing comments and status transitions so the
    board's activity widget shows everything happening in one timeline.

    ``project`` scopes the feed to a specific product's tracker (e.g. a
    machine-wide worker roster on a host reads each lane's rounds from the
    project it signs into — tradeOS t-1327). Omitted or unknown → the
    server default tracker (``product_tracker`` falls back for an unknown
    slug, so a stale ?project= degrades to today's behavior, never errors).

    Wire contract (wl-348 / wl-387): every entry carries ``store`` (product
    slug) and composite ``task_id`` (``<prefix>-<rowid>``), matching
    ``/api/events`` / ``/api/events/stream``. Existing fields retained for
    the board activity widget.
    """
    from worklane.api.events_stream import _composite_task_id

    tracker, store_slug, prefix = _activity_store_context(project)
    if not hasattr(tracker, "_connect"):
        return JSONResponse({"entries": []})
    with tracker._connect() as conn:
        # Comments
        comment_rows = conn.execute(
            """
            SELECT c.id, c.task_id, c.body, c.author, c.created_at,
                   t.title AS task_title, 'comment' AS entry_type,
                   '' AS new_status
              FROM task_comments c
              JOIN tasks t ON t.id = c.task_id
             ORDER BY c.created_at DESC
             LIMIT ?
            """,
            (limit,),
        ).fetchall()

        # Recent status changes — tasks updated in last 24h, inferred
        # from updated_at + current status. Not perfect but gives
        # visibility into terminal activity without a dedicated log table.
        status_rows = conn.execute(
            """
            SELECT id AS task_id, title AS task_title, status AS new_status,
                   updated_at AS created_at,
                   'status_change' AS entry_type
              FROM tasks
             WHERE updated_at > datetime('now', '-24 hours')
               AND status IN ('in_progress', 'in_review', 'done', 'canceled')
             ORDER BY updated_at DESC
             LIMIT ?
            """,
            (limit,),
        ).fetchall()

    entries = []
    for r in comment_rows:
        composite_tid = _composite_task_id(prefix, r["task_id"])
        entries.append({
            "id": r["id"],
            "task_id": composite_tid,
            "store": store_slug,
            "body": r["body"],
            "author": r["author"],
            "created_at": r["created_at"],
            "task_title": r["task_title"],
            "entry_type": "comment",
            "new_status": "",
        })
    for r in status_rows:
        composite_tid = _composite_task_id(prefix, r["task_id"])
        entries.append({
            "id": f"sc-{r['task_id']}",
            "task_id": composite_tid,
            "store": store_slug,
            "body": "",
            "author": "",
            "created_at": r["created_at"],
            "task_title": r["task_title"],
            "entry_type": "status_change",
            "new_status": r["new_status"],
        })

    # Deduplicate: if a comment and status change share the same task_id
    # and created_at (within 2s), keep only the comment.
    entries.sort(key=lambda e: _activity_ts_sort_key(e.get("created_at")), reverse=True)
    seen_keys = set()
    deduped = []
    for e in entries:
        key = (e["task_id"], (e["created_at"] or "")[:17])
        if e["entry_type"] == "status_change" and key in seen_keys:
            continue
        seen_keys.add(key)
        deduped.append(e)
    deduped.sort(key=lambda e: _activity_ts_sort_key(e.get("created_at")), reverse=True)
    return JSONResponse({"entries": deduped[:limit]})


@router.get("/api/dev/board-summary")
def api_dev_board_summary(scope: str = "") -> JSONResponse:
    """Lightweight summary for header pills: ready, in-flight, stalled counts
    (wl-28). ``scope`` narrows to one project store (wl-85); empty/"all"
    aggregates across every registered product tracker (wl-40), matching the
    All board's merged view.
    """
    prod = "" if scope.strip().lower() in ("", "all") else scope.strip().lower()
    if prod and get_product(prod) is None:
        return JSONResponse(
            {"ok": False, "error": "Unknown scope"}, status_code=404
        )
    ready_count = _merged_ready_count(prod)
    in_flight_tasks = _merged_in_flight_tasks(prod)
    now = datetime.now(timezone.utc)
    stale_minutes = _claim_stale_minutes()
    stale_cutoff = now - timedelta(minutes=stale_minutes)
    stalled_count = 0
    for t in in_flight_tasks:
        dt = _parse_task_date_utc(t.updated_at)
        if dt is not None and dt < stale_cutoff:
            stalled_count += 1
    return JSONResponse({
        "ready_count": ready_count,
        "in_flight_count": len(in_flight_tasks),
        "stalled_count": stalled_count,
        "stale_minutes": stale_minutes,
    })


@router.get("/api/dev/board-summary/all-scopes")
def api_dev_board_summary_all_scopes() -> JSONResponse:
    """Batch counterpart to /api/dev/board-summary (wl-120): ready/in-flight/
    stalled counts for every discovered product store plus "all", in one call
    — the scope switcher polls this once instead of one request per pill.
    """
    now = datetime.now(timezone.utc)
    stale_cutoff = now - timedelta(minutes=_claim_stale_minutes())

    def _counts(prod: str) -> Dict[str, int]:
        ready_count = _merged_ready_count(prod)
        in_flight_tasks = _merged_in_flight_tasks(prod)
        stalled_count = 0
        for t in in_flight_tasks:
            dt = _parse_task_date_utc(t.updated_at)
            if dt is not None and dt < stale_cutoff:
                stalled_count += 1
        return {
            "ready_count": ready_count,
            "in_flight_count": len(in_flight_tasks),
            "stalled_count": stalled_count,
        }

    scopes: Dict[str, Dict[str, int]] = {"all": _counts("")}
    for spec in discover_products():
        scopes[spec.slug] = _counts(spec.slug)
    return JSONResponse({"scopes": scopes, "stale_minutes": _claim_stale_minutes()})


@router.get("/api/dev/allocation")
def api_dev_allocation(window_days: int = 7, scope: str = "all") -> JSONResponse:
    """wl-160: filed-vs-closed tallies per lane:* label and per signed author
    — the wl-106 derivation, retired from the desk Overview by wl-156, exposed
    as a JSON seam for the dispatch report (oc-22). Reporting doctrine
    (wl-139): the desk computes its comment-derived facts; the board joins
    them to shift data over HTTP, never recomputing them.
    """
    window_days = max(1, min(int(window_days), 90))
    prod = "" if scope.strip().lower() in ("", "all") else scope.strip().lower()
    if prod and get_product(prod) is None:
        return JSONResponse({"ok": False, "error": "Unknown scope"}, status_code=404)
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=window_days)
    all_tasks = _merged_scope_tasks_for_filters(prod)
    resp = JSONResponse({
        "ok": True,
        "generated_at": now.isoformat(),
        "window_days": window_days,
        "since": since.isoformat(),
        "lanes": _allocation_lane_rows(all_tasks, since),
        "authors": _allocation_author_rows(prod, since),
    })
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


# pc-881: Map + suite soft-poll stampede /api/dev/attention (~1s rebuild).
# Short single-flight cache keeps For You truthful without re-walking every store.
# wl-353: rebuild itself is now slim-seed (sub-100ms cold on ~4k tickets); TTL
# still absorbs soft-poll stampede.
_ATTENTION_CACHE_TTL_S = 4.0
_attention_lock = threading.Condition()
_attention_cache: Dict[int, Tuple[float, Dict[str, Any]]] = {}
_attention_inflight: Dict[int, bool] = {}


def _invalidate_attention_cache() -> None:
    """Drop cached attention payloads (snooze / gate truth must not lag TTL)."""
    with _attention_lock:
        _attention_cache.clear()
        _attention_lock.notify_all()


def _build_attention_payload(include_snoozed: int = 0) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    all_items = _collect_founder_attention_items(now=now)
    visible, hidden, snoozes = _partition_attention_items(all_items, now=now)
    items = all_items if include_snoozed else visible
    # wl-205: human-gate activity for the last 24h across all local stores.
    since_24h = (now - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    gate_stats: Dict[str, int] = {}
    for _spec, tr in product_trackers():
        if hasattr(tr, "human_gate_stats_since"):
            for row in tr.human_gate_stats_since(since_24h):
                a = str(row["author"])
                gate_stats[a] = gate_stats.get(a, 0) + int(row["count"])
    human_gate_stats = [
        {"author": a, "count": c}
        for a, c in sorted(gate_stats.items(), key=lambda x: -x[1])
    ]
    # wl-405: band counts always tally the *visible* feed so Map/Overview KPIs
    # cannot treat stalled + inbox-read rows as act-now decisions. sum == visible_count.
    bands = _attention_band_counts(visible)
    return {
        "ok": True,
        "count": len(items) if include_snoozed else len(visible),
        "items": items,
        "visible_count": len(visible),
        "snoozed_count": len(hidden),
        "snoozes": snoozes,
        "stale_minutes": _claim_stale_minutes(),
        "updated_at": now.isoformat(),
        "human_gate_stats": human_gate_stats,
        "human_gate_stats_window_hours": 24,
        "act_now_count": bands["act_now_count"],
        "read_count": bands["read_count"],
        "watch_count": bands["watch_count"],
    }


@router.get("/api/dev/attention")
def api_dev_attention(include_snoozed: int = 0) -> JSONResponse:
    """wl-135: the founder-attention feed — everything blocked on You,
    always all stores. Same shape as /api/dev/board-summary consumers.

    Snoozes (product/kind focus mutes) filter the default feed without
    changing ticket gates. Pass ``include_snoozed=1`` for the full set;
    response always includes ``snoozed_count`` and ``snoozes``.
    """
    key = 1 if include_snoozed else 0
    now_m = time.monotonic()
    with _attention_lock:
        hit = _attention_cache.get(key)
        if hit is not None and (now_m - hit[0]) < _ATTENTION_CACHE_TTL_S:
            resp = JSONResponse(hit[1])
            resp.headers["Cache-Control"] = "no-store, max-age=0"
            resp.headers["X-Attention-Cache"] = "hit"
            return resp
        while _attention_inflight.get(key):
            _attention_lock.wait(timeout=6.0)
            now_m = time.monotonic()
            hit = _attention_cache.get(key)
            if hit is not None and (now_m - hit[0]) < _ATTENTION_CACHE_TTL_S:
                resp = JSONResponse(hit[1])
                resp.headers["Cache-Control"] = "no-store, max-age=0"
                resp.headers["X-Attention-Cache"] = "coalesce"
                return resp
            if not _attention_inflight.get(key):
                break
        _attention_inflight[key] = True

    payload: Optional[Dict[str, Any]] = None
    err: Optional[BaseException] = None
    try:
        payload = _build_attention_payload(include_snoozed=include_snoozed)
    except BaseException as exc:  # noqa: BLE001 — re-raise after unlock
        err = exc
    finally:
        with _attention_lock:
            if payload is not None:
                _attention_cache[key] = (time.monotonic(), payload)
            _attention_inflight[key] = False
            _attention_lock.notify_all()
    if err is not None:
        raise err
    assert payload is not None
    resp = JSONResponse(payload)
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    resp.headers["X-Attention-Cache"] = "miss"
    return resp


@router.post("/api/dev/attention/snooze")
async def api_dev_attention_snooze(request: Request) -> JSONResponse:
    """Mute product/kind/task from Waiting on You until `until` (not a ticket gate).

    Body JSON: ``{ "product": "tradeos", "until": "today"|"1d"|"1w"|ISO,
    "reason": "optional" }`` or ``{ "kind": "embargo", "until": "1w" }``
    or ``{ "task_id": "so-2", "until": "1d" }``
    or ``{ "scope": "all", "until": "today" }``.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    now = datetime.now(timezone.utc)
    product = str(body.get("product") or "").strip().lower()
    kind = str(body.get("kind") or "").strip().lower()
    task_id = str(body.get("task_id") or "").strip().lower()
    scope = str(body.get("scope") or "").strip().lower()
    if not scope:
        if task_id:
            scope = "task"
        elif product:
            scope = "product"
        elif kind:
            scope = "kind"
        else:
            raise HTTPException(400, "task_id, product, kind, or scope=all required")
    if scope == "task" and not task_id:
        raise HTTPException(400, "task_id required for task snooze")
    if scope == "product" and not product:
        raise HTTPException(400, "product required for product snooze")
    if scope == "kind" and not kind:
        raise HTTPException(400, "kind required for kind snooze")
    until_raw = body.get("until") or "today"
    try:
        until = _parse_until_iso(str(until_raw), now=now)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    reason = str(body.get("reason") or "").strip()
    prefs = _load_attention_prefs()
    snoozes = [s for s in (prefs.get("snoozes") or []) if isinstance(s, dict)]

    def _same(s: Dict[str, Any]) -> bool:
        if (s.get("scope") or "product") != scope:
            return False
        if scope == "task":
            return (s.get("task_id") or "").lower() == task_id
        if scope == "product":
            return (s.get("product") or "").lower() == product
        if scope == "kind":
            return (s.get("kind") or "").lower() == kind
        return scope == "all"

    snoozes = [s for s in snoozes if not _same(s)]
    entry: Dict[str, Any] = {
        "scope": scope,
        "task_id": task_id if scope == "task" else "",
        "product": product if scope == "product" else "",
        "kind": kind if scope == "kind" else "",
        "until": until.isoformat(),
        "reason": reason,
        "created_at": now.isoformat(),
    }
    snoozes.append(entry)
    prefs["snoozes"] = snoozes
    _save_attention_prefs(prefs)
    # Snooze mutes must apply on the next GET — don't wait out the 4s TTL.
    _invalidate_attention_cache()
    all_items = _collect_founder_attention_items(now=now)
    visible, hidden, active = _partition_attention_items(all_items, now=now)
    return JSONResponse({
        "ok": True,
        "snooze": entry,
        "visible_count": len(visible),
        "snoozed_count": len(hidden),
        "snoozes": active,
    })


@router.post("/api/dev/attention/unsnooze")
async def api_dev_attention_unsnooze(request: Request) -> JSONResponse:
    """Clear a product/kind/task/all snooze. Body: ``{ "product": "tradeos" }``
    or ``{ "kind": "embargo" }`` or ``{ "task_id": "so-2" }``
    or ``{ "scope": "all" }`` or ``{ "clear": true }``.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    now = datetime.now(timezone.utc)
    prefs = _load_attention_prefs()
    snoozes = [s for s in (prefs.get("snoozes") or []) if isinstance(s, dict)]
    if body.get("clear") is True or str(body.get("scope") or "").lower() == "all" and body.get("clear_all"):
        prefs["snoozes"] = []
        _save_attention_prefs(prefs)
    else:
        product = str(body.get("product") or "").strip().lower()
        kind = str(body.get("kind") or "").strip().lower()
        task_id = str(body.get("task_id") or "").strip().lower()
        scope = str(body.get("scope") or "").strip().lower()
        if not scope:
            if task_id:
                scope = "task"
            elif product:
                scope = "product"
            elif kind:
                scope = "kind"
        if scope == "all" or body.get("clear") is True:
            prefs["snoozes"] = []
        else:
            def _drop(s: Dict[str, Any]) -> bool:
                sc = s.get("scope") or "product"
                if scope and sc != scope:
                    return False
                if task_id and (s.get("task_id") or "").lower() == task_id:
                    return True
                if product and (s.get("product") or "").lower() == product:
                    return True
                if kind and (s.get("kind") or "").lower() == kind:
                    return True
                return False
            prefs["snoozes"] = [s for s in snoozes if not _drop(s)]
        _save_attention_prefs(prefs)
    _invalidate_attention_cache()
    all_items = _collect_founder_attention_items(now=now)
    visible, hidden, active = _partition_attention_items(all_items, now=now)
    return JSONResponse({
        "ok": True,
        "visible_count": len(visible),
        "snoozed_count": len(hidden),
        "snoozes": active,
    })


# ── Dev queue API ───────────────────────────────────────────────────────────

@router.get("/api/dev/queue/ready")
def api_dev_queue_ready() -> JSONResponse:
    """Prioritized, dependency-aware ready queue grouped by file conflicts."""
    tracker = get_default_tracker()
    queue = WorkQueue(tracker)
    ready = queue.ready()
    batches = group_by_file_conflict(ready)
    return JSONResponse({
        "tracker": tracker.name,
        "ready_count": len(ready),
        "batches": [
            {
                **batch.to_dict(),
                "dispatch_prompt": build_dispatch_prompt(batch.tickets),
            }
            for batch in batches
        ],
    })


@router.post("/api/dev/queue/dispatch")
def api_dev_queue_dispatch(task_ids: str = "") -> RedirectResponse:
    """Transition batch tasks to in_progress and redirect with dispatch prompt."""
    if not task_ids:
        return JSONResponse({"error": "task_ids required"}, status_code=400)  # type: ignore[return-value]

    tracker = get_default_tracker()
    ids = [tid.strip() for tid in task_ids.split(",") if tid.strip()]

    transitioned: List[str] = []
    for tid in ids:
        result = tracker.update_status(tid, TaskStatus.IN_PROGRESS)
        if result:
            transitioned.append(tid)

    queue = WorkQueue(tracker)
    tasks = [t for t in queue.all_tasks if t.id in transitioned or (t.ext_id and t.ext_id in transitioned)]
    prompt = build_dispatch_prompt(tasks) if tasks else ""

    q = urlencode(
        {
            "view": "table",
            "dispatched": ",".join(transitioned),
            "prompt": prompt,
        }
    )
    return RedirectResponse(url=f"{TICKETS_APP_ALL}?{q}", status_code=303)


@router.get("/api/dev/queue/in-flight")
def api_dev_queue_in_flight() -> JSONResponse:
    """Tickets actively in flight: in_progress + in_review (wl-28; replaces the
    old /api/dev/queue/orphans, which counted all in_progress tasks as
    'orphaned' — a pre-pool relic that red-flagged healthy live work).
    Aggregated across every registered product tracker (wl-40)."""
    in_flight = _merged_in_flight_tasks()
    return JSONResponse({
        "tracker": "merged",
        "in_flight": [t.to_dict() for t in in_flight],
    })


@router.post("/api/dev/queue/shutdown")
def api_dev_queue_shutdown(apply: int = 0) -> JSONResponse:
    """Run the close-out protocol against every in-progress ticket."""
    tracker = get_default_tracker()
    report = run_shutdown(tracker, apply=bool(apply))
    return JSONResponse({
        "tracker": tracker.name,
        **report.to_dict(),
    })


@router.get("/api/admin/identity")
def api_get_identity() -> JSONResponse:
    return JSONResponse({"ok": True, **_identity_config()})


@router.patch("/api/admin/identity")
async def api_patch_identity(request: Request) -> JSONResponse:
    try:
        payload: Dict[str, Any] = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)
    cfg = _identity_config()
    if "founder_alias" in payload:
        alias = str(payload["founder_alias"] or "").strip()
        if len(alias) > 60:
            return JSONResponse(
                {"ok": False, "error": "alias too long (max 60)"}, status_code=400)
        cfg["founder_alias"] = alias
    if "founder_id" in payload:
        fid = str(payload["founder_id"] or "").strip()
        if not _FOUNDER_ID_RE.match(fid):
            return JSONResponse(
                {"ok": False, "error": "founder_id must be a kebab-case §5.2 id"},
                status_code=400)
        cfg["founder_id"] = fid
    path = _identity_config_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cfg, indent=1), encoding="utf-8")
    except OSError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    return JSONResponse({"ok": True, **cfg})
