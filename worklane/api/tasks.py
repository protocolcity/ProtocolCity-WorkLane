"""Task CRUD and dev API routes extracted from task_server (wl-225)."""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from worklane import archival
from worklane.board import (
    TASK_ID_PREFIX_OPS,
    TASK_ID_PREFIX_TRADEOS,
    TICKETS_APP_ALL,
    _OWNER_LINE_RE,
    _claim_stale_minutes,
    _load_preview_comments_multi,
    _render_task_card,
    column_counts_for_scope_multi,
    get_ops_ticket_tracker,
    ops_tickets_db_path,
    parse_wq_priority,
    parse_wq_product,
    resolve_wq_product,
    status_counts_for_scope_multi,
)
from worklane.devqueue import (
    WorkQueue,
    build_dispatch_prompt,
    group_by_file_conflict,
    run_shutdown,
)
from worklane.products import (
    all_taken_prefixes,
    default_product_slug,
    discover_products,
    get_product,
    live_feed_product_slug,
    product_tracker,
    product_trackers,
    register_product_meta,
    split_task_id,
    wl_data_dir,
)
from worklane.rendering import render_markdown
from worklane.server_helpers import (
    _activity_ts_sort_key,
    _allocation_author_rows,
    _allocation_lane_rows,
    _collect_founder_attention_items,
    _get_task_hot_or_archive,
    _list_tasks_for_wq_multi_resolved,
    _load_attention_prefs,
    _merged_in_flight_tasks,
    _merged_ready_count,
    _merged_scope_tasks_for_filters,
    _parse_task_date_utc,
    _parse_until_iso,
    _partition_attention_items,
    _request_tradeos_json,
    _resolve_product_tracker,
    _save_attention_prefs,
    _task_relations_dicts,
    _tradeos_tickets_use_http_feed,
    _FOUNDER_ID_RE,
    _identity_config,
    _identity_config_path,
)
from worklane.notify import notify_done
from worklane.trackers import (
    TaskStatus,
    get_default_tracker,
    task_is_gated,
)
from worklane.wake_nudge import (
    gate_of,
    maybe_wake_hand,
    previous_hand_from_labels,
)

logger = logging.getLogger(__name__)
router = APIRouter()

DEFAULT_AGENT_ID = "founder"


def _project_from_request(
    request: Request, payload: Optional[Dict[str, Any]] = None
) -> Optional[str]:
    """Extract explicit project/product scope for write addressing (wl-344).

    Preference: body ``project`` → body ``product`` → query ``project`` →
    query ``product``. Empty strings ignored. Conflicting non-empty body
    values are not resolved here — callers that need conflict detection
    (create) handle it themselves; write mutation endpoints treat the first
    non-empty as the explicit store.
    """
    body = payload or {}
    for key in ("project", "product"):
        raw = body.get(key)
        if raw not in (None, ""):
            return str(raw).strip().lower()
    for key in ("project", "product"):
        raw = request.query_params.get(key) if request is not None else None
        if raw not in (None, ""):
            return str(raw).strip().lower()
    return None


def _resolve_write_tracker(
    task_id: str, project: Optional[str] = None
) -> Tuple[Optional[Tuple[str, str, Any]], Optional[JSONResponse]]:
    """Write-path task addressing (wl-344).

    Returns ``((surf, raw_id, tracker), None)`` on success, or
    ``(None, JSONResponse 400)`` when the id is bare without project= or
    when composite prefix disagrees with project=.
    """
    try:
        return (
            _resolve_product_tracker(task_id, project=project, write=True),
            None,
        )
    except ValueError as exc:
        return None, JSONResponse(
            {"ok": False, "error": str(exc)}, status_code=400
        )


def _workforce_roster_path() -> Optional[str]:
    """Return the local WorkForce roster.json path from env or auto-discovery.

    Checks WL_WORKFORCE_ROSTER (or WL_WORKFORCE_ROSTER) first, then derives the path from
    WORKFORCE_PREDIRTY ($ROOT/local/run/predirty-*.txt → $ROOT/local/roster.json).
    Finally, walks up from CWD looking for .protocolcity/workforce/local/roster.json
    (skipped when WL_WORKFORCE_NO_CITY_ROSTER=1 — set that in test fixtures).
    Returns None when no source is found.
    """
    explicit = (os.environ.get("WL_WORKFORCE_ROSTER") or os.environ.get("WL_WORKFORCE_ROSTER", "")).strip()
    if explicit:
        return explicit
    predirty = os.environ.get("WORKFORCE_PREDIRTY", "").strip()
    if predirty:
        try:
            return str(Path(predirty).parent.parent / "roster.json")
        except Exception:
            return None
    if os.environ.get("WL_WORKFORCE_NO_CITY_ROSTER", "").strip() not in ("1", "true", "yes"):
        try:
            here = Path.cwd().resolve()
            for _ in range(6):
                candidate = here / ".protocolcity" / "workforce" / "local" / "roster.json"
                if candidate.exists():
                    return str(candidate)
                parent = here.parent
                if parent == here:
                    break
                here = parent
        except Exception:
            pass
    return None


def _workforce_workers_for_product(product_slug: str) -> List[str]:
    """Return lane worker names hired for *product_slug* per the WorkForce roster.

    Primary: queries WL_WORKFORCE_URL (or WL_WORKFORCE_URL)/api/workers?light=1 with a
    3-second timeout (wl-308: ?light=1 returns in <50ms when WorkForce supports it; until
    then the 404 falls through immediately to the roster fallback, avoiding the full 3s
    wait from the unparameterized endpoint which serialises 25 queue probes at ~11s total).
    Fallback (wl-287, wl-306): when the API is unreachable or too slow, reads the local
    roster from WL_WORKFORCE_ROSTER / WL_WORKFORCE_ROSTER, WORKFORCE_PREDIRTY, or the
    city root auto-discovered at .protocolcity/workforce/local/roster.json.
    Returns an empty list on all failures — the caller must never block on
    this (wl-256: soft warning only, never a gate).

    A worker is considered hired for the product when its ``queue_url`` contains
    ``product=<slug>`` (the convention all current lanes follow).
    """
    needle = "product=" + product_slug
    url = (os.environ.get("WL_WORKFORCE_URL") or os.environ.get("WL_WORKFORCE_URL", "http://127.0.0.1:8797")) + "/api/workers?light=1"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        workers = data.get("workers") or []
        if isinstance(workers, list):
            return [
                "worker:" + w["name"]
                for w in workers
                if w.get("kind") == "lane" and needle in (w.get("queue_url") or "")
            ]
        if isinstance(workers, dict):
            return [
                "worker:" + name
                for name, w in workers.items()
                if isinstance(w, dict) and w.get("kind") == "lane"
                and needle in (w.get("queue_url") or "")
            ]
    except Exception:
        pass
    # Fallback: local roster.json when the WorkForce service is unavailable.
    roster_path = _workforce_roster_path()
    if not roster_path:
        return []
    try:
        with open(roster_path) as fh:
            data = json.load(fh)
        workers = data.get("workers") or {}
        if isinstance(workers, dict):
            return [
                "worker:" + name
                for name, w in workers.items()
                if isinstance(w, dict) and w.get("kind") == "lane"
                and needle in (w.get("queue_url") or "")
            ]
    except Exception:
        pass
    return []


_PRODUCT_FROM_URL_RE = re.compile(r"[?&]product=([^&]+)")


