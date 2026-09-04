"""Report JSON routes extracted from task_server (wl-487).

HTML placards for /admin/overview stay in task_server — this module is the
JSON engine only (GET /api/report + GET /api/admin/overview/summary).
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from worklane.products import discover_products, get_product
from worklane.server_helpers import (
    _collect_founder_attention_items,
    _merged_ready_count,
    _merged_scope_tasks_for_filters,
    _parse_task_date_utc,
    _partition_attention_items,
)
from worklane.trackers import TaskStatus

router = APIRouter()

_REPORT_WINDOW_DAYS = int(
    os.environ.get("WL_REPORT_WINDOW_DAYS")
    or os.environ.get("WL_REPORT_WINDOW_DAYS", "7")
)
_REPORT_AGING_DAYS = int(
    os.environ.get("WL_REPORT_AGING_DAYS")
    or os.environ.get("WL_REPORT_AGING_DAYS", "7")
)
_REPORT_PRUNE_QUIET_HOURS = int(
    os.environ.get("WL_REPORT_PRUNE_QUIET_HOURS")
    or os.environ.get("WL_REPORT_PRUNE_QUIET_HOURS", "72")
)


def _report_verdict(filed: int, signed: int, backlog: int, over_aging: int) -> str:
    """One deterministic word per ledger. Order matters: rot beats flow."""
    if backlog and over_aging >= max(5, (backlog + 4) // 5):
        return "aging"
    if filed >= 5 and filed >= 2 * signed:
        return "growing"
    if signed >= 0.8 * filed:
        return "keeping up"
    return "steady"


@router.get("/api/admin/overview/summary")
def api_admin_overview_summary(scope: str = "") -> JSONResponse:
    """Live status/priority counts for the Overview cards. ``scope`` narrows
    to one project store (wl-85); empty/"all" merges every store. Formerly
    /api/admin/cockpit/summary — renamed with the landing page.
    """
    prod = "" if scope.strip().lower() in ("", "all") else scope.strip().lower()
    if prod and get_product(prod) is None:
        return JSONResponse(
            {"ok": False, "error": "Unknown scope"}, status_code=404
        )
    all_tasks = _merged_scope_tasks_for_filters(prod)
    tasks = [t for t in all_tasks if t.status != TaskStatus.DONE]
    status_counts: Dict[str, int] = {s: 0 for s in TaskStatus.ALL if s != TaskStatus.DONE}
    priority_counts: Dict[str, int] = {"1": 0, "2": 0, "3": 0, "4": 0}
    for t in tasks:
        st = (t.status or "").strip()
        if st in status_counts:
            status_counts[st] += 1
        p = str(int(t.priority or 3))
        if p in priority_counts:
            priority_counts[p] += 1
    # Same 14-day activity window used by the cockpit chart.
    days = 14
    today = datetime.now(timezone.utc).date()
    day_list = [today - timedelta(days=(days - 1 - i)) for i in range(days)]
    activity_counts: Dict[str, int] = {d.isoformat(): 0 for d in day_list}
    for t in tasks:
        dt = _parse_task_date_utc(t.updated_at)
        if dt is None:
            continue
        key = dt.date().isoformat()
        if key in activity_counts:
            activity_counts[key] += 1
    payload = {
        "ok": True,
        "total": len(tasks),
        "status_counts": status_counts,
        "priority_counts": priority_counts,
        "activity_14d": [activity_counts[d.isoformat()] for d in day_list],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    resp = JSONResponse(payload)
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


@router.get("/api/report")
def api_report() -> JSONResponse:
    """The strategic report over the entire backlog, computed once (wl-156):
    per-store flow + verdicts, city-wide aging, urgent-but-unclaimed,
    the founder/worker blocker split, and prune candidates."""
    now = datetime.now(timezone.utc)
    win = now - timedelta(days=_REPORT_WINDOW_DAYS)
    aging_cut = now - timedelta(days=_REPORT_AGING_DAYS)
    prune_cut = now - timedelta(hours=_REPORT_PRUNE_QUIET_HOURS)

    stores: List[Dict[str, Any]] = []
    aging_buckets = [0, 0, 0, 0]  # <1d · 1-3d · 3-<aging> · >=aging
    oldest: List[Dict[str, Any]] = []
    urgent: List[Dict[str, Any]] = []
    prune: List[Dict[str, Any]] = []
    total_open = 0

    for spec in discover_products():
        tasks = _merged_scope_tasks_for_filters(spec.slug)
        filed = signed = open_n = backlog_n = over_aging = 0
        for t in tasks:
            st = (t.status or "").strip()
            c = _parse_task_date_utc(t.created_at)
            u = _parse_task_date_utc(t.updated_at)
            if c is not None and c >= win:
                filed += 1
            if st == TaskStatus.DONE:
                if u is not None and u >= win:
                    signed += 1
                continue
            if st == TaskStatus.CANCELED:
                continue
            open_n += 1
            if st != TaskStatus.BACKLOG:
                continue
            backlog_n += 1
            age_days = (now - c).total_seconds() / 86400 if c else 0.0
            aging_buckets[0 if age_days < 1 else 1 if age_days < 3 else
                          2 if age_days < _REPORT_AGING_DAYS else 3] += 1
            if c is not None and c < aging_cut:
                over_aging += 1
            entry = {"id": t.id, "store": spec.slug, "title": t.title,
                     "priority": int(t.priority or 3),
                     "age_days": round(age_days, 1)}
            oldest.append(entry)
            if entry["priority"] <= 2:
                urgent.append(entry)
            elif u is not None and u < prune_cut:
                prune.append(dict(entry, quiet_days=round(
                    (now - u).total_seconds() / 86400, 1)))
        total_open += open_n
        if open_n or filed or signed:
            stores.append({
                "slug": spec.slug, "display": spec.display,
                "prefix": spec.prefix, "filed": filed, "signed": signed,
                "net": filed - signed, "open": open_n, "backlog": backlog_n,
                "over_aging": over_aging,
                "ready": _merged_ready_count(spec.slug),
                "verdict": _report_verdict(filed, signed, backlog_n, over_aging),
            })

    oldest.sort(key=lambda e: -e["age_days"])
    urgent.sort(key=lambda e: -e["age_days"])
    prune.sort(key=lambda e: -e["quiet_days"])
    waiting_on_you = len(_partition_attention_items(
        _collect_founder_attention_items(now=now), now=now)[0])
    ready_total = sum(s["ready"] for s in stores)
    payload = {
        "ok": True,
        "generated_at": now.isoformat(),
        "window_days": _REPORT_WINDOW_DAYS,
        "aging_days": _REPORT_AGING_DAYS,
        "prune_quiet_hours": _REPORT_PRUNE_QUIET_HOURS,
        "stores": stores,
        "open_total": total_open,
        "blocker": {
            "waiting_on_you": waiting_on_you,
            "worker_ready": ready_total,
            "other": max(0, total_open - waiting_on_you - ready_total),
        },
        "aging_buckets": aging_buckets,
        "oldest": oldest[:6],
        "urgent_unclaimed": urgent[:8],
        "prune": {"count": len(prune), "items": prune[:10]},
    }
    resp = JSONResponse(payload)
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp
