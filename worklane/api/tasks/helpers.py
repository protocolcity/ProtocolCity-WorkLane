"""Write-path addressing and WorkForce roster helpers for the tasks API."""
from __future__ import annotations

import json
import os
import re
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from worklane.server_helpers import _resolve_product_tracker

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