def _workforce_products_for_workers() -> Dict[str, str]:
    """Return {worker_id: product_slug} for all known lane workers (wl-296).

    Inverse of _workforce_workers_for_product — used for cross-product guard.
    Same API / roster fallback chain; returns empty dict on all failures so
    the caller is never blocked.
    """
    url = (os.environ.get("WL_WORKFORCE_URL") or os.environ.get("WL_WORKFORCE_URL", "http://127.0.0.1:8797")) + "/api/workers?light=1"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        workers = data.get("workers") or []
        result: Dict[str, str] = {}
        if isinstance(workers, list):
            for w in workers:
                if w.get("kind") != "lane":
                    continue
                m = _PRODUCT_FROM_URL_RE.search(w.get("queue_url") or "")
                if m:
                    result[w["name"]] = m.group(1)
        elif isinstance(workers, dict):
            for name, w in workers.items():
                if not isinstance(w, dict) or w.get("kind") != "lane":
                    continue
                m = _PRODUCT_FROM_URL_RE.search(w.get("queue_url") or "")
                if m:
                    result[name] = m.group(1)
        return result
    except Exception:
        pass
    roster_path = _workforce_roster_path()
    if not roster_path:
        return {}
    try:
        with open(roster_path) as fh:
            data = json.load(fh)
        workers = data.get("workers") or {}
        result = {}
        if isinstance(workers, dict):
            for name, w in workers.items():
                if not isinstance(w, dict) or w.get("kind") != "lane":
                    continue
                m = _PRODUCT_FROM_URL_RE.search(w.get("queue_url") or "")
                if m:
                    result[name] = m.group(1)
        return result
    except Exception:
        pass
    return {}


def _tracker_db_path(tracker: Any) -> Path:
    """Resolve the SQLite path for a tracker (HTTP-raising variant)."""
    path = getattr(tracker, "_db_path", None)
    if path is None:
        raise HTTPException(status_code=500, detail="tracker has no local db path")
    return Path(path)


# ── Product management ──────────────────────────────────────────────────────

@router.get("/api/admin/products")
def api_list_products() -> JSONResponse:
    """List all registered product stores (wl-253): slug, display, prefix, db_path."""
    specs = discover_products()
    return JSONResponse({
        "ok": True,
        "products": [
            {
                "slug": s.slug,
                "display": s.display,
                "prefix": s.prefix,
                "db_path": str(s.db_path),
            }
            for s in specs
        ],
    })


@router.post("/api/admin/products")
async def api_create_product(request: Request) -> JSONResponse:
    """Bootstrap a new product store (wl-12): creates ``<slug>.db`` and
    returns its surface. Deliberate by design — no implicit creation from a
    typo'd ``surface=`` on ``/api/admin/tasks``; this is the only door in.
    """
    from worklane.trackers.sqlite import SQLiteTracker  # noqa: PLC0415
    from worklane.task_server import _city_neighborhood_slugs  # noqa: PLC0415

    try:
        payload: Dict[str, Any] = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)

    slug = str(payload.get("slug") or "").strip().lower()
    if not slug:
        return JSONResponse({"ok": False, "error": "slug is required"}, status_code=400)
    if not re.match(r"^[a-z][a-z0-9_-]{0,39}$", slug):
        return JSONResponse(
            {
                "ok": False,
                "error": "slug must start with a letter and contain only lowercase "
                "letters, digits, '-' or '_' (max 40 chars)",
            },
            status_code=400,
        )
    if slug in ("all", "ops", "op"):
        return JSONResponse(
            {"ok": False, "error": f"{slug!r} is a reserved surface name"},
            status_code=400,
        )

    existing = get_product(slug)
    if existing is not None and (existing.db_path.exists() or slug == live_feed_product_slug()):
        return JSONResponse(
            {"ok": False, "error": f"project {slug!r} already exists"},
            status_code=409,
        )

    display = str(payload.get("display") or "").strip() or None
    prefix = str(payload.get("prefix") or "").strip().lower() or None
    if prefix is not None:
        if not re.match(r"^[a-z][a-z0-9]{1,7}$", prefix):
            return JSONResponse(
                {
                    "ok": False,
                    "error": "prefix must be 2-8 lowercase letters/digits, starting with a letter",
                },
                status_code=400,
            )
        taken = all_taken_prefixes()
        if prefix in taken:
            return JSONResponse(
                {"ok": False, "error": f"prefix {prefix!r} is already used by another project"},
                status_code=400,
            )

    db_path = wl_data_dir() / f"{slug}.db"
    tracker = SQLiteTracker(db_path=db_path, product_default=f"product:{slug}")
    tracker.list_tasks(limit=1)  # forces _connect(), materializing the file + schema

    if display or prefix:
        register_product_meta(slug, display=display, prefix=prefix)

    spec = get_product(slug)
    if spec is None:
        return JSONResponse(
            {"ok": False, "error": "project store created but not discoverable — check runtime dir"},
            status_code=500,
        )
    # wl-155 / wl-270: soft founding-path guardrail — city joins store to
    # neighborhood by slug == slugify(dirname) (pc-313: lower + whitespace→-);
    # warn (never refuse) when no such folder exists. Skips silently outside
    # a city (host-neutral).
    warning = None
    hoods = _city_neighborhood_slugs()
    if hoods is not None and slug not in hoods:
        warning = (
            f"store created, but no neighborhood folder whose slug is {slug!r} "
            "exists at the city root — the ProtocolCity map won't show a "
            "building until one does (slug = folder name lowercased with "
            "whitespace runs collapsed to hyphens; e.g. 'Work Folder' → "
            "'work-folder')"
        )
    return JSONResponse(
        {
            "ok": True,
            "warning": warning,
            "product": {
                "slug": spec.slug,
                "display": spec.display,
                "prefix": spec.prefix,
                "db_path": str(spec.db_path),
            },
        }
    )


@router.patch("/api/admin/products/{slug}")
async def api_update_product(slug: str, request: Request) -> JSONResponse:
    """Rename a product's display name / id prefix (wl-17): writes the
    ``local/config/products.json`` overlay via :func:`register_product_meta`.
    Editing only — the store itself is untouched, and ids already stored
    with the old prefix keep rendering under the new one (composite ids are
    computed at render time, never rewritten).
    """
    spec = get_product(slug)
    if spec is None:
        return JSONResponse({"ok": False, "error": f"unknown project {slug!r}"}, status_code=404)

    try:
        payload: Dict[str, Any] = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)

    display = payload.get("display")
    display = str(display).strip() if display is not None else None
    prefix = payload.get("prefix")
    prefix = str(prefix).strip().lower() if prefix is not None else None

    if display is None and prefix is None:
        return JSONResponse(
            {"ok": False, "error": "at least one of display/prefix is required"},
            status_code=400,
        )
    if display is not None and not display:
        return JSONResponse({"ok": False, "error": "display cannot be blank"}, status_code=400)
    if prefix is not None:
        if not re.match(r"^[a-z][a-z0-9]{1,7}$", prefix):
            return JSONResponse(
                {
                    "ok": False,
                    "error": "prefix must be 2-8 lowercase letters/digits, starting with a letter",
                },
                status_code=400,
            )
        if prefix == "o":
            return JSONResponse(
                {"ok": False, "error": "prefix 'o' is reserved (legacy ops store)"},
                status_code=400,
            )
        taken = all_taken_prefixes(exclude_slug=slug)
        if prefix in taken:
            return JSONResponse(
                {"ok": False, "error": f"prefix {prefix!r} is already used by another project"},
                status_code=400,
            )

    # A real prefix rename retires the old prefix into legacy_prefixes (wl-152)
    # so every composite id already written under it — comments, close-out
    # Links:, commit messages, bookmarks — keeps resolving forever.
    old_prefix = spec.prefix
    retiring_prefix = old_prefix if (prefix is not None and prefix != old_prefix) else None
    register_product_meta(
        slug, display=display, prefix=prefix, add_legacy_prefix=retiring_prefix
    )
    updated = get_product(slug)
    if updated is None:
        return JSONResponse(
            {"ok": False, "error": "project updated but no longer discoverable"},
            status_code=500,
        )
    return JSONResponse(
        {
            "ok": True,
            "product": {
                "slug": updated.slug,
                "display": updated.display,
                "prefix": updated.prefix,
                "db_path": str(updated.db_path),
            },
        }
    )


