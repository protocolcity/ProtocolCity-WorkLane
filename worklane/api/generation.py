"""Generation token routes extracted from task_server (wl-222)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from worklane.products import product_tracker, product_trackers

router = APIRouter()


@router.get("/api/generation")
@router.get("/api/pulse")
def api_generation(project: str = "") -> JSONResponse:
    """Generation token(s) for suite live-shell Layer B (wl-217).

    Tokens only — no task bodies. ``project`` scopes to one store; empty
    returns a city-wide composite of every registered product tracker so
    suite Desk can cheaply detect any ticket movement.
    """
    now = datetime.now(timezone.utc).isoformat()
    prod = (project or "").strip().lower()
    if prod and prod not in ("", "all"):
        try:
            tracker = product_tracker(prod)
        except Exception:
            tracker = None
        if tracker is None or not hasattr(tracker, "generation_token"):
            return JSONResponse(
                {"ok": False, "error": "unknown project or no token support",
                 "token": "0", "ts": now},
                status_code=404,
            )
        gen = tracker.generation_token()
        resp = JSONResponse({
            "ok": True,
            "token": gen.get("token") or "0",
            "ts": now,
            "project": prod,
            "detail": gen,
        })
        resp.headers["Cache-Control"] = "no-store, max-age=0"
        return resp

    parts: List[str] = []
    by_project: Dict[str, Any] = {}
    for spec, tracker in product_trackers():
        if not hasattr(tracker, "generation_token"):
            continue
        try:
            gen = tracker.generation_token()
        except Exception:
            gen = {"token": "err"}
        slug = getattr(spec, "slug", "?")
        tok = str(gen.get("token") or "0")
        by_project[slug] = tok
        parts.append("%s=%s" % (slug, tok))
    composite = "|".join(parts) if parts else "0"
    resp = JSONResponse({
        "ok": True,
        "token": composite,
        "ts": now,
        "by_project": by_project,
    })
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp
