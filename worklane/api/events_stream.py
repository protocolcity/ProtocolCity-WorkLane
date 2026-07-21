"""SSE event-stream routes extracted from task_server (wl-222)."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from worklane.products import product_tracker
from worklane.trackers import get_default_tracker

router = APIRouter()

_SSE_HIGH_VALUE_TYPES: frozenset = frozenset({"status_change", "labels_changed"})


@router.get("/api/events")
def api_events(since: int = 0, project: str = "", limit: int = 200) -> JSONResponse:
    """Poll-cursor change feed (wl-101): ordered ticket events after ``since``.

    Event ids are the store's own autoincrement, so the cursor is durable
    across server restarts with no separate cursor-store — a consumer
    just remembers the highest ``id`` it has seen and passes it back as
    ``since`` next poll. ``project`` scopes to one product tracker (same
    convention as ``/api/dev/activity``, wl-105); omitted/unknown falls
    back to the server default tracker rather than erroring.
    """
    tracker = product_tracker(project) if project else get_default_tracker()
    if not hasattr(tracker, "list_events"):
        return JSONResponse({"events": [], "cursor": since})
    limit = max(1, min(int(limit), 500))
    events = tracker.list_events(since=max(0, int(since)), limit=limit)
    cursor = events[-1]["id"] if events else since
    return JSONResponse({"events": events, "cursor": cursor})


@router.get("/api/events/stream")
async def api_events_stream(
    request: Request,
    project: str = "",
    since: int = 0,
    interval: float = 1.5,
) -> StreamingResponse:
    """SSE stream of high-value ticket transition events (wl-218 · LIVE-C2).

    Streams ``status_change`` and ``labels_changed`` events as they occur.
    Payload is minimal — ``id``, ``task_id``, ``event_type``, ``status``,
    ``labels``, ``created_at`` — never full task bodies. Does not replace
    the generation-token pulse bus (Layer B); supplements it for clients
    that want immediate notification of individual transitions.

    ``project`` scopes to one store (same convention as ``/api/events``).
    ``since`` is the last event id the client already saw (0 for tail-from-now).
    ``interval`` controls the internal poll cadence in seconds (default 1.5).

    The endpoint disconnects cleanly when the client closes. Recommended
    client-side pattern::

        const es = new EventSource('/api/events/stream?project=worklane');
        es.onmessage = ev => {
            const t = JSON.parse(ev.data);
            // trigger targeted refetch of t.task_id card
        };
        document.addEventListener('visibilitychange', () => {
            if (document.hidden) es.close();
        });
    """
    tracker = product_tracker(project) if project else get_default_tracker()
    poll_secs = max(0.5, min(float(interval), 10.0))

    async def _event_generator():
        yield "retry: 2000\n\n"
        cursor = max(0, int(since))
        while True:
            if await request.is_disconnected():
                break
            if tracker is not None and hasattr(tracker, "list_events"):
                try:
                    events = await asyncio.to_thread(
                        tracker.list_events, since=cursor, limit=50
                    )
                except Exception:
                    events = []
                for ev in events:
                    if ev.get("event_type") not in _SSE_HIGH_VALUE_TYPES:
                        cursor = max(cursor, ev["id"])
                        continue
                    payload = json.dumps({
                        "id": ev["id"],
                        "task_id": ev["task_id"],
                        "event_type": ev["event_type"],
                        "status": ev.get("status"),
                        "labels": ev.get("labels"),
                        "created_at": ev.get("created_at"),
                    })
                    yield "id: %d\ndata: %s\n\n" % (ev["id"], payload)
                    cursor = max(cursor, ev["id"])
            await asyncio.sleep(poll_secs)

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