@router.post("/api/admin/products/{slug}/compact")
async def api_compact_product(slug: str, request: Request) -> JSONResponse:
    """Move cold done/canceled tickets into the sibling archive DB (wl-23).

    Archival is move-not-delete and reversible. Default age is 90 days.
    Body (optional JSON): ``{"older_than_days": 90}``.
    """
    s = (slug or "").strip().lower()
    spec = get_product(s)
    if spec is None:
        return JSONResponse({"ok": False, "error": f"unknown project {s!r}"}, status_code=404)

    older_than_days = archival.DEFAULT_ARCHIVE_AGE_DAYS
    try:
        payload: Dict[str, Any] = await request.json()
    except Exception:
        payload = {}
    if isinstance(payload, dict) and "older_than_days" in payload:
        try:
            older_than_days = int(payload["older_than_days"])
        except (TypeError, ValueError):
            return JSONResponse(
                {"ok": False, "error": "older_than_days must be an integer"},
                status_code=400,
            )
        if older_than_days < 1:
            return JSONResponse(
                {"ok": False, "error": "older_than_days must be >= 1"},
                status_code=400,
            )

    tracker = product_tracker(spec)
    hot = _tracker_db_path(tracker) or Path(spec.db_path)
    result = archival.archive_cold_tickets(hot, older_than_days=older_than_days)
    archive_path = archival.archive_db_path_for(hot)
    return JSONResponse(
        {
            "ok": True,
            "product": s,
            "tickets": result.tickets,
            "comments": result.comments,
            "relations": result.relations,
            "older_than_days": older_than_days,
            "source_path": result.source_path,
            "archive_path": result.archive_path,
            "archive_count": archival.archive_counts(archive_path),
        }
    )


# ── Task CRUD ───────────────────────────────────────────────────────────────

@router.post("/api/admin/tasks")
async def api_create_task(request: Request) -> JSONResponse:
    try:
        payload: Dict[str, Any] = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)

    title = (payload.get("title") or "").strip()
    if not title:
        return JSONResponse({"ok": False, "error": "title is required"}, status_code=400)

    # PROTOCOL.md §5 intake + §3.8 identity: tickets are filed by a signed
    # agent with a real problem statement — not bare titles.
    author = str(payload.get("author") or payload.get("created_by") or "").strip()
    if not author:
        return JSONResponse(
            {
                "ok": False,
                "error": "author is required — sign ticket intake with your "
                         "canonical agent id (PROTOCOL.md §3.8/§5.2), e.g. "
                         '"author": "work-pool"',
            },
            status_code=400,
        )
    description = str(payload.get("description") or "")
    if not description.strip():
        return JSONResponse(
            {
                "ok": False,
                "error": "description is required — state the problem and the "
                         "expected outcome (PROTOCOL.md §5 intake)",
            },
            status_code=400,
        )
    status_val = str(payload.get("status") or TaskStatus.BACKLOG)
    if status_val not in TaskStatus.ALL:
        return JSONResponse(
            {"ok": False, "error": f"unknown status {status_val!r}"},
            status_code=400,
        )
    try:
        priority = int(payload.get("priority") or 3)
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "priority must be an integer"}, status_code=400)

    labels_raw = payload.get("labels") or []
    if isinstance(labels_raw, str):
        labels = [s.strip() for s in labels_raw.split(",") if s.strip()]
    else:
        labels = [str(s).strip() for s in labels_raw if str(s).strip()]

    ext_id: Optional[str] = payload.get("ext_id") or None
    intake_val = str(payload.get("intake") or "").strip() or None

    def _sign_intake(tracker: Any, raw_id: Any) -> None:
        """Record the filer on the ticket (tasks have no creator column —
        the signed comment trail is the §5.2 record)."""
        try:
            tracker.add_comment(str(raw_id), f"Intake: filed by {author}", author=author)
        except Exception:
            pass

    # project (wl-64) is the canonical field name; product / ticket_surface /
    # surface remain silent back-compat aliases. Reject rather than silently
    # pick when both are given with different values (PROTOCOL.md §5.2 rule).
    # wl-344: ``product=`` was previously ignored on create (ts-2423 bleed).
    project_val = payload.get("project")
    product_alias = payload.get("product")
    legacy_surface_val = payload.get("ticket_surface") or payload.get("surface")
    if (
        project_val not in (None, "")
        and product_alias not in (None, "")
        and str(project_val).strip().lower()
        != str(product_alias).strip().lower()
    ):
        return JSONResponse(
            {
                "ok": False,
                "error": (
                    f"conflicting project/product values: project={project_val!r} "
                    f"product={product_alias!r} — pass only one"
                ),
            },
            status_code=400,
        )
    if project_val in (None, "") and product_alias not in (None, ""):
        project_val = product_alias
    if (
        project_val not in (None, "")
        and legacy_surface_val not in (None, "")
        and str(project_val).strip().lower() != str(legacy_surface_val).strip().lower()
    ):
        return JSONResponse(
            {
                "ok": False,
                "error": f"conflicting project/surface values: project={project_val!r} "
                         f"surface={legacy_surface_val!r} — pass only one",
            },
            status_code=400,
        )
    surface = project_val or legacy_surface_val or default_product_slug()
    surface = str(surface).strip().lower()

    # Create-path routing (wl-274 B): hard-require worker:* when hired hands exist;
    # pre-hire still soft-stamps needs:routing. worker:you is a valid seat.
    from worklane.routing_labels import ensure_create_labels

    hired = _workforce_workers_for_product(surface)
    labels, stamped_nr, route_err = ensure_create_labels(
        labels, hired_hands=hired, hard_when_hands=True
    )
    if route_err:
        return JSONResponse(
            {"ok": False, "error": route_err},
            status_code=400,
        )
    routing_warning: Optional[str] = None
    if stamped_nr:
        routing_warning = (
            "no worker:* label and no known hands for this store — stamped needs:routing "
            "so unrouted ready stays visible. After hire, create requires "
            "worker:<persona> or worker:you. " + (
                "Hired hands: " + ", ".join(hired) + ". "
                if hired
                else ""
            )
        )

    # wl-296: cross-product mismatch guard — warn/reject when the worker's
    # roster queue_url points at a different product than the ticket's store.
    from worklane.routing_labels import (  # noqa: PLC0415
        check_worker_product_mismatch,
        worker_ids_from_labels,
    )
    _wids = worker_ids_from_labels(labels)
    if _wids:
        _all_wp = _workforce_products_for_workers()
        _mismatch = check_worker_product_mismatch(_wids, surface, _all_wp)
        if _mismatch:
            _hard = os.environ.get("WL_WORKER_PRODUCT_HARD_REJECT", "").lower() in ("1", "true", "yes")
            if _hard:
                return JSONResponse({"ok": False, "error": _mismatch}, status_code=400)
            routing_warning = (_mismatch + " | " + routing_warning) if routing_warning else _mismatch

    def _wake_after_create(public_id: str, task_labels: list, st: str, gt: Optional[str] = None) -> None:
        # wl-359: route-event nudge — create with a claimable worker:<hand> seat.
        maybe_wake_hand(
            task_labels,
            status=st,
            gate_type=gt,
            only_on_seat_change=True,
            previous_hand=None,
            task_id=public_id,
        )

    create_gate = payload.get("gate_type")
    create_gate_s = str(create_gate).strip() if create_gate not in (None, "") else None

    if surface in ("ops", "op"):
        tracker = get_ops_ticket_tracker()
        prefix = TASK_ID_PREFIX_OPS
        task = tracker.create_task(
            title=title,
            description=description,
            status=status_val,
            priority=priority,
            labels=labels,
            ext_id=ext_id,
            actor=author,
            intake=intake_val,
        )
        _sign_intake(tracker, task.id)
        out = task.to_dict()
        out["id"] = f"{prefix}-{task.id}"
        _wake_after_create(out["id"], labels, status_val, create_gate_s or gate_of(task))
        return JSONResponse({"ok": True, "task": out, "routing_warning": routing_warning})

    spec = get_product(surface)
    if spec is None:
        from worklane.products import unknown_product_message

        return JSONResponse(
            {
                "ok": False,
                "error": (
                    f"unknown ticket surface {surface!r} — no "
                    f"{surface}.db project store; {unknown_product_message(surface)}"
                ),
            },
            status_code=400,
        )

    if spec.slug != live_feed_product_slug():
        tracker = product_tracker(spec)
        task = tracker.create_task(
            title=title,
            description=description,
            status=status_val,
            priority=priority,
            labels=labels,
            ext_id=ext_id,
            actor=author,
            intake=intake_val,
        )
        _sign_intake(tracker, task.id)
        out = task.to_dict()
        out["id"] = f"{spec.prefix}-{task.id}"
        _wake_after_create(out["id"], labels, status_val, create_gate_s or gate_of(task))
        return JSONResponse({"ok": True, "task": out, "routing_warning": routing_warning})

    if _tradeos_tickets_use_http_feed():
        code, data = _request_tradeos_json(
            "POST",
            "/api/ops/tickets/tradeos",
            {
                "title": title,
                "description": description,
                "status": status_val,
                "priority": priority,
                "labels": labels,
                "ext_id": ext_id,
            },
        )
        if code < 0 or code >= 400 or not data or not data.get("ok"):
            err = (data or {}).get("error") if isinstance(data, dict) else None
            return JSONResponse(
                {
                    "ok": False,
                    "error": err or "tradeOS ticket API unreachable",
                },
                status_code=502 if code < 0 else 400,
            )
        tm = data.get("task") or {}
        out = dict(tm) if isinstance(tm, dict) else {}
        rid = str(out.get("id") or "")
        out["id"] = f"{TASK_ID_PREFIX_TRADEOS}-{rid}"
        _wake_after_create(out["id"], labels, status_val, create_gate_s)
        return JSONResponse({"ok": True, "task": out, "routing_warning": routing_warning})

    tracker = get_default_tracker()
    task = tracker.create_task(
        title=title,
        description=description,
        status=status_val,
        priority=priority,
        labels=labels,
        ext_id=ext_id,
        intake=intake_val,
    )
    _sign_intake(tracker, task.id)
    out = task.to_dict()
    out["id"] = f"{TASK_ID_PREFIX_TRADEOS}-{task.id}"
    _wake_after_create(out["id"], labels, status_val, create_gate_s or gate_of(task))
    return JSONResponse({"ok": True, "task": out, "routing_warning": routing_warning})


