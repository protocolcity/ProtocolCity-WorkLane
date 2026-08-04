"""SSE event-stream routes extracted from task_server (wl-222).

wl-348: every emitted event carries store slug + composite task id;
unscoped ``/api/events/stream`` aggregates all registered stores rather
than silently tailing the default (tradeos) store alone.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from worklane.products import (
    ProductSpec,
    get_product,
    product_tracker,
    product_trackers,
)

router = APIRouter()

_SSE_HIGH_VALUE_TYPES: frozenset = frozenset({"status_change", "labels_changed"})


def _composite_task_id(prefix: str, raw_task_id: Any) -> str:
    """Prefix a bare task id; leave already-prefixed composites alone."""
    s = str(raw_task_id or "").strip()
    if not s:
        return s
    if s.startswith("%s-" % prefix):
        return s
    return "%s-%s" % (prefix, s)


def _stamp_event(ev: dict, spec: ProductSpec) -> dict:
    """Shape one store-native event for wire consumers (wl-348).

    Contract:
    - ``store``: product slug (e.g. ``tradeos``, ``worklane``)
    - ``task_id``: composite id (e.g. ``t-21``, ``wl-348``)
    - ``id``: store-local event id (integer; scoped ``since`` cursor)
    - ``event_type``, ``status``, ``labels``, ``created_at`` unchanged
    """
    return {
        "id": ev["id"],
        "task_id": _composite_task_id(spec.prefix, ev.get("task_id")),
        "event_type": ev.get("event_type"),
        "status": ev.get("status"),
        "labels": ev.get("labels"),
        "created_at": ev.get("created_at"),
        "store": spec.slug,
    }


def _scoped_spec(project: str) -> Optional[ProductSpec]:
    """Resolve a single store when ``project`` is set; else None (aggregate)."""
    prod = (project or "").strip().lower()
    if not prod or prod == "all":
        return None
    return get_product(prod)


def _stream_sources(
    project: str,
) -> List[Tuple[ProductSpec, Any]]:
    """(spec, tracker) pairs for the stream.

    Scoped (``project=`` set and known): that store only.
    Unscoped / ``all``: every registered product — never a silent single
    default (wl-348). Unknown slug falls through to empty list so the
    stream stays open but emits nothing rather than inventing tradeos.
    """
    spec = _scoped_spec(project)
    if spec is not None:
        return [(spec, product_tracker(spec))]
    prod = (project or "").strip().lower()
    if prod and prod != "all":
        # Explicit but unknown project: do not silently default.
        return []
    return list(product_trackers())


@router.get("/api/events")
def api_events(since: int = 0, project: str = "", limit: int = 200) -> JSONResponse:
    """Poll-cursor change feed (wl-101): ordered ticket events after ``since``.

    Event ids are the store's own autoincrement, so the cursor is durable
    across server restarts with no separate cursor-store — a consumer
    just remembers the highest ``id`` it has seen and passes it back as
    ``since`` next poll. ``project`` scopes to one product tracker.

    wl-348: each event is stamped with ``store`` + composite ``task_id``.
    Unscoped / ``project=all`` merges every registered store (sorted by
    store-local id, then slug); the returned ``cursor`` is the max
    store-local id among the page (same integer ``since`` applied per
    store). Unknown ``project`` returns an empty page (no silent default).
    """
    sources = _stream_sources(project)
    limit = max(1, min(int(limit), 500))
    since_i = max(0, int(since))
    stamped: List[dict] = []
    for spec, tracker in sources:
        if tracker is None or not hasattr(tracker, "list_events"):
            continue
        try:
            events = tracker.list_events(since=since_i, limit=limit)
        except Exception:
            events = []
        for ev in events:
            stamped.append(_stamp_event(ev, spec))
    # Stable order: event id, then store slug (ids are per-store).
    stamped.sort(key=lambda e: (int(e["id"]), e.get("store") or ""))
    page = stamped[:limit]
    cursor = page[-1]["id"] if page else since_i
    return JSONResponse({"events": page, "cursor": cursor})


@router.get("/api/events/stream")
async def api_events_stream(
    request: Request,
    project: str = "",
    since: int = 0,
    interval: float = 1.5,
) -> StreamingResponse:
    """SSE stream of high-value ticket transition events (wl-218 · LIVE-C2).

    Streams ``status_change`` and ``labels_changed`` events as they occur.
    Payload is minimal — ``id``, ``task_id`` (composite), ``store``,
    ``event_type``, ``status``, ``labels``, ``created_at`` — never full
    task bodies. Does not replace the generation-token pulse bus (Layer B);
    supplements it for clients that want immediate notification of
    individual transitions.

    ``project`` scopes to one store; omit or ``all`` aggregates every
    registered store with each event stamped ``store`` (wl-348). Never
    silently tails only the default store. ``since`` is the last
    store-local event id the client already saw (0 for tail-from-start);
    the same integer is applied independently per store when aggregating.
    ``interval`` controls the internal poll cadence in seconds (default 1.5).

    The endpoint disconnects cleanly when the client closes. Recommended
    client-side pattern::

        const es = new EventSource('/api/events/stream?project=worklane');
        es.onmessage = ev => {
            const t = JSON.parse(ev.data);
            // t.store + t.task_id (composite) identify the card
        };
        document.addEventListener('visibilitychange', () => {
            if (document.hidden) es.close();
        });
    """
    sources = _stream_sources(project)
    poll_secs = max(0.5, min(float(interval), 10.0))
    start_cursor = max(0, int(since))

    async def _event_generator():
        yield "retry: 2000\n\n"
        # Independent per-store cursor (store-local autoincrement ids).
        cursors: Dict[str, int] = {
            spec.slug: start_cursor for spec, _tr in sources
        }
        while True:
            if await request.is_disconnected():
                break
            for spec, tracker in sources:
                if tracker is None or not hasattr(tracker, "list_events"):
                    continue
                cursor = cursors.get(spec.slug, start_cursor)
                try:
                    events = await asyncio.to_thread(
                        tracker.list_events, since=cursor, limit=50
                    )
                except Exception:
                    events = []
                for ev in events:
                    cursors[spec.slug] = max(
                        cursors.get(spec.slug, start_cursor), int(ev["id"])
                    )
                    if ev.get("event_type") not in _SSE_HIGH_VALUE_TYPES:
                        continue
                    payload = json.dumps(_stamp_event(ev, spec))
                    # Wire id is store-scoped so multi-store streams stay unique.
                    wire_id = "%s:%d" % (spec.slug, int(ev["id"]))
                    yield "id: %s\ndata: %s\n\n" % (wire_id, payload)
            await asyncio.sleep(poll_secs)

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
