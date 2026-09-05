"""Task CRUD, relations, comments, and list/ready routes."""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from urllib.parse import quote

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from worklane.api.tasks._router import router
from worklane.api.tasks.helpers import (
    DEFAULT_AGENT_ID,
    _project_from_request,
    _resolve_write_tracker,
    _tracker_db_path,
    _workforce_products_for_workers,
    _workforce_workers_for_product,
)
from worklane.board import (
    TASK_ID_PREFIX_OPS,
    TASK_ID_PREFIX_TRADEOS,
    _OWNER_LINE_RE,
    _load_preview_comments_multi,
    _render_task_card,
    column_counts_for_scope_multi,
    get_ops_ticket_tracker,
    parse_wq_priority,
    resolve_wq_product,
    status_counts_for_scope_multi,
)
from worklane.notify import notify_done
from worklane.products import (
    default_product_slug,
    get_product,
    live_feed_product_slug,
    product_tracker,
    product_trackers,
    split_task_id,
)
from worklane.rendering import render_markdown
from worklane.server_helpers import (
    _get_task_hot_or_archive,
    _list_tasks_for_wq_multi_resolved,
    _request_tradeos_json,
    _resolve_product_tracker,
    _task_relations_dicts,
    _tradeos_tickets_use_http_feed,
)
from worklane.trackers import TaskStatus, get_default_tracker, task_is_gated
from worklane.wake_nudge import gate_of, maybe_wake_hand, previous_hand_from_labels

# Preserve historical logger name so assertLogs("worklane.api.tasks") still matches.
logger = logging.getLogger("worklane.api.tasks")

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
    if gate_type is not None and gate_type not in ("", "human", "timer", "deferred", "tracking"):
        return JSONResponse(
            {
                "ok": False,
                "error": "gate_type must be '' (clear), 'human', 'timer', 'deferred', or 'tracking'",
            },
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
    request: Request,
    status: str = "",
    label: str = "",
    priority: str = "",
    product: str = "",
    project: str = "",
    limit: int = 200,
    with_preview: int = 0,
    gate: str = "",
    q: str = "",
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
    # wl-493: bound id/title search. Status omitted → every status (including
    # done/canceled). Default limit 20, hard cap 50. Existing list without
    # q= keeps limit default 200. Distinguishing omitted limit from the
    # FastAPI default requires the raw query map.
    q_norm = (q or "").strip()
    include_description = True
    if q_norm:
        include_description = False
        if "limit" not in request.query_params:
            limit = 20
        elif limit < 1:
            limit = 20
        if limit > 50:
            limit = 50
    tasks, tradeos_prev = _list_tasks_for_wq_multi_resolved(
        products,
        status=status or None,
        label=label or None,
        priority=prio_int,
        product=prod,
        limit=limit,
        with_preview=bool(with_preview),
        gate_type=gate_type,
        q=q_norm or None,
        include_description=include_description,
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