@router.get("/api/admin/tasks/ready")
def api_tasks_ready(
    product: str = "",
    label: str = "",
    worker: str = "",
    explain: int = 0,
    limit: int = 200,
) -> JSONResponse:
    """Dispatch-ready backlog tickets (wl-20 structured relations).

    Uses ``blocks`` edges in ``task_relations``. When ``explain=1``, each
    ticket includes ``ready`` / ``blocked_by`` detail (ready list is only
    the ready ones; full backlog explain is under ``explain`` when set).
    Prose ``Depends on #N`` remains the intake shim — it is not replaced
    here; materialize via the dry-run backfill script.

    ``worker=<name>`` applies the assignment law for default lanes
    (wl-191): a ticket carrying any ``worker:*`` label is ready for the
    caller only when ``worker:<name>`` is among them; unlabeled tickets
    stay ready for everyone. Narrow lanes keep ``label=`` (strict
    include); the two filters compose.
    """
    from worklane import relations as relmod  # noqa: PLC0415

    # wl-219: aliases (worklane→worklane) + fail closed on unknown
    prod, scope_ok = resolve_wq_product(product)
    if not scope_ok:
        return JSONResponse(
            {"ok": False, "error": "unknown project %r" % (str(product).strip(),)},
            status_code=400,
        )
    if not prod:
        return JSONResponse(
            {
                "ok": False,
                "error": "product query param is required (single project store)",
            },
            status_code=400,
        )
    if prod in ("ops", "op"):
        tracker = get_ops_ticket_tracker()
        prefix = TASK_ID_PREFIX_OPS
    else:
        spec = get_product(prod)
        if spec is None:
            return JSONResponse(
                {"ok": False, "error": f"unknown project {prod!r}"},
                status_code=400,
            )
        tracker = product_tracker(spec)
        prefix = spec.prefix

    try:
        db_path = _tracker_db_path(tracker)
    except HTTPException:
        return JSONResponse(
            {"ok": False, "error": "project store is not a local SQLite tracker"},
            status_code=400,
        )

    tasks = tracker.list_tasks(status=TaskStatus.BACKLOG, limit=None)
    tasks = [t for t in tasks if not task_is_gated(t)]
    if label:
        lab = label.strip()
        tasks = [t for t in tasks if lab in (t.labels or [])]
    if worker:
        me = "worker:" + worker.strip().lower()

        def _claimable(t: Any) -> bool:
            assigned = [l for l in (t.labels or []) if l.startswith("worker:")]
            return not assigned or me in assigned

        tasks = [t for t in tasks if _claimable(t)]

    status_by_id = relmod.load_status_map(db_path)
    # Include non-backlog statuses for blocker resolution (done/canceled).
    for t in tracker.list_tasks(limit=None):
        status_by_id[str(t.id)] = t.status

    edges = relmod.list_relations(db_path)
    backlog_ids = [str(t.id) for t in tasks]
    explained = relmod.explain_ready(backlog_ids, status_by_id, edges)

    # Stable priority order matching WorkQueue.
    by_id = {str(t.id): t for t in tasks}
    ready_raw = [tid for tid in backlog_ids if explained[tid].ready]
    ready_raw.sort(
        key=lambda tid: (
            by_id[tid].priority if 1 <= int(by_id[tid].priority or 3) <= 4 else 99,
            by_id[tid].updated_at or "",
        )
    )
    ready_raw = ready_raw[: max(0, int(limit or 200))]

    def _pub(tid: str) -> str:
        return f"{prefix}-{tid}"

    ready_out: List[Dict[str, Any]] = []
    for tid in ready_raw:
        t = by_id[tid]
        entry = t.to_dict()
        entry["id"] = _pub(tid)
        if explain:
            info = explained[tid]
            entry["ready"] = True
            entry["blocked_by"] = [_pub(b) if b.isdigit() else b for b in info.blocked_by]
        ready_out.append(entry)

    payload: Dict[str, Any] = {
        "ok": True,
        "product": prod,
        "count": len(ready_out),
        "tasks": ready_out,
    }
    if explain:
        # Full backlog matrix (not just ready) for bd ready --explain parity.
        matrix = []
        for tid in backlog_ids:
            info = explained[tid]
            matrix.append(
                {
                    "id": _pub(tid),
                    "ready": info.ready,
                    "blocked_by": [
                        _pub(b) if str(b).isdigit() else b for b in info.blocked_by
                    ],
                    "status": info.status,
                    "title": by_id[tid].title if tid in by_id else "",
                }
            )
        payload["explain"] = matrix
    return JSONResponse(payload)


