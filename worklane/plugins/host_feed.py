"""Optional tradeOS HTTP feed plugin (wl-222 / wl-223 Phase B).

Extracted from task_server so the host-bleed surface is isolated.
task_server imports this module inside a try/except ImportError block
so the public WorkLane export can ship without it — all callers degrade
to empty/False stubs when the module is absent.

``tradeos_configured()`` is the runtime guard: True only when TRADEOS_HOST
is explicitly present in the environment (not just the default 127.0.0.1).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

from worklane.board import (
    TASK_ID_PREFIX_TRADEOS,
    list_tasks_for_wq_multi,
)
from worklane.products import (
    ProductSpec,
    live_feed_product_slug,
)
from worklane.trackers import Task, TaskStatus


def _tradeos_api_base() -> str:
    host = os.environ.get("TRADEOS_HOST", "127.0.0.1")
    port = os.environ.get("TRADEOS_PORT", "8788")
    return f"http://{host}:{port}"


def _fetch_tradeos_json(path: str, timeout: float = 1.75) -> Optional[Dict[str, Any]]:
    url = _tradeos_api_base() + path
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            data = json.loads(raw)
            return data if isinstance(data, dict) else None
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError):
        return None


def _tradeos_tickets_use_http_feed() -> bool:
    """When True, product tickets are read from main app HTTP API.

    Default is ``sqlite`` so WorkLane stays independent from
    tradeOS runtime availability.
    """
    return os.environ.get("TRADEOS_TICKETS_SOURCE", "sqlite").strip().lower() != "sqlite"


def _request_tradeos_json(
    method: str,
    path: str,
    body: Optional[Dict[str, Any]] = None,
    timeout: float = 12.0,
) -> Tuple[int, Optional[Dict[str, Any]]]:
    """HTTP JSON helper for mutating tradeOS ticket routes on port 8788."""
    url = _tradeos_api_base() + path
    data_bytes: Optional[bytes] = None
    headers: Dict[str, str] = {"Accept": "application/json"}
    if body is not None:
        data_bytes = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    try:
        req = urllib.request.Request(
            url,
            data=data_bytes,
            headers=headers,
            method=method.upper(),
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            code = resp.getcode()
            if not raw.strip():
                return code, {}
            try:
                parsed = json.loads(raw)
            except ValueError:
                return code, None
            return code, parsed if isinstance(parsed, dict) else None
    except urllib.error.HTTPError as e:
        try:
            raw_err = e.read().decode("utf-8")
            parsed = json.loads(raw_err)
            return e.code, parsed if isinstance(parsed, dict) else None
        except Exception:
            return e.code, None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return -1, None


def _task_from_tradeos_api_row(row: Dict[str, Any]) -> Task:
    raw_id = str(row.get("id") or "")
    return Task(
        id=f"{TASK_ID_PREFIX_TRADEOS}-{raw_id}",
        title=str(row.get("title") or ""),
        description=str(row.get("description") or ""),
        status=str(row.get("status") or TaskStatus.BACKLOG),
        priority=int(row.get("priority") or 3),
        labels=list(row.get("labels") or []),
        ext_id=row.get("ext_id"),
        created_at=str(row.get("created_at") or ""),
        updated_at=str(row.get("updated_at") or ""),
    )


def _tradeos_preview_map_from_api_tasks(
    rows: List[Dict[str, Any]],
) -> Dict[str, Dict[str, str]]:
    out: Dict[str, Dict[str, str]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw_id = str(row.get("id") or "")
        if not raw_id:
            continue
        cid = f"{TASK_ID_PREFIX_TRADEOS}-{raw_id}"
        if row.get("last_comment_at") or row.get("last_comment_preview"):
            out[cid] = {
                "body": str(row.get("last_comment_preview") or ""),
                "author": str(row.get("last_comment_author") or ""),
                "created_at": str(row.get("last_comment_at") or ""),
            }
    return out


def _fetch_tradeos_tasks_via_http(
    *,
    status: Optional[str],
    label: Optional[str],
    priority: Optional[int],
    limit: int,
    with_preview: bool,
) -> Tuple[List[Task], Dict[str, Dict[str, str]]]:
    parts: List[Tuple[str, str]] = []
    if status:
        parts.append(("status", status))
    if label:
        parts.append(("label", label))
    if priority is not None:
        parts.append(("priority", str(priority)))
    parts.append(("limit", str(min(int(limit), 5000))))
    if with_preview:
        parts.append(("with_preview", "1"))
    q = urlencode(parts)
    path = f"/api/ops/tickets/tradeos?{q}" if q else "/api/ops/tickets/tradeos"
    data = _fetch_tradeos_json(path, timeout=15.0)
    if not data or not data.get("ok"):
        return [], {}
    raw_tasks = data.get("tasks")
    if not isinstance(raw_tasks, list):
        return [], {}
    tasks = [
        _task_from_tradeos_api_row(r)
        for r in raw_tasks
        if isinstance(r, dict)
    ]
    previews: Dict[str, Dict[str, str]] = {}
    if with_preview:
        previews = _tradeos_preview_map_from_api_tasks(
            [r for r in raw_tasks if isinstance(r, dict)]
        )
    return tasks, previews


def _list_tasks_for_wq_multi_resolved(
    products: List[Tuple[ProductSpec, Any]],
    *,
    status: Optional[str],
    label: Optional[str],
    priority: Optional[int],
    product: str,
    limit: int,
    with_preview: bool,
    gate_type: Optional[str] = None,
    q: Optional[str] = None,
    include_description: bool = True,
) -> Tuple[List[Task], Dict[str, Dict[str, str]]]:
    """Merge tasks across all product stores; the live-feed product's half
    may come from the main app HTTP API when ``TRADEOS_TICKETS_SOURCE`` says
    so (see products.live_feed_product_slug).

    DECISION (recommendation-default): ``q`` search stays on the sqlite
    stores. The host HTTP feed has no q= and would dump unfiltered tradeOS
    rows into search hits (wl-493).
    """
    empty_prev: Dict[str, Dict[str, str]] = {}
    p = (product or "").strip().lower()
    q_norm = (q or "").strip() or None
    if q_norm or not _tradeos_tickets_use_http_feed():
        tasks = list_tasks_for_wq_multi(
            products,
            status=status,
            label=label,
            priority=priority,
            product=p,
            gate_type=gate_type,
            limit=limit,
            q=q_norm,
            include_description=include_description,
        )
        return tasks, empty_prev

    feed_slug = live_feed_product_slug()
    merged: List[Task] = []
    prev: Dict[str, Dict[str, str]] = {}
    if p in ("", feed_slug):
        ta, prev = _fetch_tradeos_tasks_via_http(
            status=status,
            label=label,
            priority=priority,
            limit=limit if p == feed_slug else 500,
            with_preview=with_preview,
        )
        merged.extend(ta)
    if p != feed_slug:
        non_feed = [(s, t) for s, t in products if s.slug != feed_slug]
        merged.extend(
            list_tasks_for_wq_multi(
                non_feed,
                status=status,
                label=label,
                priority=priority,
                product=p,
                gate_type=gate_type,
                limit=500,
                include_description=include_description,
            )
        )
    merged.sort(key=lambda x: x.updated_at or "", reverse=True)
    out = merged[:limit]
    if not with_preview:
        prev = {}
    return out, prev


def tradeos_configured() -> bool:
    """True when TRADEOS_HOST is explicitly set in the environment.

    The default value (127.0.0.1) is never written — only explicit caller
    configuration counts.  Use this as a cheap guard before attempting any
    outbound HTTP to the host process.
    """
    return "TRADEOS_HOST" in os.environ


def _fetch_tradeos_ops_snapshot() -> Dict[str, Optional[Dict[str, Any]]]:
    """Fetch status, positions, trades, and signals from tradeOS in parallel.

    Returns a dict keyed by data type; values are None when the host is
    unreachable or the endpoint returns an unexpected shape.  Moved from
    task_server to host_feed in wl-223 Phase B so callers that stub out this
    module get an empty dict without touching the network.
    """
    specs: List[Tuple[str, str]] = [
        ("status", "/api/ops/status"),
        ("positions", "/api/ops/positions"),
        ("trades", "/api/ops/trades/recent?limit=10"),
        ("signals", "/api/ops/signals/recent?limit=8"),
    ]
    out: Dict[str, Optional[Dict[str, Any]]] = {k: None for k, _ in specs}

    def _one(item: Tuple[str, str]) -> Tuple[str, Optional[Dict[str, Any]]]:
        key, path = item
        return key, _fetch_tradeos_json(path)

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(_one, s) for s in specs]
        for fu in as_completed(futures):
            key, data = fu.result()
            out[key] = data
    return out