@router.get("/api/admin/tasks/resolve")
def api_tasks_resolve(id: str = "") -> JSONResponse:
    """Resolve a Jump-# box entry to a composite task id (wl-76).

    Composite ids (``wl-503``) are the caller's job to build directly —
    this endpoint only exists for the ambiguous case: a bare number typed
    in the All view, where the same sequence number can exist in more than
    one product store. Looks the raw id up in every store's hot + archive
    tables and reports back a single match, the full candidate list when
    more than one store has that number, or not_found.
    """
    raw = (id or "").strip().lstrip("#")
    if not raw:
        return JSONResponse({"ok": False, "error": "empty"}, status_code=400)
    if not raw.isdigit():
        return JSONResponse({"ok": False, "error": "invalid"}, status_code=400)

    candidates: List[Dict[str, Any]] = []
    for spec, tracker in product_trackers():
        task, _comments, _archived = _get_task_hot_or_archive(tracker, raw)
        if task is not None:
            candidates.append({"id": f"{spec.prefix}-{raw}", "title": task.title})

    if not candidates:
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
    if len(candidates) == 1:
        return JSONResponse({"ok": True, "match": candidates[0]["id"]})
    return JSONResponse({"ok": True, "candidates": candidates})


@router.get("/api/admin/tasks/{task_id}/relations")
def api_list_task_relations(task_id: str) -> JSONResponse:
    """List structured relations touching ``task_id`` (wl-20)."""
    from worklane import relations as relmod  # noqa: PLC0415

    surf, raw_id, tracker = _resolve_product_tracker(task_id)
    task = tracker.get_task(raw_id)
    if task is None:
        return JSONResponse({"ok": False, "error": "task not found"}, status_code=404)
    try:
        db_path = _tracker_db_path(tracker)
    except HTTPException:
        return JSONResponse(
            {"ok": False, "error": "project store is not a local SQLite tracker"},
            status_code=400,
        )
    rels = relmod.list_relations(db_path, task_id=raw_id)
    # Re-prefix endpoints for composite-id clients when possible.
    spec = get_product(surf) if surf not in ("ops", "op") else None
    prefix = TASK_ID_PREFIX_OPS if surf in ("ops", "op") else (spec.prefix if spec else "t")

    def _pub(tid: str) -> str:
        return f"{prefix}-{tid}"

    out = []
    for r in rels:
        d = r.to_dict()
        d["from_id"] = _pub(r.from_id)
        d["to_id"] = _pub(r.to_id)
        out.append(d)
    return JSONResponse({"ok": True, "task_id": task_id, "relations": out})


@router.post("/api/admin/tasks/{task_id}/relations")
async def api_create_task_relation(task_id: str, request: Request) -> JSONResponse:
    """Create a directed relation from ``task_id`` to ``to_id`` (wl-20).

    Body::
        {"to_id": "wl-5" | "5", "relation_type": "blocks"|...}

    ``blocks`` means path task blocks ``to_id``. Cycle detection rejects
    edges that would close a loop on blocks/parent-child.
    """
    from worklane import relations as relmod  # noqa: PLC0415

    try:
        payload: Dict[str, Any] = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)

    to_raw = payload.get("to_id") or payload.get("to") or payload.get("target")
    if to_raw is None or str(to_raw).strip() == "":
        return JSONResponse(
            {"ok": False, "error": "to_id is required"},
            status_code=400,
        )
    rel_type = str(
        payload.get("relation_type") or payload.get("type") or ""
    ).strip()

    resolved, err = _resolve_write_tracker(
        task_id, _project_from_request(request, payload)
    )
    if err is not None:
        return err
    assert resolved is not None
    surf, raw_from, tracker = resolved
    # Target may be bare or composite; same product only.
    to_str = str(to_raw).strip()
    if "-" in to_str and not to_str.isdigit():
        to_surf, raw_to = split_task_id(to_str)
        if to_surf != surf and not (
            surf in ("ops", "op") and to_surf in ("ops", "op")
        ):
            return JSONResponse(
                {
                    "ok": False,
                    "error": "cross-project relations are not supported",
                },
                status_code=400,
            )
    else:
        raw_to = to_str.lstrip("#")

    if tracker.get_task(raw_from) is None:
        return JSONResponse({"ok": False, "error": "task not found"}, status_code=404)
    if tracker.get_task(raw_to) is None:
        return JSONResponse(
            {"ok": False, "error": f"to_id {to_str!r} not found"},
            status_code=404,
        )

    try:
        db_path = _tracker_db_path(tracker)
        created = relmod.create_relation(db_path, raw_from, raw_to, rel_type)
    except relmod.RelationError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except HTTPException:
        return JSONResponse(
            {"ok": False, "error": "project store is not a local SQLite tracker"},
            status_code=400,
        )

    spec = get_product(surf) if surf not in ("ops", "op") else None
    prefix = TASK_ID_PREFIX_OPS if surf in ("ops", "op") else (spec.prefix if spec else "t")
    d = created.to_dict()
    d["from_id"] = f"{prefix}-{created.from_id}"
    d["to_id"] = f"{prefix}-{created.to_id}"
    return JSONResponse({"ok": True, "relation": d})


@router.delete("/api/admin/tasks/{task_id}/relations/{relation_id}")
def api_delete_task_relation(
    task_id: str, relation_id: str, request: Request
) -> JSONResponse:
    """Delete a relation by id; task_id must be an endpoint of the edge."""
    from worklane import relations as relmod  # noqa: PLC0415

    resolved, err = _resolve_write_tracker(
        task_id, _project_from_request(request)
    )
    if err is not None:
        return err
    assert resolved is not None
    surf, raw_id, tracker = resolved
    if tracker.get_task(raw_id) is None:
        return JSONResponse({"ok": False, "error": "task not found"}, status_code=404)
    try:
        db_path = _tracker_db_path(tracker)
    except HTTPException:
        return JSONResponse(
            {"ok": False, "error": "project store is not a local SQLite tracker"},
            status_code=400,
        )
    existing = relmod.get_relation(db_path, relation_id)
    if existing is None:
        return JSONResponse(
            {"ok": False, "error": "relation not found"},
            status_code=404,
        )
    if raw_id not in (existing.from_id, existing.to_id):
        return JSONResponse(
            {
                "ok": False,
                "error": "relation does not involve this task",
            },
            status_code=400,
        )
    try:
        ok = relmod.delete_relation(db_path, relation_id)
    except relmod.RelationError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    if not ok:
        return JSONResponse(
            {"ok": False, "error": "relation not found"},
            status_code=404,
        )
    return JSONResponse({"ok": True, "deleted": relation_id})


@router.get("/api/admin/tasks/{task_id}")
def api_get_task(task_id: str) -> JSONResponse:
    _surf, raw_id, tracker = _resolve_product_tracker(task_id)
    task, comments, archived = _get_task_hot_or_archive(tracker, raw_id)
    if task is None:
        return JSONResponse({"ok": False, "error": "task not found"}, status_code=404)
    out = task.to_dict()
    out["id"] = task_id
    out["archived"] = archived
    out["comments"] = [
        {
            "id": c.id,
            "task_id": task_id,
            "body": c.body,
            "author": c.author,
            "created_at": c.created_at,
        }
        for c in comments
    ]
    desc = task.description or ""
    out["description_html"] = render_markdown(desc) if desc else ""
    out["relations"] = _task_relations_dicts(_surf, raw_id, tracker)
    return JSONResponse({"ok": True, "task": out, "archived": archived})


# Match sqlite lifecycle auto-transition predicate (PROTOCOL.md §3):
# Completed: + Verification: on a comment → eligible for done.
_DONE_CLOSEOUT_COMPLETED_RE = re.compile(
    r"^\s*completed\s*:", re.IGNORECASE | re.MULTILINE
)
_DONE_CLOSEOUT_VERIFICATION_RE = re.compile(
    r"^\s*verification\s*:", re.IGNORECASE | re.MULTILINE
)

_DONE_WITHOUT_CLOSEOUT_HINT = (
    "cannot transition to done without a §5 close-out comment "
    "(Completed: + Verification:). Post a compliant close-out "
    "(or use wl_close) first — bare status→done is refused "
    "(PROTOCOL.md §3/§5; wl-114)"
)


def _comments_have_done_closeout(comments: List[Any]) -> bool:
    """True if any comment satisfies the §3 done auto-transition predicate."""
    for c in comments:
        body = getattr(c, "body", None)
        if body is None and isinstance(c, dict):
            body = c.get("body")
        text = body or ""
        if _DONE_CLOSEOUT_COMPLETED_RE.search(text) and _DONE_CLOSEOUT_VERIFICATION_RE.search(
            text
        ):
            return True
    return False


def _epic_coverage_block(
    task: Any,
    tracker: Any,
    *,
    product_slug: str = "",
) -> Optional[str]:
    """wl-347: refuse done for umbrella/epic with uncovered children."""
    from worklane.epic_coverage import coverage_block_reason  # noqa: PLC0415

    db_path: Optional[Path] = None
    try:
        db_path = _tracker_db_path(tracker)
    except HTTPException:
        db_path = getattr(tracker, "_db_path", None)
        db_path = Path(db_path) if db_path is not None else None
    prefix: Optional[str] = None
    if product_slug:
        spec = get_product(product_slug)
        if spec is not None:
            prefix = spec.prefix
    return coverage_block_reason(
        task, tracker, db_path=db_path, product_prefix=prefix
    )


@router.patch("/api/admin/tasks/{task_id}")
async def api_update_task(task_id: str, request: Request) -> JSONResponse:
    try:
        payload: Dict[str, Any] = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)

    resolved, err = _resolve_write_tracker(
        task_id, _project_from_request(request, payload)
    )
    if err is not None:
        return err
    assert resolved is not None
    surf, raw_id, tracker = resolved

    # Scene-feed attribution: optional signer on status writes. Not required
    # (existing clients PATCH bare {"status": ...}); when present it lands in
    # task_events.actor so /api/scene attributes the transition accurately.
    actor = str(payload.get("author") or payload.get("actor") or "").strip()

    if "status" in payload:
        new_status = str(payload.get("status") or "")
        if new_status not in TaskStatus.ALL:
            return JSONResponse(
                {"ok": False, "error": f"unknown status {new_status!r}"},
                status_code=400,
            )
        if surf == live_feed_product_slug() and _tradeos_tickets_use_http_feed():
            code, data = _request_tradeos_json(
                "PATCH",
                f"/api/ops/tickets/tradeos/{quote(raw_id, safe='')}",
                {"status": new_status},
            )
            if code == 404:
                return JSONResponse({"ok": False, "error": "task not found"}, status_code=404)
            if code < 0 or code >= 400 or not data or not data.get("ok"):
                return JSONResponse(
                    {"ok": False, "error": "tradeOS ticket API unreachable"},
                    status_code=502,
                )
            tm = data.get("task") or {}
            out = dict(tm) if isinstance(tm, dict) else {}
            out["id"] = task_id
            return JSONResponse({"ok": True, "task": out})
        # wl-114: CLI `wl status <id> done` hits this PATCH. Refuse done
        # without a Completed:+Verification: close-out already on the ticket
        # (same predicate as the comment lifecycle auto-transition).
        if new_status == TaskStatus.DONE:
            current = tracker.get_task(raw_id)
            if current is None:
                return JSONResponse(
                    {"ok": False, "error": "task not found"}, status_code=404
                )
            if current.status != TaskStatus.DONE:
                comments = tracker.list_comments(raw_id)
                if not _comments_have_done_closeout(comments):
                    return JSONResponse(
                        {"ok": False, "error": _DONE_WITHOUT_CLOSEOUT_HINT},
                        status_code=400,
                    )
                # wl-347: umbrella/epic child-coverage before status→done.
                cov_err = _epic_coverage_block(
                    current, tracker, product_slug=surf
                )
                if cov_err:
                    return JSONResponse(
                        {"ok": False, "error": cov_err}, status_code=400
                    )
        updated = tracker.update_status(raw_id, new_status, actor=actor)
        if updated is None:
            return JSONResponse({"ok": False, "error": "task not found"}, status_code=404)
        if new_status == TaskStatus.DONE:
            notify_done(task_id, updated.title or "")
        # wl-359: release / reopen → backlog on a seated hand → wake.
        if new_status == TaskStatus.BACKLOG:
            maybe_wake_hand(
                list(updated.labels or []),
                status=updated.status,
                gate_type=gate_of(updated),
                only_on_seat_change=False,
                task_id=task_id,
            )
        out = updated.to_dict()
        out["id"] = task_id
        return JSONResponse({"ok": True, "task": out})

    # Field updates: title, description, priority, gate (wl-21)
    title = payload.get("title")
    description = payload.get("description")
    priority_raw = payload.get("priority")
    gate_type = payload.get("gate_type")
    gate_until = payload.get("gate_until")
    gate_note = payload.get("gate_note")
    if gate_type is None and (gate_until is not None or gate_note is not None):
        return JSONResponse(
            {"ok": False, "error": "gate_type is required when setting gate_until or gate_note"},
            status_code=400,
        )
    if gate_type is not None and gate_type not in ("", "human", "timer", "deferred"):
        return JSONResponse(
            {"ok": False, "error": "gate_type must be '' (clear), 'human', 'timer', or 'deferred'"},
            status_code=400,
        )
    if gate_type == "timer" and not gate_until:
        return JSONResponse(
            {"ok": False, "error": "gate_until is required when gate_type is 'timer'"},
            status_code=400,
        )
    # wl-205: human-gate hard stop — max 3 per author per 2h rolling window.
    # bulk_gate_ok=true bypasses the cap but requires ticket_ids + authorizing_ticket.
    if gate_type == "human":
        gate_author = actor or "cli-update"
        bulk_gate_ok = bool(payload.get("bulk_gate_ok"))
        if not bulk_gate_ok:
            since_iso = (
                datetime.now(timezone.utc) - timedelta(hours=2)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
            if hasattr(tracker, "count_human_gate_sets_since"):
                recent = tracker.count_human_gate_sets_since(gate_author, since_iso)
                if recent >= 3:
                    return JSONResponse(
                        {
                            "ok": False,
                            "error": (
                                f"Human-gate hard stop: {gate_author!r} has already set "
                                f"{recent} human gates in the last 2 hours (limit 3). "
                                "Pass bulk_gate_ok=true with ticket_ids and "
                                "authorizing_ticket to override."
                            ),
                            "human_gate_count": recent,
                            "window_hours": 2,
                            "limit": 3,
                        },
                        status_code=429,
                    )
        else:
            ticket_ids = payload.get("ticket_ids")
            authorizing_ticket = payload.get("authorizing_ticket")
            if not ticket_ids or not authorizing_ticket:
                return JSONResponse(
                    {
                        "ok": False,
                        "error": (
                            "bulk_gate_ok requires ticket_ids (list) "
                            "and authorizing_ticket"
                        ),
                    },
                    status_code=400,
                )
    if title is not None or description is not None or priority_raw is not None or gate_type is not None:
        priority: Optional[int] = None
        if priority_raw is not None:
            try:
                priority = int(priority_raw)
            except (TypeError, ValueError):
                return JSONResponse(
                    {"ok": False, "error": "priority must be an integer"},
                    status_code=400,
                )
        updated = tracker.update_task(
            raw_id,
            title=str(title) if title is not None else None,
            description=str(description) if description is not None else None,
            priority=priority,
            gate_type=gate_type,
            gate_until=str(gate_until) if gate_until is not None else None,
            gate_note=str(gate_note) if gate_note is not None else None,
            actor=actor,
        )
        if updated is None:
            return JSONResponse({"ok": False, "error": "task not found"}, status_code=404)
        # wl-359: gate-clear on a seated ready ticket → wake the hand.
        if gate_type is not None and str(gate_type).strip() == "":
            maybe_wake_hand(
                list(updated.labels or []),
                status=updated.status,
                gate_type=gate_of(updated),
                only_on_seat_change=False,
                task_id=task_id,
            )
        out = updated.to_dict()
        out["id"] = task_id
        return JSONResponse({"ok": True, "task": out})

    return JSONResponse(
        {
            "ok": False,
            "error": "no supported fields in payload (status, title, description, "
                     "priority, gate_type, gate_until, gate_note)",
        },
        status_code=400,
    )


@router.patch("/api/admin/tasks/{task_id}/labels")
async def api_update_labels(task_id: str, request: Request) -> JSONResponse:
    try:
        payload: Dict[str, Any] = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)

    add_raw = payload.get("add") or []
    remove_raw = payload.get("remove") or []
    if not add_raw and not remove_raw:
        return JSONResponse(
            {"ok": False, "error": "at least one of 'add' or 'remove' is required"},
            status_code=400,
        )

    def _parse_labels(raw: object) -> List[str]:
        if isinstance(raw, str):
            return [s.strip() for s in raw.split(",") if s.strip()]
        if isinstance(raw, list):
            return [str(s).strip() for s in raw if str(s).strip()]
        return []

    add_labels = _parse_labels(add_raw)
    remove_labels = _parse_labels(remove_raw)

    resolved, err = _resolve_write_tracker(
        task_id, _project_from_request(request, payload)
    )
    if err is not None:
        return err
    assert resolved is not None
    surf, raw_id, tracker = resolved

    # wl-320: starve guard — label mutation must not bypass wl-315 by
    # creating a ticket with a valid seat and then swapping to bare worker:you.
    # wl-372: foreign-seat guard — reject worker:<hand> not hired for this store.
    from worklane.routing_labels import (  # noqa: PLC0415
        check_mutation_foreign_seat,
        check_mutation_starve_guard,
        check_worker_product_mismatch,
        worker_ids_from_labels,
    )
    _existing_task = tracker.get_task(raw_id)
    if _existing_task is None:
        return JSONResponse({"ok": False, "error": "task not found"}, status_code=404)
    _hired = _workforce_workers_for_product(surf)
    _starve_err = check_mutation_starve_guard(
        list(_existing_task.labels or []),
        add=add_labels,
        remove=remove_labels,
        hired_hands=_hired,
    )
    if _starve_err:
        return JSONResponse({"ok": False, "error": _starve_err}, status_code=400)
    _foreign_err = check_mutation_foreign_seat(
        list(_existing_task.labels or []),
        add=add_labels,
        remove=remove_labels,
        hired_hands=_hired,
    )
    if _foreign_err:
        return JSONResponse({"ok": False, "error": _foreign_err}, status_code=400)

    # wl-296: cross-product mismatch guard on newly added worker:* labels.
    _added_wids = worker_ids_from_labels(add_labels)
    _mismatch_warn: Optional[str] = None
    if _added_wids:
        _all_wp = _workforce_products_for_workers()
        _mismatch_warn = check_worker_product_mismatch(_added_wids, surf, _all_wp)
        if _mismatch_warn:
            _hard = os.environ.get("WL_WORKER_PRODUCT_HARD_REJECT", "").lower() in ("1", "true", "yes")
            if _hard:
                return JSONResponse({"ok": False, "error": _mismatch_warn}, status_code=400)

    _prev_hand = previous_hand_from_labels(list(_existing_task.labels or []))
    updated = tracker.update_labels(raw_id, add=add_labels, remove=remove_labels)
    if updated is None:
        return JSONResponse({"ok": False, "error": "task not found"}, status_code=404)
    # wl-359: seat gain / re-route on a claimable ticket → wake the new hand.
    maybe_wake_hand(
        list(updated.labels or []),
        status=updated.status,
        gate_type=gate_of(updated),
        only_on_seat_change=True,
        previous_hand=_prev_hand,
        task_id=task_id,
    )
    out = updated.to_dict()
    out["id"] = task_id
    return JSONResponse({"ok": True, "task": out, "routing_warning": _mismatch_warn})


_CLOSEOUT_HINT = (
    "close-out comments must carry literal 'Verification:' and 'Links:' "
    "sections (PROTOCOL.md §5 — Completed/Verification/Links/Follow-ups)"
)


def _comment_process_violation(body: str, author: str) -> Optional[str]:
    """PROTOCOL.md guard: §3.8 signed comments + §5 close-out contract.

    Returns an error string when the comment must be rejected, else None.
    """
    if not author.strip():
        return (
            "author is required — sign every comment with your canonical "
            "agent id (PROTOCOL.md §3.8/§5.2), e.g. --author work-pool"
        )
    first_line = next(
        (ln.strip() for ln in body.split("\n") if ln.strip()), ""
    )
    if first_line.startswith("Completed"):
        if "Verification:" not in body or "Links:" not in body:
            return _CLOSEOUT_HINT
        # wl-396: Links must cite a landing commit SHA (cheap presence).
        from worklane.closeout_links import (  # noqa: PLC0415
            closeout_links_violation,
        )

        sha_err = closeout_links_violation(body)
        if sha_err:
            return sha_err
    if first_line.startswith("Blocked") and "Next step:" not in body:
        return (
            "Blocked comments must include a 'Next step:' line "
            "(PROTOCOL.md §5)"
        )
    return None


def _misattributed_owner(author: str, body: str) -> Optional[str]:
    """wl-50 guard: default-identity write claiming a different Owner:.

    Returns the mismatched agent id when a comment signed with the default
    identity (``DEFAULT_AGENT_ID``) carries an ``Owner: <agent>`` marker for
    a *different* agent — the signature the wl-39 misconfiguration produces
    (launcher never exported ``WL_AGENT_ID`` / ``WL_AGENT_ID``, so an autonomous agent's
    writes fall back to the default and silently mis-sign). Returns None for
    normal interactive use of the default identity.
    """
    if author != DEFAULT_AGENT_ID:
        return None
    m = _OWNER_LINE_RE.search(body)
    if not m:
        return None
    marked = m.group(1).strip()
    if marked and marked != DEFAULT_AGENT_ID:
        return marked
    return None


@router.post("/api/admin/tasks/{task_id}/comments")
async def api_add_comment(task_id: str, request: Request) -> JSONResponse:
    try:
        payload: Dict[str, Any] = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)

    body = (payload.get("body") or "").strip()
    if not body:
        return JSONResponse({"ok": False, "error": "body is required"}, status_code=400)
    author = str(payload.get("author") or "")

    violation = _comment_process_violation(body, author)
    if violation:
        return JSONResponse({"ok": False, "error": violation}, status_code=400)

    marked = _misattributed_owner(author, body)
    if marked:
        logger.warning(
            "default-identity write looks autonomous: author=%r task=%s "
            "claims Owner: %r — launcher likely never exported WL_AGENT_ID/WL_AGENT_ID "
            "(wl-39/wl-50)",
            author, task_id, marked,
        )

    resolved, err = _resolve_write_tracker(
        task_id, _project_from_request(request, payload)
    )
    if err is not None:
        return err
    assert resolved is not None
    surf, raw_id, tracker = resolved
    if surf == live_feed_product_slug() and _tradeos_tickets_use_http_feed():
        code, data = _request_tradeos_json(
            "POST",
            f"/api/ops/tickets/tradeos/{quote(raw_id, safe='')}/comments",
            {"body": body, "author": author},
        )
        if code == 404:
            return JSONResponse({"ok": False, "error": "task not found"}, status_code=404)
        if code < 0 or code >= 400 or not data or not data.get("ok"):
            return JSONResponse(
                {"ok": False, "error": "tradeOS ticket API unreachable"},
                status_code=502,
            )
        cm = data.get("comment") or {}
        return JSONResponse(
            {
                "ok": True,
                "comment": {
                    "id": cm.get("id"),
                    "task_id": task_id,
                    "body": cm.get("body"),
                    "author": cm.get("author"),
                    "created_at": cm.get("created_at"),
                },
            }
        )
    # wl-347: refuse Completed: close-outs on umbrella/epic with uncovered children
    # (before add_comment so lifecycle cannot race to done).
    from worklane.epic_coverage import body_is_done_closeout  # noqa: PLC0415

    if body_is_done_closeout(body):
        current = tracker.get_task(raw_id)
        if current is None:
            return JSONResponse({"ok": False, "error": "task not found"}, status_code=404)
        cov_err = _epic_coverage_block(current, tracker, product_slug=surf)
        if cov_err:
            return JSONResponse({"ok": False, "error": cov_err}, status_code=400)
        # wl-339: when product registers checks, Verification must cite them
        # (docs/notes/research exempt via registry). No registration → no-op.
        from worklane.closeout_checks import (  # noqa: PLC0415
            closeout_checks_violation,
        )

        chk_err = closeout_checks_violation(
            body, product=surf, labels=getattr(current, "labels", None)
        )
        if chk_err:
            return JSONResponse({"ok": False, "error": chk_err}, status_code=400)

    try:
        comment = tracker.add_comment(raw_id, body, author=author)
    except KeyError:
        return JSONResponse({"ok": False, "error": "task not found"}, status_code=404)
    return JSONResponse(
        {
            "ok": True,
            "comment": {
                "id": comment.id,
                "task_id": task_id,
                "body": comment.body,
                "author": comment.author,
                "created_at": comment.created_at,
            },
        }
    )


@router.get("/api/admin/tasks")
def api_list_tasks(
    status: str = "",
    label: str = "",
    priority: str = "",
    product: str = "",
    project: str = "",
    limit: int = 200,
    with_preview: int = 0,
    gate: str = "",
) -> JSONResponse:
    from worklane.board import _parse_gate_filter  # noqa: PLC0415

    products = product_trackers()
    prio_int = parse_wq_priority(priority)
    gate_type = _parse_gate_filter(gate)
    # wl-64: ``project`` is the canonical scope param; ``product`` stays a
    # silent back-compat alias for the same field (mirrors the CLI/MCP
    # surfaces). Existing ``product=`` callers keep working unchanged.
    # wl-219: explicit unknown slug fails closed (not multi/all).
    raw_scope = project or product
    prod, scope_ok = resolve_wq_product(raw_scope)
    if not scope_ok:
        return JSONResponse(
            {
                "ok": False,
                "error": "unknown project %r" % (str(raw_scope).strip(),),
                "tasks": [],
            },
            status_code=404,
        )
    tasks, tradeos_prev = _list_tasks_for_wq_multi_resolved(
        products,
        status=status or None,
        label=label or None,
        priority=prio_int,
        product=prod,
        limit=limit,
        with_preview=bool(with_preview),
        gate_type=gate_type,
    )

    task_dicts = [t.to_dict() for t in tasks]

    previews: Dict[str, Dict[str, str]] = {}
    if with_preview:
        previews = _load_preview_comments_multi(
            products,
            tasks,
            tradeos_preview=tradeos_prev if tradeos_prev else None,
        )
        for td in task_dicts:
            entry = previews.get(td["id"])
            if entry:
                td["last_comment_preview"] = entry["body"]
                td["last_comment_author"] = entry["author"]
                td["last_comment_at"] = entry["created_at"]
                td["owner"] = entry.get("owner") or ""
            else:
                td["last_comment_preview"] = ""
                td["last_comment_author"] = ""
                td["last_comment_at"] = ""
                td["owner"] = ""

    # wl-9: single card renderer — poll ships the same HTML the SSR board
    # embeds via _render_task_card, so JS never re-implements card markup.
    scope_product = prod or ""
    for t, td in zip(tasks, task_dicts):
        td["card_html"] = _render_task_card(
            t, previews.get(str(t.id), {}), scope_product
        )

    # wl-354: SQL GROUP BY counts — never SELECT * every store for chips.
    # Full-row materialization was O(all tickets × description bytes) on
    # every list and wedged the single-threaded desk under concurrent
    # WorkForce preflights (project=all&status=backlog&limit=1).
    scope_counts = status_counts_for_scope_multi(products, prod)
    scope_total = sum(scope_counts.get(s, 0) for s in TaskStatus.ALL)
    # wl-47: board column headers show the filtered-scope truth, not the
    # capped fetch; chips keep the unfiltered scope_counts.
    column_counts = column_counts_for_scope_multi(
        products,
        prod,
        status=status or None,
        label=label or None,
        priority=prio_int,
        gate_type=gate_type,
    )

    return JSONResponse(
        {
            "ok": True,
            "tracker": "multi",
            "tasks": task_dicts,
            "scope_counts": scope_counts,
            "scope_total": scope_total,
            "column_counts": column_counts,
        }
    )


# ── Ops health ──────────────────────────────────────────────────────────────

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
