"""WorkLane board server (#154, #163, #212).

A lightweight FastAPI app that serves the WorkLane board (task board + dev
dashboard) on a separate port (default 8799) with zero dependency on the
main tradeOS Cockpit web app. Uses :class:`worklane.trackers.sqlite.SQLiteTracker`
(default ``worklane/local/data/tradeos.db`` for product tickets; Ops DB under
``worklane/local/data/ops_tickets.db``) so
agents have a working task view even when
the main app is being torn apart.

Launch::

    ./tradeos tasks                  # foreground, default: 127.0.0.1:8799
    ./tradeos cockpit start          # background (nohup), same defaults
    TASK_PORT=9000 ./tradeos tasks   # override port
    ./tradeos cockpit install        # install as macOS LaunchAgent (auto-start)

This is a dev utility, not a product feature.  No auth, no CSRF, no mode
gating.  Reuses the design-token stylesheet from the main app for visual
consistency.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3

# :mod:`worklane.trackers.sqlite` mirrors to Ops Cockpit HTTP; when this
# server binds the same port as ``TASK_PORT``, suppress self-POST.
os.environ.setdefault("TRADEOS_SKIP_OPS_MIRROR", "1")
import sys
from urllib.parse import quote, urlencode
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Ensure project root is on sys.path so `core.*` imports resolve.
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from worklane.devqueue import (
    WorkQueue,
    group_by_file_conflict,
)
from worklane.trackers import (
    Task,
    TaskComment,
    TaskStatus,
    get_default_tracker,
)
from worklane import archival
from worklane.products import (
    ProductSpec,
    get_product,
    live_feed_product_slug,
    product_trackers,
    wl_data_dir,
)
from worklane.rendering import _esc, _label_chip, render_markdown

from worklane.board import (
    _board_styles,
    _client_js,
    _load_preview_comments_multi,
    list_tasks_for_wq_multi,
    parse_wq_priority,
    parse_wq_product,
    product_scope_from_list_path,
    _render_comments,
    _render_labels,
    _scoped_labels,
    _render_priority_badge,
    _render_status_badge,
    _render_task_board,
    _render_work_queue_filters,
    _owner_claim_html,
    _parse_iso_ts,
    _STATUS_LABELS,
    TICKETS_APP_ALL,
    TICKETS_APP_TRADEOS,
    _WORK_QUEUE_PATH,
    _wq_query_for_view,
    _wq_column_counts,
)

# Service start time — recorded at module load, used by the service-health pane (#485).
_SERVER_START: datetime = datetime.now(timezone.utc)

# Path to extracted surface assets (wl-222).
_SURFACES_DESK = Path(__file__).parent / "surfaces" / "desk"

# Canonical ticket store label (wl-90: Board and Table are sibling top-level
# views in the header; this names the store itself in card copy).
_TICKETS_SYSTEM_LABEL = "Tickets"
_OPS_TASK_LIST_PATH = TICKETS_APP_ALL

# D1 page shell extracted to worklane.surfaces.shell (code-efficiency first cut).
# Re-export so tests and routes keep importing from task_server.
from worklane.surfaces.chrome import (  # noqa: E402
    _BRAND_HEADER_HTML,
    _BRAND_MODE,
    _BRAND_NAME,
    _BRAND_SUBTITLE,
)
from worklane.surfaces.shell import (  # noqa: E402
    _OPS_READING_SHEET_CLOSE,
    _OPS_READING_SHEET_OPEN,
    _OPS_WORKSPACE_CLOSE,
    _OPS_WORKSPACE_OPEN,
    _SCOPE_NAV_MAX_INLINE,
    _render_overview_scope_nav,
    _render_scope_nav,
    _render_tickets_context_strip,
    _render_tickets_surface_nav,
    _seg_label_html,
    _split_for_middle_truncate,
    _task_page,
    _ticket_create_surface_from_scope,
    _tickets_path_for_scope_key,
    _tickets_product_from_labels,
    _tickets_shell_kwargs,
)


def _task_card(title: str, content: str) -> str:
    """Render a card without mode gating (standalone server has no modes)."""
    return (
        f"<section class='tos-card'>"
        f"<header class='tos-card-header'>"
        f"<h2 class='tos-card-title'>{_esc(title)}</h2>"
        f"</header>"
        f"<div class='tos-card-body'>{content}</div>"
        f"</section>"
    )


# wl-225: plugin shims + shared helpers extracted to server_helpers.py
from worklane.server_helpers import (  # noqa: E402
    _collect_founder_attention_items,
    _fetch_tradeos_json,
    _fetch_tradeos_ops_snapshot,
    _get_task_hot_or_archive,
    _list_tasks_for_wq_multi_resolved,
    _merged_scope_tasks_for_filters,
    _partition_attention_items,
    _resolve_product_tracker,
    _task_relations_dicts,
    _tracker_db_path,
    _tradeos_api_base,
    _tradeos_tickets_use_http_feed,
    _identity_config,
)


def _render_task_relations_panel(
    task_id: str, raw_id: str, surf: str, tracker: Any
) -> str:
    """HTML list of structured relations for the classic full-record page."""
    rels = _task_relations_dicts(surf, raw_id, tracker)
    if not rels:
        return (
            "<p class='dim' style='margin:0;'>No structured relations yet "
            "(wl-20). Link tickets via MCP/CLI <code>relations</code>.</p>"
        )
    rows: List[str] = []
    for r in rels:
        fr = str(r.get("from_id") or "")
        to = str(r.get("to_id") or "")
        rt = str(r.get("relation_type") or "")
        rows.append(
            "<li style='margin:4px 0;'>"
            f"<a href='/admin/desk?open={_esc(fr)}'>{_esc(fr)}</a>"
            f" <span class='dim'>{_esc(rt)}</span> "
            f"<a href='/admin/desk?open={_esc(to)}'>{_esc(to)}</a>"
            "</li>"
        )
    return (
        f"<ul style='margin:0; padding-left:18px;'>{''.join(rows)}</ul>"
        f"<p class='dim' style='margin:8px 0 0; font-size:var(--fs-sm);'>"
        f"Skim any linked work order in the Desk drawer · this page is the power bench."
        f"</p>"
    )


def _percentile(sorted_vals: List[float], pct: float) -> Optional[float]:
    """Linear-interpolation percentile over an already-sorted list (wl-107).

    ``pct`` is a 0..1 fraction (0.5 = median, 0.9 = p90). Same convention as
    numpy's default interpolation so results are easy to sanity-check.
    """
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    idx = pct * (len(sorted_vals) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = idx - lo
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac


def _status_totals(all_tasks: List[Task]) -> Dict[str, Any]:
    """All-time status histogram — mirrors MCPHandlers.wl_counts's bucket
    logic exactly (same TaskStatus.ALL buckets, zero buckets dropped, no
    window) so the Allocation totals row never drifts from wl_counts.
    """
    counts: Dict[str, int] = {s: 0 for s in TaskStatus.ALL}
    total = 0
    for t in all_tasks:
        if t.status not in counts:
            continue
        counts[t.status] += 1
        total += 1
    return {"total": total, "counts": {k: v for k, v in counts.items() if v > 0}}


# wl-107: focus cut — founder-session prep list. Clusters (by lane:* label)
# ranked by open P1/P2 count x staleness x blocked-status. Data only, no
# prescriptions — the founder decides what to do with the ranking.
def _focus_cut_rows(
    all_tasks: List[Task], blocked_entries: List[Any], *, now: datetime, prefix: str = "lane:"
) -> List[Dict[str, Any]]:
    blocked_ids = {bt.task.id for bt in blocked_entries}
    buckets: Dict[str, Dict[str, Any]] = {}

    def _bucket(name: str) -> Dict[str, Any]:
        return buckets.setdefault(name, {"lane": name, "open_p1p2": 0, "blocked": 0, "ages": []})

    for t in all_tasks:
        if t.status not in (TaskStatus.BACKLOG, TaskStatus.IN_PROGRESS, TaskStatus.IN_REVIEW):
            continue
        lanes = [lbl[len(prefix):] for lbl in (t.labels or []) if lbl.startswith(prefix)] or ["unlabeled"]
        ts = _parse_iso_ts(t.updated_at) or _parse_iso_ts(t.created_at)
        age_hours = max(0.0, (now - ts).total_seconds() / 3600) if ts is not None else 0.0
        is_p1p2 = int(t.priority or 3) <= 2
        is_blocked = t.id in blocked_ids
        for lane in lanes:
            b = _bucket(lane)
            if is_p1p2:
                b["open_p1p2"] += 1
            if is_blocked:
                b["blocked"] += 1
            b["ages"].append(age_hours)

    rows = []
    for b in buckets.values():
        if not (b["open_p1p2"] or b["blocked"]):
            continue
        staleness = max(b["ages"]) if b["ages"] else 0.0
        score = b["open_p1p2"] * (1.0 + staleness / 24.0) * (2.0 if b["blocked"] else 1.0)
        rows.append({
            "lane": b["lane"],
            "open_p1p2": b["open_p1p2"],
            "blocked": b["blocked"],
            "staleness_hours": staleness,
            "score": score,
        })
    rows.sort(key=lambda r: (-r["score"], r["lane"]))
    return rows


def _fmt_minutes(mins: int) -> str:
    """Compact duration like '45m', '3h20m', '2d4h'."""
    if mins < 60:
        return f"{mins}m"
    hrs, m = divmod(mins, 60)
    if hrs < 24:
        return f"{hrs}h{m}m" if m else f"{hrs}h"
    days, h = divmod(hrs, 24)
    return f"{days}d{h}h" if h else f"{days}d"


# wl-135: founder-attention feed — everything waiting on the founder,
# aggregated across every registered product store, always (not scoped
# to the current store). Five gates: review, decision label, human
# gate, stalled in-flight, date-gated embargo.
_ATTENTION_KIND_META = {
    "in_review": ("in review", "#38bdf8"),
    "founder_decision": ("decision", "#a855f7"),
    "human_gate": ("gated", "#ef4444"),
    "stalled": ("stalled", "#f59e0b"),
    "embargo": ("embargo", "#64748b"),
}


# Decision-board kind order: what You can act on first, embargoes last.
_ATTENTION_KIND_RANK = {
    "founder_decision": 0,
    "in_review": 1,
    "stalled": 2,
    "human_gate": 3,
    "embargo": 4,
}
_ATTENTION_KIND_BLURB = {
    "founder_decision": "Needs a yes/no or direction from You",
    "in_review": "Soft-lock / reserve (not auto gold — use human gate if You must act)",
    "stalled": "In flight with no update (stale claim)",
    "human_gate": "Needs You now (action-shaped human gate — not deferred/umbrella park)",
    "embargo": "Date-gated — not actionable until the date",
}


def _sort_attention_for_decisions(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Decisions first, then priority, then oldest wait."""
    return sorted(
        items,
        key=lambda it: (
            _ATTENTION_KIND_RANK.get(it.get("kind") or "", 9),
            int(it.get("priority") or 3),
            -(int(it.get("age_minutes") or 0)),
            str(it.get("id") or ""),
        ),
    )


def _render_attention_decision_card(it: Dict[str, Any]) -> str:
    """One scannable decision row — full note, age, product, open action."""
    kind = it.get("kind") or "other"
    tag, color = _ATTENTION_KIND_META.get(kind, (kind, "#64748b"))
    age = (
        _fmt_minutes(it["age_minutes"])
        if it.get("age_minutes") is not None
        else "—"
    )
    pri = int(it.get("priority") or 3)
    note = (it.get("note") or "").strip()
    title = it.get("title") or "(untitled)"
    prod = it.get("product") or ""
    tid = it.get("id") or "?"
    href = it.get("url") or f"/admin/desk?open={tid}"
    until = it.get("gate_until") or ""
    until_bit = (
        f"<span class='you-until'>until {_esc(str(until)[:10])}</span>"
        if until
        else ""
    )
    note_html = (
        f"<div class='you-card-note'>{_esc(note)}</div>" if note else ""
    )
    return (
        f"<article class='you-card kind-{_esc(kind)}' data-kind='{_esc(kind)}' "
        f"data-product='{_esc(prod)}' data-priority='{pri}' "
        f"data-age='{int(it.get('age_minutes') or 0)}' data-id='{_esc(tid)}'>"
        f"<div class='you-card-top'>"
        f"<span class='you-card-tag' style='color:{color};border-color:{color};'>{_esc(tag)}</span>"
        f"<a class='you-card-id' href='{_esc(href)}'>{_esc(tid)}</a>"
        f"<span class='you-card-pri'>P{pri}</span>"
        f"<span class='you-card-prod'>{_esc(prod)}</span>"
        f"<span class='you-card-age' title='how long waiting on You'>{_esc(age)}</span>"
        f"{until_bit}"
        f"</div>"
        f"<a class='you-card-title' href='{_esc(href)}'>{_esc(title)}</a>"
        f"{note_html}"
        f"<div class='you-card-actions'>"
        f"<a class='you-card-open' href='{_esc(href)}'>Open on Desk →</a>"
        f"</div>"
        f"</article>"
    )


def _render_attention_snooze_banner(
    snoozes: List[Dict[str, Any]],
    snoozed_items: List[Dict[str, Any]],
) -> str:
    """Active focus mutes + undo controls (not ticket gates)."""
    if not snoozes and not snoozed_items:
        return ""
    bits: List[str] = []
    for s in snoozes:
        scope = s.get("scope") or "product"
        until = (s.get("until") or "")[:16].replace("T", " ")
        if scope == "product":
            label = s.get("product") or "?"
        elif scope == "kind":
            label = "kind:" + (s.get("kind") or "?")
        else:
            label = "everything"
        n = sum(
            1
            for it in snoozed_items
            if (scope == "all")
            or (
                scope == "product"
                and (it.get("product") or "").lower() == (s.get("product") or "")
            )
            or (
                scope == "kind"
                and (it.get("kind") or "") == (s.get("kind") or "")
            )
        )
        bits.append(
            f"<span class='you-snooze-pill' data-scope='{_esc(scope)}' "
            f"data-product='{_esc(s.get('product') or '')}' "
            f"data-kind='{_esc(s.get('kind') or '')}'>"
            f"<b>{_esc(label)}</b> snoozed"
            + (f" · {n} hidden" if n else "")
            + (f" until {_esc(until)} UTC" if until else "")
            + f" <button type='button' class='you-unsnooze' "
            f"data-product='{_esc(s.get('product') or '')}' "
            f"data-kind='{_esc(s.get('kind') or '')}' "
            f"data-scope='{_esc(scope)}'>undo</button>"
            f"</span>"
        )
    return (
        f"<div class='you-snooze-banner' id='you-snooze-banner'>"
        f"<div class='you-snooze-banner-label'>Focus mutes (not ticket gates)</div>"
        f"<div class='you-snooze-pills'>{''.join(bits)}</div>"
        f"</div>"
    )


def _render_attention_page_body(
    items: List[Dict[str, Any]],
    *,
    snoozed: Optional[List[Dict[str, Any]]] = None,
    snoozes: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Full-page decision board for /admin/attention (wl-135).

    Not a dump of side-panel rows — grouped for triage: decisions → review →
    stalled → human gates (by product) → embargoes. Filters client-side.
    Persona law: human is You. Snoozes mute attention, not workflow gates.
    """
    snoozed = snoozed or []
    snoozes = snoozes or []
    if not items:
        empty_msg = (
            "<div class='pulse-empty'>&#10003; Nothing waiting on You"
            + (" (some items are snoozed — see banner)." if snoozed else " across any store.")
            + "</div>"
        )
        banner = _render_attention_snooze_banner(snoozes, snoozed)
        return _task_card("Waiting on You", banner + empty_msg)

    ordered = _sort_attention_for_decisions(items)
    by_kind: Dict[str, List[Dict[str, Any]]] = {}
    by_prod: Dict[str, int] = {}
    for it in ordered:
        by_kind.setdefault(it["kind"], []).append(it)
        p = it.get("product") or "—"
        by_prod[p] = by_prod.get(p, 0) + 1

    # KPI tiles — kind order
    tiles = ""
    for kind in sorted(by_kind.keys(), key=lambda k: _ATTENTION_KIND_RANK.get(k, 9)):
        tag, color = _ATTENTION_KIND_META.get(kind, (kind, "#64748b"))
        n = len(by_kind[kind])
        blurb = _ATTENTION_KIND_BLURB.get(kind, "")
        tiles += (
            f"<button type='button' class='you-kpi' data-filter-kind='{_esc(kind)}' "
            f"style='--kpi:{color};' title='{_esc(blurb)}'>"
            f"<span class='you-kpi-n'>{n}</span>"
            f"<span class='you-kpi-l'>{_esc(tag)}</span>"
            f"</button>"
        )
    tiles = (
        f"<button type='button' class='you-kpi on' data-filter-kind='all'>"
        f"<span class='you-kpi-n'>{len(ordered)}</span>"
        f"<span class='you-kpi-l'>all</span></button>"
        + tiles
    )

    prod_chips = "".join(
        f"<button type='button' class='you-pchip' data-filter-product='{_esc(p)}'>"
        f"{_esc(p)} <b>{n}</b></button>"
        for p, n in sorted(by_prod.items(), key=lambda kv: (-kv[1], kv[0]))
    )
    prod_chips = (
        "<button type='button' class='you-pchip on' data-filter-product='all'>"
        "all stores</button>" + prod_chips
    )

    # Sections by kind (decision order)
    sections = ""
    for kind in sorted(by_kind.keys(), key=lambda k: _ATTENTION_KIND_RANK.get(k, 9)):
        tag, color = _ATTENTION_KIND_META.get(kind, (kind, "#64748b"))
        blurb = _ATTENTION_KIND_BLURB.get(kind, "")
        group = by_kind[kind]
        # human_gate: subgroup by product so 40 tradeos gates aren't a wall
        if kind == "human_gate" and len(group) > 6:
            by_p: Dict[str, List[Dict[str, Any]]] = {}
            for it in group:
                by_p.setdefault(it.get("product") or "—", []).append(it)
            inner = ""
            for prod in sorted(by_p.keys(), key=lambda x: (-len(by_p[x]), x)):
                cards = "".join(_render_attention_decision_card(it) for it in by_p[prod])
                inner += (
                    f"<details class='you-prod-group' open data-product='{_esc(prod)}'>"
                    f"<summary><b>{_esc(prod)}</b> · {len(by_p[prod])} gated</summary>"
                    f"<div class='you-card-grid'>{cards}</div></details>"
                )
            body = inner
        else:
            body = (
                f"<div class='you-card-grid'>"
                + "".join(_render_attention_decision_card(it) for it in group)
                + "</div>"
            )
        sections += (
            f"<section class='you-section' data-section-kind='{_esc(kind)}'>"
            f"<header class='you-section-h' style='--kpi:{color};'>"
            f"<h3>{_esc(tag)} <span class='you-section-n'>{len(group)}</span></h3>"
            f"<p class='you-section-blurb'>{_esc(blurb)}</p>"
            f"</header>{body}</section>"
        )

    css = """
<style>
/* City-boundary board — suite daylight tokens (SUITE_PERIMETER pc-162). */
.you-board {
  --page: #faf6ec;
  --paper: #e2d9c2;
  --paper-top: #efe8d5;
  --card: #fffdf8;
  --line: #c4b8a4;
  --ink: #2a241c;
  --dim: #6b6154;
  --verd: #3d7a6a;
  --fire: #a33327;
  --gold: #c99212;
  --ok: #2e7d4f;
  max-width: 1100px;
  margin: 0 auto;
  color: var(--ink);
  font-family: Georgia, "Palatino Linotype", Palatino, "Times New Roman", serif;
}
.you-city-strip {
  display: flex; flex-wrap: wrap; align-items: center; gap: 8px 12px;
  margin: 0 0 14px; padding: 8px 12px;
  background: linear-gradient(180deg, var(--paper-top), var(--paper));
  border: 1px solid var(--line);
  border-radius: 8px;
  font-size: 12px;
}
.you-city-strip .yc-here { font-weight: 700; color: var(--ink); letter-spacing: .04em; }
.you-city-strip .yc-bench {
  color: var(--fire); background: color-mix(in srgb, var(--gold) 18%, var(--card));
  border: 1px solid color-mix(in srgb, var(--gold) 45%, var(--line));
  padding: 2px 8px; border-radius: 999px; letter-spacing: .03em;
}
.you-city-strip .yc-dim { color: var(--dim); }
.you-city-strip a {
  color: var(--verd); font-weight: 700; text-decoration: none;
  border: 1px solid var(--line); background: var(--card);
  padding: 4px 10px; border-radius: 8px;
}
.you-city-strip a:hover { border-color: var(--verd); }
.you-board-lead {
  font-size: 14px; color: var(--dim); margin: 0 0 10px; line-height: 1.5;
}
.you-board-lead b { color: var(--ink); }
.you-howto {
  font-size: 12.5px; color: var(--dim); margin: 0 0 14px; line-height: 1.45;
}
.you-kpis {
  display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 12px;
}
.you-kpi {
  display: flex; flex-direction: column; align-items: flex-start;
  min-width: 76px; padding: 10px 12px; border-radius: 8px; cursor: pointer;
  border: 1px solid var(--line); background: var(--card);
  color: var(--ink); font: inherit;
  box-shadow: 0 1px 0 #2a241c12;
}
.you-kpi.on, .you-kpi:hover {
  border-color: var(--verd);
  box-shadow: 0 0 0 1px var(--verd);
}
.you-kpi-n {
  font-size: 22px; font-weight: 700; line-height: 1.1;
  color: var(--kpi, var(--ink));
  font-variant-numeric: tabular-nums;
}
.you-kpi-l {
  font-size: 11px; color: var(--dim); text-transform: lowercase; margin-top: 2px;
  letter-spacing: .02em;
}
.you-toolbar {
  display: flex; flex-wrap: wrap; gap: 10px 14px; align-items: center;
  margin-bottom: 14px; padding: 8px 0;
  border-bottom: 1px solid var(--line);
}
.you-prods { display: flex; flex-wrap: wrap; gap: 6px; flex: 1; }
.you-pchip {
  border: 1px solid var(--line); background: var(--card);
  color: var(--dim); border-radius: 999px; padding: 4px 11px;
  font-size: 12px; cursor: pointer; font: inherit;
}
.you-pchip.on, .you-pchip:hover {
  border-color: var(--verd); color: var(--verd); font-weight: 600;
}
.you-pchip b { font-weight: 700; margin-left: 2px; color: var(--ink); }
.you-search {
  min-width: 200px; flex: 0 1 260px; padding: 7px 11px; border-radius: 8px;
  border: 1px solid var(--line); background: var(--card);
  color: var(--ink); font: inherit; font-size: 13px;
}
.you-search::placeholder { color: var(--dim); }
.you-section { margin: 0 0 22px; }
.you-section.is-hidden { display: none; }
.you-section-h {
  border-left: 3px solid var(--kpi, var(--verd)); padding: 0 0 0 10px; margin: 0 0 10px;
}
.you-section-h h3 {
  margin: 0; font-size: 16px; font-weight: 700; text-transform: capitalize;
  display: flex; align-items: baseline; gap: 8px;
  font-family: Georgia, "Palatino Linotype", Palatino, serif;
  letter-spacing: .02em;
}
.you-section-n {
  font-size: 12px; font-weight: 600; color: var(--dim);
  background: var(--paper-top); border: 1px solid var(--line);
  border-radius: 999px; padding: 1px 8px;
}
.you-section-blurb { margin: 4px 0 0; font-size: 12.5px; color: var(--dim); }
.you-card-grid {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: 10px;
}
.you-card {
  display: flex; flex-direction: column; gap: 6px;
  border: 1px solid var(--line); border-radius: 8px;
  background: var(--card); padding: 11px 12px;
  box-shadow: 0 1px 0 #2a241c10;
}
.you-card.is-hidden { display: none; }
.you-card-top {
  display: flex; flex-wrap: wrap; align-items: baseline; gap: 6px 8px;
  font-size: 11px; color: var(--dim);
  font-family: ui-monospace, Menlo, "SF Mono", monospace;
}
.you-card-tag {
  border: 1px solid; border-radius: 999px; padding: 1px 7px;
  font-weight: 700; text-transform: lowercase; font-size: 10px;
  background: var(--paper-top);
}
.you-card-id {
  color: var(--verd); font-weight: 700; text-decoration: none;
}
.you-card-id:hover { text-decoration: underline; }
.you-card-pri { font-weight: 700; color: var(--ink); }
.you-card-age { margin-left: auto; }
.you-until {
  color: var(--dim); border: 1px dashed var(--line); border-radius: 4px; padding: 0 5px;
}
.you-card-title {
  font-size: 14.5px; font-weight: 700; line-height: 1.35;
  color: var(--ink); text-decoration: none;
  font-family: Georgia, "Palatino Linotype", Palatino, serif;
}
.you-card-title:hover { color: var(--verd); }
.you-card-note {
  font-size: 12.5px; line-height: 1.45; color: var(--dim);
  border-left: 2px solid var(--paper);
  padding-left: 8px;
  max-height: 4.6em; overflow: hidden;
}
.you-card-note:hover, .you-card-note:focus {
  max-height: none; overflow: visible;
  background: var(--paper-top);
}
.you-card-actions { margin-top: auto; padding-top: 4px; }
.you-card-open {
  font-size: 12px; font-weight: 700; color: var(--verd); text-decoration: none;
}
.you-card-open:hover { text-decoration: underline; }
.you-prod-group {
  border: 1px solid var(--line); border-radius: 8px;
  margin-bottom: 10px; padding: 0 10px 10px;
  background: var(--page);
}
.you-prod-group > summary {
  cursor: pointer; padding: 10px 4px; font-size: 13px; color: var(--ink);
  list-style: none; font-weight: 600;
}
.you-prod-group > summary::-webkit-details-marker { display: none; }
.you-empty-filter {
  display: none; padding: 20px; text-align: center; color: var(--dim);
  font-size: 13px;
}
.you-empty-filter.show { display: block; }
.you-snooze-banner {
  border: 1px solid #c4a35a88; background: linear-gradient(180deg, #fff6dc, #f5e6b8);
  border-radius: 8px; padding: 10px 12px; margin-bottom: 12px;
  color: var(--ink);
}
.you-snooze-banner-label {
  font-size: 11px; font-weight: 700; color: var(--gold); margin-bottom: 6px;
  letter-spacing: .06em; text-transform: uppercase;
}
.you-snooze-pills { display: flex; flex-wrap: wrap; gap: 8px; }
.you-snooze-pill {
  display: inline-flex; align-items: center; gap: 8px;
  background: var(--card); border: 1px solid var(--line);
  border-radius: 999px; padding: 4px 10px; font-size: 12px; color: var(--ink);
}
.you-unsnooze {
  border: 0; background: transparent; color: var(--verd);
  cursor: pointer; font: inherit; font-size: 12px; font-weight: 700;
  text-decoration: underline;
}
.you-focus-bar {
  display: flex; flex-wrap: wrap; gap: 8px; align-items: center;
  margin: 0 0 14px; padding: 8px 12px;
  border: 1px dashed var(--line); border-radius: 8px;
  background: var(--card);
}
.you-focus-label { font-size: 12px; color: var(--dim); margin-right: 4px; }
.you-snooze-prod {
  border: 1px solid #c4a35a88; background: #fff6dc;
  color: #6a5010; border-radius: 999px; padding: 4px 10px;
  font-size: 12px; cursor: pointer; font: inherit; font-weight: 600;
}
.you-snooze-prod:hover { background: #ffe7a8; border-color: var(--gold); }
.tos-card:has(.you-board) {
  background: transparent;
  border: 0;
  box-shadow: none;
}
.tos-card:has(.you-board) > .tos-card-header {
  border-bottom: 1px solid #c4b8a4;
  margin-bottom: 12px;
  padding-bottom: 8px;
}
.tos-card:has(.you-board) .tos-card-title {
  font-family: Georgia, "Palatino Linotype", Palatino, serif;
  letter-spacing: .04em;
}
</style>

"""

    js = """
<script>
(function(){
  var kind = 'all', product = 'all', q = '';
  function apply(){
    var cards = document.querySelectorAll('.you-card');
    var nShow = 0;
    cards.forEach(function(c){
      var okK = (kind === 'all' || c.getAttribute('data-kind') === kind);
      var okP = (product === 'all' || c.getAttribute('data-product') === product);
      var hay = ((c.getAttribute('data-id')||'') + ' ' + (c.textContent||'')).toLowerCase();
      var okQ = !q || hay.indexOf(q) >= 0;
      var on = okK && okP && okQ;
      c.classList.toggle('is-hidden', !on);
      if(on) nShow++;
    });
    document.querySelectorAll('.you-section').forEach(function(sec){
      var k = sec.getAttribute('data-section-kind');
      var any = false;
      sec.querySelectorAll('.you-card').forEach(function(c){
        if(!c.classList.contains('is-hidden')) any = true;
      });
      /* When filtering to one kind, hide other sections entirely. */
      if(kind !== 'all' && k !== kind) any = false;
      sec.classList.toggle('is-hidden', !any);
      /* Collapse empty product groups inside human_gate */
      sec.querySelectorAll('.you-prod-group').forEach(function(g){
        var gAny = false;
        g.querySelectorAll('.you-card').forEach(function(c){
          if(!c.classList.contains('is-hidden')) gAny = true;
        });
        g.style.display = gAny ? '' : 'none';
      });
    });
    var empty = document.getElementById('you-empty-filter');
    if(empty) empty.classList.toggle('show', nShow === 0);
  }
  document.querySelectorAll('[data-filter-kind]').forEach(function(btn){
    btn.addEventListener('click', function(){
      kind = btn.getAttribute('data-filter-kind') || 'all';
      document.querySelectorAll('[data-filter-kind]').forEach(function(b){
        b.classList.toggle('on', b === btn);
      });
      apply();
    });
  });
  document.querySelectorAll('[data-filter-product]').forEach(function(btn){
    btn.addEventListener('click', function(){
      product = btn.getAttribute('data-filter-product') || 'all';
      document.querySelectorAll('[data-filter-product]').forEach(function(b){
        b.classList.toggle('on', b === btn);
      });
      apply();
    });
  });
  var search = document.getElementById('you-search');
  if(search){
    search.addEventListener('input', function(){
      q = (search.value || '').trim().toLowerCase();
      apply();
    });
  }

  function postJSON(url, body){
    return fetch(url, {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'Accept': 'application/json'},
      body: JSON.stringify(body || {})
    }).then(function(r){ return r.json().then(function(j){ if(!r.ok) throw new Error(j.detail||j.error||r.status); return j; }); });
  }
  document.querySelectorAll('.you-snooze-prod').forEach(function(btn){
    btn.addEventListener('click', function(){
      var prod = btn.getAttribute('data-product') || '';
      if(!prod || prod === '__custom__') return;
      if(!confirm('Snooze all ' + prod + ' items from Waiting on You until tomorrow?\n\nThis does NOT clear ticket gates — it only mutes the attention feed so you can focus elsewhere.')) return;
      btn.disabled = true;
      postJSON('/api/dev/attention/snooze', {product: prod, until: 'today', reason: 'focus elsewhere'})
        .then(function(){ window.location.reload(); })
        .catch(function(e){ alert('Snooze failed: ' + e.message); btn.disabled = false; });
    });
  });
  document.querySelectorAll('.you-unsnooze').forEach(function(btn){
    btn.addEventListener('click', function(){
      var body = {
        product: btn.getAttribute('data-product') || '',
        kind: btn.getAttribute('data-kind') || '',
        scope: btn.getAttribute('data-scope') || ''
      };
      btn.disabled = true;
      postJSON('/api/dev/attention/unsnooze', body)
        .then(function(){ window.location.reload(); })
        .catch(function(e){ alert('Undo failed: ' + e.message); btn.disabled = false; });
    });
  });
})();
</script>
"""
    # Decision-first lead copy
    n_dec = len(by_kind.get("founder_decision", []))
    n_rev = len(by_kind.get("in_review", []))
    n_gate = len(by_kind.get("human_gate", []))
    n_emb = len(by_kind.get("embargo", []))
    lead = (
        f"<p class='you-board-lead'><b>{len(ordered)}</b> items waiting on You"
        + (f" · <b>{len(snoozed)}</b> snoozed" if snoozed else "")
        + f" across {len(by_prod)} store{'s' if len(by_prod) != 1 else ''} in this view. "
        f"This page is the Desk <b>in-tray</b> (decision board) — the living "
        f"<a href='/admin/desk' style='color:var(--verd);font-weight:700'>top-down Desk room</a> "
        f"is separate. "
        f"Start with <b>decisions</b> ({n_dec}) and <b>review</b> ({n_rev}); "
        f"human gates ({n_gate}) listed here need a <b>concrete You action</b> "
        f"(parked deferred/umbrella gates are hidden from this tray — wl-257); "
        f"embargoes ({n_emb}) are date-locked.</p>"
        f"<p class='you-howto'>Tip: click a KPI tile or store chip to filter. "
        f"Snooze mutes notifications without clearing gates. "
        f"To park work without golding You: human gate + <code>deferred:</code> / "
        f"<code>umbrella</code> note (ready still blocked). "
        f"Hover a note to expand. Open a card when you are ready to act.</p>"
    )
    banner = _render_attention_snooze_banner(snoozes, snoozed)
    # Per-product snooze controls for products present on the board
    snooze_btns = "".join(
        f"<button type='button' class='you-snooze-prod' data-product='{_esc(p)}' "
        f"title='Hide {_esc(p)} from Waiting on You until tomorrow'>"
        f"Snooze {_esc(p)} today</button>"
        for p in sorted(by_prod.keys())
    )
    if by_prod:
        snooze_bar = (
            f"<div class='you-focus-bar'>"
            f"<span class='you-focus-label'>Not working a store today?</span>"
            f"{snooze_btns}"
            f"<button type='button' class='you-snooze-prod' data-product='__custom__' hidden></button>"
            f"</div>"
        )
    else:
        snooze_bar = ""
    city_strip = (
        "<div class='you-city-strip'>"
        "<span class='yc-here'>CITY</span>"
        "<span class='yc-dim'>·</span>"
        "<a href='http://127.0.0.1:8796/' title='Office foyer'>Office</a>"
        "<a href='http://127.0.0.1:8797/' title='Roster — WorkForce (who is working)'>Roster</a>"
        "<a href='/admin/desk' title='Desk Home — top-down work-order room (WorkLane D0)'>Desk</a>"
        "<span class='yc-here yc-bench'>Waiting on You</span>"
        "<span class='yc-dim'>· Desk in-tray (D1) · not the top-down floor</span>"
        "</div>"
    )
    body = (
        f"<div class='you-board' id='you-board'>"
        f"{city_strip}"
        f"{banner}"
        f"{lead}"
        f"{snooze_bar}"
        f"<div class='you-kpis'>{tiles}</div>"
        f"<div class='you-toolbar'>"
        f"<div class='you-prods'>{prod_chips}</div>"
        f"<input type='search' class='you-search' id='you-search' "
        f"placeholder='Filter by id, title, note…' autocomplete='off' />"
        f"</div>"
        f"<div class='you-empty-filter' id='you-empty-filter'>No items match this filter.</div>"
        f"{sections}"
        f"</div>"
        f"{css}{js}"
    )
    return _task_card(f"Waiting on You — {len(ordered)}", body)


# ── Rendering helpers with standalone URLs ──────────────────────────────
# Most rendering functions from admin_tasks.py are imported directly.  A
# few have hardcoded /admin/tasks URLs that we keep unchanged — the
# standalone server mounts routes at the same paths.

def _status_select(task_id: str, current: str) -> str:
    options = "".join(
        f"<option value='{_esc(s)}'{' selected' if s == current else ''}>"
        f"{_esc(_STATUS_LABELS.get(s, s))}</option>"
        for s in TaskStatus.ALL
    )
    return (
        f"<select class='admin-task-status' data-task-id='{_esc(task_id)}' "
        f"onchange='adminTaskStatusChange(this)'>{options}</select>"
    )


def _wq_poll_script(
    status: str, label: str, priority: str, product: str = "", gate: str = ""
) -> str:
    """Board polling must mirror the same filters as the page."""
    tsurf = _ticket_create_surface_from_scope(product or "")
    payload = json.dumps(
        {
            "status": status or "",
            "label": label or "",
            "priority": priority or "",
            "product": product or "",
            "gate": gate or "",
            "ticket_surface": tsurf,
        }
    )
    return f"<script>window.__WQ_POLL_PARAMS = {payload};</script>"


def _render_task_table(
    tasks: List[Task],
    previews: Optional[Dict[str, Dict[str, str]]] = None,
    scope_product: str = "",
) -> str:
    if not tasks:
        return "<p class='dim'>No tickets match the current filters.</p>"
    previews = previews or {}
    rows = "".join(
        _render_task_row(t, previews.get(t.id, {}), scope_product) for t in tasks
    )
    return (
        "<div class='ts-timetable'><table class='ts-timetable-table'>"
        "<thead><tr>"
        "<th class='tt-c-age' data-tt-key='age'>Age</th>"
        "<th class='tt-c-no' data-tt-key='no'>No.</th>"
        "<th class='tt-c-ticket' data-tt-key='ticket'>Work order</th>"
        "<th class='tt-c-labels' data-tt-key='labels'>Labels</th>"
        "<th class='tt-c-owner' data-tt-key='owner'>Owner</th>"
        "<th class='tt-c-status' data-tt-key='status'>Status</th>"
        "<th class='tt-c-pri' data-tt-key='pri'>Pri.</th>"
        "</tr></thead>"
        f"<tbody>{rows}</tbody>"
        "</table></div>"
        + _TIMETABLE_SORT_JS
    )


def _render_task_row(
    t: Task, preview: Optional[Dict[str, str]] = None, scope_product: str = ""
) -> str:
    # wl-38: timetable row — whole row opens the ticket (no inline status
    # edit here; that stays on the task detail page's _status_select).
    href = f"/admin/desk?open={_esc(t.id)}"
    ext = f" <span class='dim'>{_esc(t.ext_id)}</span>" if t.ext_id else ""
    updated_attr = _esc(t.updated_at or "")
    age_html = (
        f"<span class='tb-card-ago' data-iso='{updated_attr}'>"
        f"{_esc((t.updated_at or '')[:10])}</span>"
        if t.updated_at else "<span class='dim'>—</span>"
    )
    labels = _scoped_labels(t.labels, scope_product)
    # Owner column (wl-104): same claim identity/age/staleness as Board cards.
    claim_html = _owner_claim_html(t, preview or {})
    owner_html = claim_html or "<span class='dim'>—</span>"
    # Sort keys as row data attributes so header sorting never has to parse
    # rendered cell markup (badges, relative-time spans, label chips).
    sort_attrs = (
        f" data-tt-age='{updated_attr}'"
        f" data-tt-no='{_esc(t.id)}'"
        f" data-tt-ticket='{_esc((t.title or '').lower())}'"
        f" data-tt-labels='{_esc(' '.join(labels).lower())}'"
        f" data-tt-status='{_esc(t.status)}'"
        f" data-tt-pri='{int(t.priority or 3)}'"
    )
    return (
        f"<tr class='tt-row'{sort_attrs} onclick=\"location.href='{href}'\">"
        f"<td class='tt-c-age'>{age_html}</td>"
        f"<td class='tt-c-no'><span class='tb-card-id'>{_esc(t.id)}</span>{ext}</td>"
        f"<td class='tt-c-ticket'><a href='{href}'>{_esc(t.title)}</a></td>"
        f"<td class='tt-c-labels'>{_render_labels(labels)}</td>"
        f"<td class='tt-c-owner'>{owner_html}</td>"
        f"<td class='tt-c-status'>{_render_status_badge(t.status)}</td>"
        f"<td class='tt-c-pri'>{_render_priority_badge(int(t.priority or 3))}</td>"
        "</tr>"
    )


# wl-38 follow-on: click a timetable column header to sort the loaded rows
# client-side. First click uses the column's natural direction (Age newest
# first, Pri. most urgent first, everything else ascending); clicking the
# same header again flips it. Sorting is a pure tbody reorder — row markup,
# zebra striping (positional nth-child), and row-click navigation all
# survive untouched.
_TIMETABLE_SORT_JS = """
<script>
(function() {
  var STATUS_ORDER = { backlog: 0, in_progress: 1, in_review: 2, done: 3, canceled: 4 };

  function ticketNoKey(id) {
    // "wl-99" / "t-1253" → [prefix, number] so wl-9 sorts before wl-99.
    var m = /^(.*?)-?(\\d+)$/.exec(id || "");
    return m ? [m[1], parseInt(m[2], 10)] : [id || "", 0];
  }

  function cmp(a, b) { return a < b ? -1 : a > b ? 1 : 0; }

  function rowValue(row, key) {
    var v = row.getAttribute("data-tt-" + key) || "";
    if (key === "no") return ticketNoKey(v);
    if (key === "status") return STATUS_ORDER[v] !== undefined ? STATUS_ORDER[v] : 99;
    if (key === "pri") {
      var p = parseInt(v, 10) || 0;
      return p === 0 ? 99 : p;  // 0 = no priority, always last
    }
    return v;
  }

  function compareRows(a, b, key) {
    var va = rowValue(a, key), vb = rowValue(b, key);
    if (key === "no") return cmp(va[0], vb[0]) || cmp(va[1], vb[1]);
    if (key === "age") {
      // Empty updated_at sorts last regardless of direction (handled by
      // caller keeping empties pinned), here plain ISO string compare.
      if (va === "" && vb === "") return 0;
      if (va === "") return 1;
      if (vb === "") return -1;
      return cmp(va, vb);
    }
    return cmp(va, vb);
  }

  function sortTable(table, th) {
    var key = th.getAttribute("data-tt-key");
    var tbody = table.tBodies[0];
    if (!key || !tbody) return;

    var current = th.getAttribute("aria-sort");
    var firstDesc = key === "age";  // Age: first click = newest first
    var dir;
    if (current === "ascending") dir = "descending";
    else if (current === "descending") dir = "ascending";
    else dir = firstDesc ? "descending" : "ascending";

    var ths = table.tHead.querySelectorAll("th[data-tt-key]");
    for (var i = 0; i < ths.length; i++) ths[i].removeAttribute("aria-sort");
    th.setAttribute("aria-sort", dir);

    var rows = Array.prototype.slice.call(tbody.rows);
    var sign = dir === "descending" ? -1 : 1;
    rows.sort(function(a, b) {
      var c = compareRows(a, b, key);
      // Keep empty-age rows pinned to the bottom in both directions.
      if (key === "age") {
        var ea = !a.getAttribute("data-tt-age"), eb = !b.getAttribute("data-tt-age");
        if (ea !== eb) return ea ? 1 : -1;
      }
      return sign * c;
    });
    for (var j = 0; j < rows.length; j++) tbody.appendChild(rows[j]);
  }

  function init() {
    var table = document.querySelector(".ts-timetable-table");
    if (!table || !table.tHead) return;
    var ths = table.tHead.querySelectorAll("th[data-tt-key]");
    for (var i = 0; i < ths.length; i++) {
      (function(th) {
        th.addEventListener("click", function() { sortTable(table, th); });
      })(ths[i]);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
</script>
"""


# ── Routes ──────────────────────────────────────────────────────────────
# wl-222: sub-routers extracted from this module; imported here AFTER all
# helpers are defined so api/* modules can safely import helpers back from
# worklane.task_server without circular-import failures.
from worklane.api.events_stream import router as _events_router  # noqa: E402
from worklane.api.generation import router as _gen_router         # noqa: E402
from worklane.api.report import router as _report_router          # noqa: E402  wl-487
from worklane.api.scene import router as _scene_router            # noqa: E402
from worklane.api.tasks import router as _tasks_router            # noqa: E402  wl-225

logger = logging.getLogger(__name__)

router = APIRouter()
router.include_router(_events_router)
router.include_router(_gen_router)
router.include_router(_report_router)
router.include_router(_scene_router)
router.include_router(_tasks_router)


@router.get("/", response_class=HTMLResponse)
def index():
    # wl-132 cutover: the living desk scene is the room you walk into.
    # Overview keeps the analytics one click away.
    return RedirectResponse("/admin/desk", status_code=302)


@router.get("/admin/overview", response_class=HTMLResponse)
@router.get("/admin/overview/{scope}", response_class=HTMLResponse)
def ops_overview(scope: str = "all", days: int = 14) -> Any:
    """The desk's report (wl-156) — the strategic view over the entire
    backlog, in the paper voice. Supersedes the wl-85/89 pulse landing;
    that renderer and the allocation helpers stay dormant below for the
    dispatch-report seam (oc-22).

    The report is deliberately city-wide (the verdict strip carries the
    per-store split); ``scope`` is still validated so old per-store links
    404 on typos rather than silently widening, and ``days`` is accepted
    as a legacy no-op.
    """
    scope = (scope or "all").strip().lower()
    if scope != "all" and get_product(scope) is None:
        raise HTTPException(status_code=404, detail="Unknown overview scope")
    return _render_report_page()


@router.get("/admin/cockpit")
def admin_cockpit_legacy() -> RedirectResponse:
    """Legacy: Cockpit renamed Overview (wl-85) — the old name was host
    (tradeOS) vocabulary. Pulse had already merged into it (2026-07-10)."""
    return RedirectResponse("/admin/overview", status_code=302)


@router.get("/admin/pulse")
def admin_pulse() -> RedirectResponse:
    """Legacy: Pulse merged into the landing (2026-07-10), now Overview."""
    return RedirectResponse("/admin/overview", status_code=302)


def _product_next_id(spec: ProductSpec, tracker: Any) -> str:
    """Peek the store's AUTOINCREMENT sequence — next raw id it will mint."""
    db = getattr(tracker, "_db_path", None) or spec.db_path
    try:
        conn = sqlite3.connect(str(db), timeout=2.0)
        try:
            row = conn.execute(
                "SELECT seq FROM sqlite_sequence WHERE name='tasks'"
            ).fetchone()
            return str((row[0] if row else 0) + 1)
        finally:
            conn.close()
    except Exception:
        return "—"


def _render_settings_page(body: str) -> str:
    """Settings in the suite daylight shell (wl-188 item #2).

    Parallel to _render_report_page() — self-contained daylight HTML,
    no ops D1 shell (data-ops-shell / data-perimeter=d1 / Board·Table nav).
    """
    if _BRAND_MODE == "city":
        h1 = "SETTINGS <span class='fn'>&#xb7; Desk</span>"
        epithet = "ProtocolCity &#xb7; projects &#xb7; prefixes &#xb7; numbering &#xb7; service"
    else:
        h1 = "SETTINGS <span class='fn'>&#xb7; WorkLane</span>"
        epithet = "projects &#xb7; prefixes &#xb7; numbering &#xb7; service"
    # Bridge CSS: map ops-shell token names (_task_server_extra_css uses these)
    # to the daylight palette already declared by _REPORT_CSS.
    _bridge = """
/* wl-188: bridge — ops-shell token aliases → daylight palette */
:root,[data-theme="light"]{
  --bg:var(--paper);--fg:var(--ink);--border:var(--line);
  --bg2:var(--paper-top);--card:var(--paper);--raised:var(--paper-top);
  --hover-tint:color-mix(in srgb,var(--line) 12%,transparent);
  --neon:var(--verd);--green:var(--verd);--red:var(--stamp);
  --accent:var(--verd);--muted:var(--dim);
  --code-bg:var(--paper-top);
  --font-sans:"IBM Plex Sans",system-ui,sans-serif;
  --font-mono:"IBM Plex Mono",ui-monospace,monospace;
  --fs-xs:11px;--fs-sm:13px;--fs-base:15px;--fs-md:15px;--fs-lg:18px;--fs-xl:22px;
  --text-badge:11px;
  --r-sm:3px;--r-md:4px;--r-lg:8px;--r-pill:999px;
  --sp-xs:4px;--sp-sm:6px;--sp-md:10px;--sp-lg:16px;--sp-xl:22px;
  --mode-color:var(--verd);
  --mode-color-bg:color-mix(in srgb,var(--verd) 8%,transparent);
}
[data-theme="dark"]{
  --bg:var(--paper);--fg:var(--ink);--border:var(--line);
  --bg2:var(--paper-top);--card:var(--paper);--raised:var(--paper-top);
  --hover-tint:color-mix(in srgb,var(--line) 12%,transparent);
  --neon:var(--verd);--green:var(--verd);--red:var(--stamp);
  --accent:var(--verd);--muted:var(--dim);--code-bg:var(--paper-top);
  --mode-color:var(--verd);
  --mode-color-bg:color-mix(in srgb,var(--verd) 8%,transparent);
}
/* Settings daylight chrome */
main.stg-sheet{flex:1;overflow:auto;padding:18px 22px 36px;}
.ts-ops-page{max-width:1100px;}
.tos-card{background:var(--paper);border:1px solid var(--line);border-radius:4px;
  padding:14px 18px;margin-bottom:18px;box-shadow:0 1px 4px #2a241c0a;}
.tos-card-header{border-bottom:1px solid var(--rule);margin-bottom:10px;padding-bottom:8px;}
.tos-card-title{font:700 10px/1 "IBM Plex Sans",system-ui,sans-serif;letter-spacing:.18em;
  color:var(--dim);text-transform:uppercase;margin:0;}
.tos-card-body code{font-family:"IBM Plex Mono",monospace;font-size:13px;
  background:var(--paper-top);border:1px solid var(--rule);border-radius:3px;padding:1px 5px;}
.tos-table{border-collapse:collapse;width:100%;}
.tos-table th{text-align:left;font:700 9px/1 "IBM Plex Sans",system-ui,sans-serif;
  letter-spacing:.14em;text-transform:uppercase;color:var(--dim);
  padding:8px 10px 6px;border-bottom:1px solid var(--line);}
.tos-table td{padding:8px 10px;border-bottom:1px solid var(--rule);
  font-size:13px;vertical-align:middle;}
.tos-table tr:last-child td{border-bottom:none;}
.tos-table tr:hover td{background:var(--paper-top);}
.btn{display:inline-flex;align-items:center;padding:5px 14px;
  border-radius:var(--r-md,4px);border:1px solid var(--line);background:var(--paper);
  color:var(--ink);font-size:13px;font-weight:500;cursor:pointer;font-family:inherit;
  transition:border-color .12s;}
.btn:hover{border-color:var(--ink);}
.btn-sm{font-size:12px;padding:3px 10px;}
.btn.go{border-color:var(--verd);color:var(--verd);}
.btn.go:hover{background:color-mix(in srgb,var(--verd) 8%,transparent);}
.dim{color:var(--dim);}
/* Toast for showToast() */
.toast-container{position:fixed;bottom:24px;right:22px;z-index:9000;
  display:flex;flex-direction:column;gap:8px;}
.toast{background:var(--paper);border:1px solid var(--line);border-radius:4px;
  padding:8px 14px;font-size:13px;font-family:"IBM Plex Sans",system-ui,sans-serif;
  box-shadow:0 2px 10px #2a241c18;transition:opacity .3s;}
.toast.success{border-color:var(--verd);color:var(--verd);}
.toast.error{border-color:var(--stamp);color:var(--stamp);}
.toast.out{opacity:0;}
/* Theme toggle button */
#stgThemeBtn{background:none;border:1px solid var(--line);border-radius:4px;
  color:var(--dim);cursor:pointer;font-size:16px;padding:3px 9px;
  font-family:inherit;line-height:1;}
#stgThemeBtn:hover{color:var(--ink);border-color:var(--ink);}
"""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Settings · {_esc(_BRAND_NAME)}</title>
<style>{_REPORT_CSS}{_bridge}</style>
<script>
(function(){{
  var K='protocolcity-theme';
  try{{
    var leg=localStorage.getItem('wl-theme');
    if(leg&&!localStorage.getItem(K))
      localStorage.setItem(K,leg==='dark'?'dark':'light');
    var t=localStorage.getItem(K)||'light';
    if(t!=='dark'&&t!=='light')t='light';
    document.documentElement.setAttribute('data-theme',t);
  }}catch(e){{ document.documentElement.setAttribute('data-theme','light'); }}
}})();
function showToast(msg,type,duration){{
  type=type||'success'; duration=duration||2500;
  if(!window._tc){{
    var c=document.createElement('div');c.className='toast-container';
    document.body.appendChild(c);window._tc=c;
  }}
  var t=document.createElement('div');t.className='toast '+type;t.textContent=msg;
  window._tc.appendChild(t);
  setTimeout(function(){{t.classList.add('out');}},duration);
  setTimeout(function(){{t.remove();}},duration+350);
}}
function stgToggleTheme(){{
  var K='protocolcity-theme';
  var cur=localStorage.getItem(K)||'light';
  var next=cur==='dark'?'light':'dark';
  try{{localStorage.setItem(K,next);localStorage.setItem('wl-theme',next);}}catch(e){{}}
  document.documentElement.setAttribute('data-theme',next);
  var b=document.getElementById('stgThemeBtn');
  if(b){{b.textContent=next==='dark'?'☀':'☽';
    b.title=next==='dark'?'Switch to light theme':'Switch to dark theme';}}
}}
document.addEventListener('DOMContentLoaded',function(){{
  var cur=localStorage.getItem('protocolcity-theme')||'light';
  var b=document.getElementById('stgThemeBtn');
  if(b){{b.textContent=cur==='dark'?'☀':'☽';
    b.title=cur==='dark'?'Switch to light theme':'Switch to dark theme';}}
}});
</script>
</head><body>
<header class="nameplate">
  <a class="room-back" href="/admin/desk">← Desk</a>
  <div>
    <h1>{h1}</h1>
    <div class="epithet">{epithet}</div>
  </div>
  <div class="badges">
    <button type="button" id="stgThemeBtn" onclick="stgToggleTheme()"
            title="Switch to dark theme" aria-label="Toggle dark or light theme">&#9789;</button>
  </div>
</header>
<main class="stg-sheet">
{body}
</main>
<footer class="bar">
  <div>Settings · <a href="/admin/desk">← Desk</a>
    <a href="/admin/overview">Overview</a>
    <a class="quiet" href="/admin/tickets/all">Board (power)</a></div>
</footer>
</body></html>"""


@router.get("/admin/settings", response_class=HTMLResponse)
def admin_settings() -> str:
    """Configuration truth: products/prefixes/numbering, identity, service.

    Rename/set-prefix and add-product (wl-17) post to
    ``/api/admin/products[/{slug}]`` and reload on success.
    """
    from worklane.products import (
        prefix_collisions,
        products_config_path,
        wl_data_dir,
    )

    cfg_path = products_config_path()
    cfg_exists = cfg_path.exists()

    # wl-151: overlay-declared prefix collisions — discovery resolves them
    # (slug-as-prefix fallback), but the operator must SEE the bad overlay.
    collision_html = ""
    collisions = prefix_collisions()
    if collisions:
        bits = []
        for c in collisions:
            owners = ", ".join(f"<code>{_esc(s)}</code>" for s in c["slugs"])
            legacy = (
                f" (also the retired alias of <code>{_esc(c['legacy_owner'])}</code>)"
                if c["legacy_owner"] else ""
            )
            resolved = ", ".join(
                f"<code>{_esc(s)}</code> now renders <code>{_esc(p)}-…</code>"
                for s, p in c["resolved"].items()
            )
            bits.append(
                f"prefix <code>{_esc(c['prefix'])}</code> is declared by {owners}{legacy}"
                f" — {resolved}"
            )
        collision_html = (
            "<p style='color:#c0392b;'><strong>Prefix collision in the overlay:</strong> "
            + "; ".join(bits)
            + f". Fix <code>{_esc(str(cfg_path))}</code> — until then discovery "
            "falls back to slug-as-prefix so nothing mis-routes (wl-151/wl-152).</p>"
        )

    rows = []
    for spec, tracker in product_trackers():
        tasks = tracker.list_tasks(limit=None)
        open_n = len(
            [t for t in tasks if t.status not in (TaskStatus.DONE, TaskStatus.CANCELED)]
        )
        db = getattr(tracker, "_db_path", None) or spec.db_path
        slug_attr = _esc(spec.slug)
        rows.append(
            "<tr>"
            f"<td><code>{_esc(spec.slug)}</code></td>"
            f"<td><input class='ts-settings-input' type='text' "
            f"id='ts-prod-display-{slug_attr}' value='{_esc(spec.display)}' "
            f"maxlength='80' /></td>"
            f"<td><input class='ts-settings-input ts-settings-input--narrow' "
            f"type='text' id='ts-prod-prefix-{slug_attr}' value='{_esc(spec.prefix)}' "
            f"minlength='2' maxlength='8' /></td>"
            f"<td class='dim'>{_esc(str(db))}</td>"
            f"<td><code>{_esc(spec.prefix)}-{_esc(_product_next_id(spec, tracker))}</code></td>"
            f"<td>{open_n} open · {len(tasks)} total</td>"
            f"<td><button class='btn btn-sm go' type='button' "
            f"onclick=\"tsSettingsSaveProject('{slug_attr}')\">Save</button></td>"
            "</tr>"
        )
    overlay_example = (
        '{"&lt;slug&gt;": {"display": "…", "prefix": "…"}}'
    )
    add_product_html = (
        "<div class='ts-settings-add-row'>"
        "<input class='ts-settings-input' type='text' id='ts-new-prod-slug' "
        "placeholder='slug (e.g. myapp)' maxlength='40' />"
        "<input class='ts-settings-input' type='text' id='ts-new-prod-display' "
        "placeholder='Display name (optional)' maxlength='80' />"
        "<input class='ts-settings-input ts-settings-input--narrow' type='text' "
        "id='ts-new-prod-prefix' placeholder='prefix (optional, 2-8 chars)' "
        "minlength='2' maxlength='8' />"
        "<button class='btn btn-sm go' type='button' "
        "onclick='tsSettingsAddProject()'>Add project</button>"
        "</div>"
    )
    products_html = (
        collision_html
        + "<table class='tos-table'>"
        "<thead><tr><th>Slug</th><th>Display</th><th>Id prefix</th>"
        "<th>Store</th><th>Next id</th><th>Tickets</th><th></th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
        f"{add_product_html}"
        "<p class='dim' style='margin-top:8px;'>Numbering is per store (SQLite "
        "AUTOINCREMENT) — projects count independently; the prefix keeps merged "
        "views collision-free. Display names and prefixes: shipped defaults in "
        "<code>worklane/products.py</code>, overridable per project in "
        f"<code>{_esc(str(cfg_path))}</code> "
        f"({'present' if cfg_exists else 'not created yet'}) as "
        f"<code>{overlay_example}</code>. "
        "Prefix changes only affect how ids render — stored rows never rewrite. "
        "Other settings knobs (board poll interval, done-column cap, accent color, "
        "stall thresholds) remain follow-ups.</p>"
    )

    # wl-23: per-product archive counts + Compact now (move-not-delete).
    archive_rows = []
    for spec, tracker in product_trackers():
        hot = _tracker_db_path(tracker) or Path(spec.db_path)
        archive_path = archival.archive_db_path_for(hot)
        arc_n = archival.archive_counts(archive_path)
        hot_n = len(tracker.list_tasks(limit=None))
        slug_attr = _esc(spec.slug)
        archive_rows.append(
            "<tr>"
            f"<td><code>{_esc(spec.slug)}</code></td>"
            f"<td>{hot_n}</td>"
            f"<td id='ts-archive-count-{slug_attr}'>{arc_n}</td>"
            f"<td class='dim'><code>{_esc(str(archive_path.name))}</code></td>"
            f"<td><button class='btn btn-sm' type='button' "
            f"onclick=\"tsSettingsCompact('{slug_attr}')\">Compact now</button></td>"
            "</tr>"
        )
    archival_html = (
        "<table class='tos-table'>"
        "<thead><tr><th>Slug</th><th>Hot tickets</th><th>Archived</th>"
        "<th>Archive store</th><th></th></tr></thead>"
        f"<tbody>{''.join(archive_rows) if archive_rows else '<tr><td colspan=5 class=dim>No projects</td></tr>'}"
        "</tbody></table>"
        "<p class='dim' style='margin-top:8px;'>Compact moves done/canceled tickets "
        f"untouched for {archival.DEFAULT_ARCHIVE_AGE_DAYS} days into a sibling "
        "<code>*_archive.db</code> (same schema). Archival is <strong>not</strong> "
        "deletion — restore by internal id is reversible. Board / "
        "<code>scope_counts</code> only read the hot store. Do not compact from "
        "automation against live stores without an operator click.</p>"
    )

    ident = _identity_config()
    founder_identity_html = (
        "<table class='tos-table'><thead><tr><th>Founder id (§5.2, signs everything)</th>"
        "<th>Alias (presentation only)</th><th></th></tr></thead><tbody>"
        "<tr>"
        f"<td><code>{_esc(ident['founder_id'])}</code></td>"
        f"<td><input class='ts-settings-input' type='text' id='ts-founder-alias' "
        f"value='{_esc(ident['founder_alias'])}' maxlength='60' "
        f"placeholder='e.g. your name — shown wherever this id signed' /></td>"
        "<td><button class='btn btn-sm go' type='button' "
        "onclick='tsSettingsSaveIdentity()'>Save</button></td>"
        "</tr></tbody></table>"
        "<p class='dim' style='margin-top:8px;'>Aliases are paint, ids are identity "
        "(wl-148): comments stay signed with the canonical id forever — the alias only "
        "changes how founder-signed entries render, and the desk pre-fills the author "
        "box with this id. Stored in <code>identity.json</code> in the data dir.</p>"
    )
    identity_html = (
        founder_identity_html
        + "<p>Agent ids are stable identities the host deployment defines "
        "(PROTOCOL.md §5.2 holds the roster) — any signed id is rendered as-is. "
        "Reserved system authors: <code>cli-label</code>, <code>cli-update</code>, "
        "<code>dependency-guard</code>.</p>"
        "<table class='tos-table'><thead><tr><th>Guard</th><th>State</th></tr></thead><tbody>"
        "<tr><td>Signed comments (§3.8) — API rejects empty <code>author</code></td><td>enforced</td></tr>"
        "<tr><td>Close-out contract (§5) — <code>Completed:</code> requires <code>Verification:</code> + <code>Links:</code> with landing SHA (wl-396)</td><td>enforced</td></tr>"
        "<tr><td>Registered checks (wl-339) — when <code>local/config/closeout_checks.json</code> lists checks for a product, <code>Verification:</code> must cite them (docs/notes exempt)</td><td>enforced (opt-in)</td></tr>"
        "<tr><td>Blocked contract (§5) — <code>Blocked:</code> requires <code>Next step:</code></td><td>enforced</td></tr>"
        "<tr><td>Dependency freeze — declared blockers hold tickets in review</td><td>enforced</td></tr>"
        "</tbody></table>"
        "<p class='dim'>Toggles for these guards are deliberately not offered — "
        "consistency was the point (2026-07-10). If a host ever needs a relaxed "
        "profile, that's a PROTOCOL.md change first.</p>"
    )

    service_html = (
        "<table class='tos-table'><tbody>"
        f"<tr><th>Port</th><td><code>{_esc(os.environ.get('TASK_PORT', '8799'))}</code> (TASK_HOST/TASK_PORT)</td></tr>"
        f"<tr><th>Runtime dir</th><td><code>{_esc(str(wl_data_dir().parent))}</code> (WORKLANE_RUNTIME_DIR / WORKLANE_RUNTIME_DIR)</td></tr>"
        f"<tr><th>Data dir</th><td><code>{_esc(str(wl_data_dir()))}</code></td></tr>"
        "<tr><th>DB overrides</th><td><code>WORKLANE_DB</code> / <code>WORKLANE_DB</code> (tradeos store), <code>OPS_TICKETS_DB</code> (legacy)</td></tr>"
        "<tr><th>Board poll</th><td>10s (board JS) · Cockpit refresh 30s</td></tr>"
        "<tr><th>Boot persistence</th><td><code>scripts/install-macos-service.sh install</code> (com.worklane.server LaunchAgent, wl-14)</td></tr>"
        "</tbody></table>"
    )

    settings_js = """
<script>
  async function tsSettingsSaveIdentity() {
    var aEl = document.getElementById('ts-founder-alias');
    try {
      var resp = await fetch('/api/admin/identity', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ founder_alias: aEl ? aEl.value : '' })
      });
      var j = await resp.json();
      if (!j.ok) { showToast('Save failed: ' + (j.error || resp.status), 'error'); return; }
      showToast('Alias saved', 'success');
    } catch (e) {
      showToast('Network error', 'error');
    }
  }

  async function tsSettingsSaveProject(slug) {
    var dEl = document.getElementById('ts-prod-display-' + slug);
    var pEl = document.getElementById('ts-prod-prefix-' + slug);
    var payload = {};
    if (dEl) payload.display = dEl.value;
    if (pEl) payload.prefix = pEl.value;
    try {
      var resp = await fetch('/api/admin/products/' + encodeURIComponent(slug), {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      var j = await resp.json();
      if (!j.ok) { showToast('Save failed: ' + (j.error || resp.status), 'error'); return; }
      showToast('Saved ' + slug, 'success');
      setTimeout(function() { window.location.reload(); }, 600);
    } catch (e) {
      showToast('Network error', 'error');
    }
  }

  async function tsSettingsAddProject() {
    var slugEl = document.getElementById('ts-new-prod-slug');
    var dEl = document.getElementById('ts-new-prod-display');
    var pEl = document.getElementById('ts-new-prod-prefix');
    var slug = (slugEl && slugEl.value || '').trim();
    if (!slug) { showToast('Slug is required', 'error'); return; }
    var payload = { slug: slug };
    if (dEl && dEl.value) payload.display = dEl.value;
    if (pEl && pEl.value) payload.prefix = pEl.value;
    try {
      var resp = await fetch('/api/admin/products', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      var j = await resp.json();
      if (!j.ok) { showToast('Add failed: ' + (j.error || resp.status), 'error'); return; }
      if (j.warning) { showToast(j.warning, 'error'); }
      else { showToast('Added ' + slug, 'success'); }
      setTimeout(function() { window.location.reload(); }, j.warning ? 2600 : 600);
    } catch (e) {
      showToast('Network error', 'error');
    }
  }

  async function tsSettingsCompact(slug) {
    if (!confirm('Compact cold done/canceled tickets for ' + slug + ' into archive? (reversible)')) {
      return;
    }
    try {
      var resp = await fetch('/api/admin/products/' + encodeURIComponent(slug) + '/compact', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({})
      });
      var j = await resp.json();
      if (!j.ok) { showToast('Compact failed: ' + (j.error || resp.status), 'error'); return; }
      showToast(
        'Compacted ' + (j.tickets || 0) + ' ticket(s) · archive now ' + (j.archive_count || 0),
        'success'
      );
      setTimeout(function() { window.location.reload(); }, 800);
    } catch (e) {
      showToast('Network error', 'error');
    }
  }
</script>
"""
    body = (
        "<div class='ts-ops-page'>"
        + _task_card("Projects · stores · numbering", products_html)
        + _task_card("Done work order archival", archival_html)
        + _task_card("Identity & enforcement", identity_html)
        + _task_card("Service", service_html)
        + "</div>"
        + _task_server_extra_css()
        + settings_js
    )
    return _render_settings_page(body)


# Docs surface (wl-27): read-only render of the repo's own truth files.
# PROTOCOL.md/README.md/CLAUDE.md live at the repo root and ship in every
# export; ARCHITECTURE.md is host-boundary internal content, deliberately
# excluded from the WorkLane public export (scripts/export_worklane.sh) — it
# lives in _OPTIONAL_DOCS below so the tab disappears rather than always
# rendering a "could not read" error on a public install (wl-125).
_DOCS: List[Tuple[str, str, str]] = [
    ("process", "PROTOCOL.md", os.path.join(_ROOT, "PROTOCOL.md")),
    ("readme", "README.md", os.path.join(_ROOT, "README.md")),
    ("claude", "CLAUDE.md", os.path.join(_ROOT, "CLAUDE.md")),
]

_OPTIONAL_DOCS: List[Tuple[str, str, str]] = [
    ("truth", "ARCHITECTURE.md", os.path.join(_ROOT, "worklane", "ARCHITECTURE.md")),
    # The desk room guide (wl-146) — stamps, sorting, acknowledge doctrine.
    ("desk", "TICKET_DESK.md", os.path.join(_ROOT, "docs", "TICKET_DESK.md")),
]

# Per-agent instruction files. Lane operating rules are normative in
# PROTOCOL.md §6; these are the per-tool entry files each agent's runtime
# loads (Claude Code → CLAUDE.md above; Cursor/Codex → AGENTS.md;
# Grok CLI → GROK.md; Gemini CLI → GEMINI.md). Candidates that don't exist
# on disk are hidden from the nav rather than rendered as read errors, so
# the tab list always reflects the instruction files agents actually load.
_AGENT_DOCS: List[Tuple[str, str, str]] = [
    ("agents", "AGENTS.md", os.path.join(_ROOT, "AGENTS.md")),
    ("grok", "GROK.md", os.path.join(_ROOT, "GROK.md")),
    ("gemini", "GEMINI.md", os.path.join(_ROOT, "GEMINI.md")),
    ("cursorrules", ".cursorrules", os.path.join(_ROOT, ".cursorrules")),
]


def _docs_entries() -> List[Tuple[str, str, str]]:
    return (
        list(_DOCS)
        + [d for d in _OPTIONAL_DOCS if os.path.isfile(d[2])]
        + [d for d in _AGENT_DOCS if os.path.isfile(d[2])]
    )


def _docs_nav(active: str) -> str:
    items = []
    for slug, label, _path in _docs_entries():
        cls = "ts-seg ts-seg--on" if slug == active else "ts-seg"
        items.append(f'<a href="/admin/docs/{slug}" class="{cls}">{_esc(label)}</a>')
    return (
        '<nav class="ts-segmented" aria-label="Docs" style="margin-bottom:12px;">'
        + "".join(items)
        + "</nav>"
    )


@router.get("/admin/docs")
def admin_docs_index() -> RedirectResponse:
    return RedirectResponse(url=f"/admin/docs/{_DOCS[0][0]}")


@router.get("/admin/docs/{doc}", response_class=HTMLResponse)
def admin_docs_page(doc: str) -> str:
    match = next((d for d in _docs_entries() if d[0] == doc), None)
    if match is None:
        raise HTTPException(status_code=404, detail=f"Unknown doc: {doc}")
    slug, label, path = match
    try:
        raw = Path(path).read_text(encoding="utf-8")
        rendered = render_markdown(raw)
    except OSError as exc:
        rendered = f"<p class='dim'>Could not read <code>{_esc(label)}</code>: {_esc(str(exc))}</p>"
    body = (
        "<div class='ts-ops-page'>"
        + _docs_nav(slug)
        + _task_card(label, f"<div class='ts-doc-body'>{rendered}</div>")
        + "</div>"
    )
    return _task_page(f"Docs — {label}", body, nav_active="docs")


@router.get("/admin/products")
@router.get("/admin/products/{_sub:path}")
def products_legacy_redirect(_sub: str = "") -> RedirectResponse:
    """Legacy URLs: Products shell removed; send to Tickets home."""
    return RedirectResponse(f"{TICKETS_APP_ALL}?view=board", status_code=302)


def _tickets_page_title(view_norm: str, list_path: str) -> str:
    # wl-90: the view is the page — "Board · All", "Table · tradeOS".
    vn = {"board": "Board", "table": "Table"}.get(view_norm, view_norm)
    sk = product_scope_from_list_path(list_path)
    if sk == "tradeos":
        return f"{vn} · tradeOS"
    return f"{vn} · {sk}" if sk else f"{vn} · All"


@router.get("/admin/tasks")
def admin_tasks_redirect(request: Request) -> RedirectResponse:
    """Legacy list URL → canonical Tickets home."""
    q = request.query_params
    loc = TICKETS_APP_ALL + (f"?{q}" if len(q) > 0 else "")
    return RedirectResponse(loc, status_code=302)


@router.get(_WORK_QUEUE_PATH)
def work_queue_legacy_redirect(request: Request) -> RedirectResponse:
    """Normalize ``/admin/work-queue`` (+ optional ``?product=``) to ``/admin/tickets/…``."""
    d = dict(request.query_params)
    prod = parse_wq_product(d.pop("product", ""))
    path = TICKETS_APP_ALL
    if prod == "tradeos":
        path = TICKETS_APP_TRADEOS
    qs = urlencode(d)
    target = path + (f"?{qs}" if qs else "")
    return RedirectResponse(target, status_code=302)


@router.get("/admin/tickets/{surface}", response_class=HTMLResponse)
def tickets_app_page(
    surface: str,
    status: str = "",
    label: str = "",
    view: str = "",
    priority: str = "",
    product: str = "",
    dispatched: str = "",
    prompt: str = "",
    gate: str = "",
) -> Any:
    """Tickets app — surface is a first-class path (``all`` | ``tradeos``)."""
    if surface == "ops":
        q = _wq_query_for_view(
            view,
            status,
            label,
            parse_wq_priority(priority),
            list_path=TICKETS_APP_ALL,
        )
        return RedirectResponse(f"{TICKETS_APP_ALL}?{q}", status_code=302)
    surface = (surface or "").strip().lower()
    if surface != "all" and get_product(surface) is None:
        raise HTTPException(status_code=404, detail="Unknown tickets surface")
    list_path = f"/admin/tickets/{surface}"
    prod_scope = "" if surface == "all" else surface
    return _tickets_app_html(
        list_path=list_path,
        product_scope=prod_scope,
        status=status,
        label=label,
        view=view,
        priority=priority,
        product_query=product,
        dispatched=dispatched,
        prompt=prompt,
        gate=gate,
    )


def _tickets_app_html(
    *,
    list_path: str,
    product_scope: str,
    status: str,
    label: str,
    view: str,
    priority: str,
    product_query: str,
    dispatched: str,
    prompt: str,
    gate: str = "",
) -> str:
    from worklane.board import _parse_gate_filter  # noqa: PLC0415

    products = product_trackers()
    tracker_tradeos = next(
        (tr for spec, tr in products if spec.slug == live_feed_product_slug()),
        get_default_tracker(),
    )
    view_norm = (view or "board").strip().lower()
    if view_norm == "heatmap":
        view_norm = "board"
    if view_norm not in ("table", "board"):
        view_norm = "board"

    prio_int = parse_wq_priority(priority)
    gate_type = _parse_gate_filter(gate)
    st = (status or "").strip() or None
    prod = (
        product_scope
        if list_path.startswith("/admin/tickets/")
        else parse_wq_product(product_query)
    )

    # wl-104: Table also needs preview data (owner/claim-age/staleness column).
    want_preview = view_norm in ("board", "table")
    tasks, tradeos_prev = _list_tasks_for_wq_multi_resolved(
        products,
        status=st,
        label=label or None,
        priority=prio_int,
        product=prod,
        limit=500,
        with_preview=want_preview,
        gate_type=gate_type,
    )

    t_shell = _tickets_shell_kwargs(
        product=product_scope or "",
        scope_path=list_path,
        view=view_norm,
        status=status or "",
        label=label or "",
        priority=prio_int,
    )

    wq_notif = _render_tickets_context_strip()
    # Fetched once: chips count the whole scope; column headers count the
    # scope narrowed to the active filters (wl-47) — the capped page fetch
    # must not masquerade as either.
    merged_scope = _merged_scope_tasks_for_filters(prod)
    column_counts = _wq_column_counts(
        merged_scope,
        status=st,
        label=label or None,
        priority=prio_int,
        gate_type=gate_type,
    )
    # One command bar (wl-36): counts + jump + filters toggle + view toggle.
    # The old "Scope & filters" card, standalone jump row, and page-tools band
    # are gone — the count chips ARE the scope UI.
    command_bar = _render_work_queue_filters(
        list_path=list_path,
        current_view=view_norm,
        status=status,
        label=label,
        priority=prio_int,
        product=prod,
        gate=gate,
        merged_scope_tasks=merged_scope,
    )
    extra_js = _task_server_extra_js()
    extra_css = _task_server_extra_css()

    poll_inject = (
        _wq_poll_script(status, label, priority, prod or "", gate)
        if view_norm == "board"
        else ""
    )
    dispatched_banner = _render_dispatched_banner(dispatched, prompt)
    dispatch_hygiene = _render_work_queue_dispatch_hygiene(tracker_tradeos)

    previews = (
        _load_preview_comments_multi(
            products,
            tasks,
            tradeos_preview=tradeos_prev if tradeos_prev else None,
        )
        if want_preview else {}
    )

    if view_norm == "board":
        body = (
            "<div class='ts-wq-shell'>"
            + wq_notif
            + dispatched_banner
            + _board_styles()
            + extra_css
            + _OPS_READING_SHEET_OPEN
            + _OPS_WORKSPACE_OPEN
            + poll_inject
            + command_bar
            + _render_task_board(
                tasks, previews, column_counts, scope_product=prod or ""
            )
            + _OPS_WORKSPACE_CLOSE
            + _OPS_READING_SHEET_CLOSE
            + dispatch_hygiene
            + _client_js()
            + extra_js
            + "</div>"
        )
    else:
        body = (
            "<div class='ts-wq-shell'>"
            + wq_notif
            + dispatched_banner
            + _board_styles()
            + extra_css
            + _OPS_READING_SHEET_OPEN
            + _OPS_WORKSPACE_OPEN
            + command_bar
            + _render_task_table(tasks, previews, scope_product=prod or "")
            + _OPS_WORKSPACE_CLOSE
            + _OPS_READING_SHEET_CLOSE
            + dispatch_hygiene
            + _client_js()
            + extra_js
            + "</div>"
        )
    page_title = _tickets_page_title(view_norm, list_path)
    return _task_page(
        page_title,
        body,
        nav_active="work_queue",
        shell="tickets",
        **t_shell,
    )


@router.get("/admin/tasks/{task_id}", response_class=HTMLResponse)
def task_detail(task_id: str) -> str:
    surf, raw_id, tracker = _resolve_product_tracker(task_id)
    task: Optional[Task] = None
    comments: List[TaskComment] = []
    archived = False

    if surf == live_feed_product_slug() and _tradeos_tickets_use_http_feed():
        data = _fetch_tradeos_json(
            f"/api/ops/tickets/tradeos/{quote(raw_id, safe='')}?include_comments=1",
            timeout=10.0,
        )
        if data and data.get("ok") and isinstance(data.get("task"), dict):
            tm = data["task"]
            task = Task(
                id=task_id,
                title=str(tm.get("title") or ""),
                description=str(tm.get("description") or ""),
                status=str(tm.get("status") or TaskStatus.BACKLOG),
                priority=int(tm.get("priority") or 3),
                labels=list(tm.get("labels") or []),
                ext_id=tm.get("ext_id"),
                created_at=str(tm.get("created_at") or ""),
                updated_at=str(tm.get("updated_at") or ""),
            )
            for c in data.get("comments") or []:
                if not isinstance(c, dict):
                    continue
                comments.append(
                    TaskComment(
                        id=str(c.get("id") or "") or None,
                        task_id=task_id,
                        body=str(c.get("body") or ""),
                        author=str(c.get("author") or ""),
                        created_at=str(c.get("created_at") or ""),
                    )
                )
        if task is None:
            # Live feed miss → local hot store, then archive read-through.
            task, comments, archived = _get_task_hot_or_archive(tracker, raw_id)
    else:
        task, comments, archived = _get_task_hot_or_archive(tracker, raw_id)

    if task is None:
        body = (
            _render_tickets_context_strip()
            + "<p>No work order with id <code>"
            f"{_esc(task_id)}</code>. "
            f"<a href='{TICKETS_APP_ALL}'>Back to list</a></p>"
            + _client_js()
            + _task_server_extra_js()
        )
        return _task_page(
            "Work order not found",
            body,
            nav_active="work_queue",
            shell="tickets",
            **_tickets_shell_kwargs(
                product="",
                scope_path=TICKETS_APP_ALL,
                view="board",
                status="",
                label="",
                priority=None,
            ),
        )

    ext_html = (
        f"<div class='dim' style='font-size:var(--fs-sm);'>{_esc(task.ext_id)}</div>"
        if task.ext_id else ""
    )
    status_cell = (
        _render_status_badge(task.status)
        if archived
        else _status_select(task_id, task.status)
    )
    meta = (
        "<table class='tos-table'>"
        f"<tr><th>Status</th><td>{status_cell}</td></tr>"
        f"<tr><th>Priority</th><td>{_render_priority_badge(int(task.priority or 3))}</td></tr>"
        f"<tr><th>Labels</th><td>{_render_labels(task.labels)}</td></tr>"
        f"<tr><th>Created</th><td class='dim'>{_esc(task.created_at[:19] if task.created_at else '')}</td></tr>"
        f"<tr><th>Updated</th><td class='dim'>{_esc(task.updated_at[:19] if task.updated_at else '')}</td></tr>"
        "</table>"
    )

    desc = task.description or ""
    desc_html = (
        f"<div class='wl-md' style='font-size:var(--fs-sm); margin:0;'>"
        f"{render_markdown(desc)}</div>"
        if desc else "<p class='dim'>No description.</p>"
    )
    rels_html = _render_task_relations_panel(task_id, raw_id, surf, tracker)

    archive_banner = ""
    if archived:
        archive_banner = (
            "<div class='ts-archive-banner' role='status'>"
            "Archived (cold storage) — read-only. This work order was compacted out "
            "of the hot board; restore moves it back via the archival engine."
            "</div>"
        )

    comment_form = ""
    if not archived:
        comment_form = (
            "<hr style='margin:12px 0;'/>"
            "<div style='display:flex; flex-direction:column; gap:8px;'>"
            "<input id='admin-task-comment-author' required "
            "placeholder='Author — canonical agent id (PROTOCOL.md §5.2)' "
            "style='width:280px;'/>"
            "<textarea id='admin-task-comment-body' rows='4' "
            "style='width:100%; font-family:var(--font-mono);' "
            "placeholder='Add a comment...'></textarea>"
            "<div><button class='btn go' "
            f"onclick='adminTaskComment(\"{_esc(task_id)}\")'>Add comment</button></div>"
            "</div>"
            # Remember the signer between visits (PROTOCOL.md §3.8).
            "<script>(function(){try{var v=localStorage.getItem('wl-comment-author');"
            "var el=document.getElementById('admin-task-comment-author');"
            "if(v&&el&&!el.value)el.value=v;}catch(e){}})();</script>"
        )

    _ident = _identity_config()
    body = (
        _render_tickets_context_strip()
        + f"<p class='dim' style='margin-bottom:8px;'>"
        f"<a href='/admin/desk?open={_esc(task_id)}'>&larr; Desk drawer</a>"
        f" · <a href='{TICKETS_APP_ALL}'>All tickets</a></p>"
        + archive_banner
        + f"<h1 style='margin:0 0 4px 0;'>{_esc(task.title)}</h1>"
        f"{ext_html}"
        + _task_card("Metadata", meta)
        + _task_card("Description", desc_html)
        + _task_card("Relations", rels_html)
        + _task_card(
            f"Comments · {len(comments)}",
            _render_comments(
                comments,
                founder_id=_ident["founder_id"],
                founder_alias=_ident["founder_alias"],
            ) + comment_form,
        )
        + _client_js()
        + _task_server_extra_js()
    )
    # Scope the shell to the store the ticket lives in (store = product).
    wl = surf if get_product(surf) is not None else _tickets_product_from_labels(task.labels)
    detail_scope = _tickets_path_for_scope_key(wl)
    return _task_page(
        task.title,
        body,
        nav_active="work_queue",
        shell="tickets",
        **_tickets_shell_kwargs(
            product=wl,
            scope_path=detail_scope,
            view="board",
            status="",
            label="",
            priority=None,
        ),
    )


# ── JSON API ────��───────────────────────────────────────────────────────

def _city_root_path() -> Optional[str]:
    """City root directory, or None when no city is detectable (wl-155).

    WL stays host-neutral: WL_CITY_ROOT (or WL_CITY_ROOT) env wins; otherwise walk up from this
    repo to the topmost dir carrying an AGENTS.md (the city-root convention).
    A standalone checkout that is its own topmost AGENTS.md dir counts as no
    city — the check silently skips.
    """
    root = (os.environ.get("WL_CITY_ROOT") or os.environ.get("WL_CITY_ROOT") or "").strip()
    if not root:
        d = os.path.abspath(_ROOT)
        top = ""
        while True:
            if os.path.isfile(os.path.join(d, "AGENTS.md")):
                top = d
            parent = os.path.dirname(d)
            if parent == d:
                break
            d = parent
        if not top or top == os.path.abspath(_ROOT):
            return None
        root = top
    if not os.path.isdir(root):
        return None
    return root


def _city_folder_name() -> str:
    """Basename of the city root for `[Folder] Desk` mast parity with Office."""
    root = _city_root_path()
    if not root:
        return "City"
    name = os.path.basename(root.rstrip(os.sep)) or "City"
    return name


def _hood_slugify(name: str) -> str:
    """Folder basename → store slug (pc-313 / wl-270 twin of protocolcity.slugs).

    WorkLane must not import protocolcity. Lowercase; whitespace runs → one
    hyphen. ``Work Folder`` / ``SE Local HC`` → ``work-folder`` / ``se-local-hc``.
    """
    return "-".join(str(name).strip().lower().split())


def _city_neighborhood_slugs() -> Optional[set]:
    """Neighborhood store slugs at the city root, or None when no city is
    detectable (wl-155 · wl-270).

    Uses the same hyphen-slug rule as BluePrint ``slugify`` (pc-313), not bare
    ``name.lower()`` — otherwise spaced folders create stores that never match
    the warning check and emit false "no neighborhood folder" warnings.
    """
    root = _city_root_path()
    if not root:
        return None
    try:
        return {
            _hood_slugify(name)
            for name in os.listdir(root)
            if os.path.isdir(os.path.join(root, name))
            and os.path.isfile(os.path.join(root, name, "AGENTS.md"))
        }
    except OSError:
        return None


# /api/admin/products*, /api/admin/tasks*, /api/ops/tickets-health, and
# /api/dev/* routes moved to worklane/api/tasks.py (wl-225).

# ── Dev dashboard (#163) ───────────────────────────────────────────────
# Moved from core/web/routes/dev.py so the ops dashboard stays alive
# even when the main tradeOS app is restarting or stopped.

_DEV_STATUS_LABELS = {
    TaskStatus.BACKLOG:     "Backlog",
    TaskStatus.IN_PROGRESS: "In Progress",
    TaskStatus.IN_REVIEW:   "In Review",
    TaskStatus.DONE:        "Done",
    TaskStatus.CANCELED:    "Canceled",
}

_DEV_STATUS_ORDER = [
    TaskStatus.IN_PROGRESS,
    TaskStatus.IN_REVIEW,
    TaskStatus.BACKLOG,
    TaskStatus.DONE,
    TaskStatus.CANCELED,
]

_DEV_PRIORITY_LABELS = {1: "Urgent", 2: "High", 3: "Normal", 4: "Low"}


def _dev_card(
    title: str,
    content: str,
    tier: Optional[str] = None,
) -> str:
    """Card without mode gating — standalone server has no modes."""
    classes = ["tos-card"]
    if tier in ("positive", "negative", "warning"):
        classes.append(f"tier-{tier}")
    return (
        f"<section class='{' '.join(classes)}'>"
        f"<header class='tos-card-header'>"
        f"<h2 class='tos-card-title'>{_esc(title)}</h2>"
        f"</header>"
        f"<div class='tos-card-body'>{content}</div>"
        f"</section>"
    )


def _dev_task_row(t: Task, *, stack_cells: bool = False) -> str:
    labels = " ".join(_label_chip(l) for l in t.labels)
    ext = f"<span class='dim'>{_esc(t.ext_id)}</span> " if t.ext_id else ""
    pri = _DEV_PRIORITY_LABELS.get(int(t.priority or 3), "Normal")
    updated = _esc(t.updated_at[:19] if t.updated_at else "")
    task_cell = f"{ext}<strong>{_esc(t.title)}</strong>"
    pri_cell = f'<span class="dim">{_esc(pri)}</span>'
    upd_cell = f'<span class="dim">{updated}</span>'

    def _td(label: str, inner: str) -> str:
        if stack_cells:
            return f'<td data-h="{_esc(label)}">{inner}</td>'
        return f"<td>{inner}</td>"

    return (
        "<tr>"
        + _td("Task", task_cell)
        + _td("Priority", pri_cell)
        + _td("Labels", labels)
        + _td("Updated", upd_cell)
        + "</tr>"
    )


def _dev_status_card(title: str, tasks: List[Task]) -> str:
    if not tasks:
        body = "<p class='dim'>No tickets.</p>"
    else:
        rows = "".join(_dev_task_row(t) for t in tasks)
        body = (
            "<table class='tos-table'>"
            "<thead><tr>"
            "<th>Work order</th><th>Priority</th><th>Labels</th><th>Updated</th>"
            "</tr></thead>"
            f"<tbody>{rows}</tbody>"
            "</table>"
        )
    return _dev_card(f"{title} · {len(tasks)}", body)


def _dev_orphan_banner(orphans: List[Task]) -> str:
    if not orphans:
        return ""
    items = "".join(
        f"<li><code>{_esc(t.ext_id or t.id)}</code> · {_esc(t.title)}</li>"
        for t in orphans
    )
    body = (
        "<p>These tickets were left in <strong>In Progress</strong> from a "
        "previous session. Resume the work or run the shutdown protocol "
        "below to write closeout comments.</p>"
        f"<ul style='margin:8px 0 0 18px;'>{items}</ul>"
    )
    return _dev_card(f"Orphaned tickets · {len(orphans)}", body, tier="warning")


def _dev_kpi_strip(queue: WorkQueue, total_loaded: int) -> str:
    """Compact metrics row for Dev Queue (tablet-friendly)."""
    ready_n = len(queue.ready())
    blocked_n = len(queue.blocked())
    orphan_n = len(queue.orphans())
    orphan_cls = "devq-kpi-tile devq-kpi-tile--warn" if orphan_n else "devq-kpi-tile"
    return (
        '<div class="devq-kpi" role="region" aria-label="Queue summary">'
        f'<div class="devq-kpi-tile">'
        f'<span class="devq-kpi-val">{total_loaded}</span>'
        f'<span class="devq-kpi-lbl">In tracker</span></div>'
        f'<div class="devq-kpi-tile devq-kpi-tile--accent">'
        f'<span class="devq-kpi-val">{ready_n}</span>'
        f'<span class="devq-kpi-lbl">Ready</span></div>'
        f'<div class="devq-kpi-tile">'
        f'<span class="devq-kpi-val">{blocked_n}</span>'
        f'<span class="devq-kpi-lbl">Blocked</span></div>'
        f'<div class="{orphan_cls}">'
        f'<span class="devq-kpi-val">{orphan_n}</span>'
        f'<span class="devq-kpi-lbl">Orphans</span></div>'
        "</div>"
    )


def _dev_ready_queue(queue: WorkQueue) -> str:
    ready = queue.ready()
    if not ready:
        body = "<p class='dim'>No ready tickets — backlog is empty or every task is blocked.</p>"
        return _dev_card("Ready queue · 0", body)

    batches = group_by_file_conflict(ready)
    parts: List[str] = []
    for idx, batch in enumerate(batches, start=1):
        ids = " · ".join(_esc(i) for i in batch.ids)
        rows = "".join(_dev_task_row(t, stack_cells=True) for t in batch.tickets)
        files_html = ""
        if batch.shared_files:
            files = " ".join(
                f"<code>{_esc(f)}</code>" for f in batch.shared_files[:6]
            )
            extra = "" if len(batch.shared_files) <= 6 else f" +{len(batch.shared_files) - 6}"
            files_html = (
                f"<div class='devq-batch-files dim'><span class='devq-files-label'>Touches</span> "
                f"{files}{extra}</div>"
            )
        single = len(batch.tickets) == 1
        title = (
            f"Batch {idx} · 1 work order" if single else
            f"Batch {idx} · {len(batch.tickets)} work orders sharing files"
        )
        task_ids_csv = ",".join(str(t.id) for t in batch.tickets)
        parts.append(
            "<article class='devq-batch'>"
            "<header class='devq-batch-hd'>"
            f"<span class='devq-batch-title'>{_esc(title)}</span>"
            f"<span class='devq-batch-ids dim'>{ids}</span>"
            "</header>"
            f"{files_html}"
            "<table class='tos-table'>"
            "<thead><tr><th>Work order</th><th>Priority</th><th>Labels</th><th>Updated</th></tr></thead>"
            f"<tbody>{rows}</tbody>"
            "</table>"
            "<div class='devq-batch-actions'>"
            f"<form method='post' action='/api/dev/queue/dispatch?task_ids={_esc(task_ids_csv)}'>"
            "<button type='submit' class='btn btn-sm go'>Dispatch batch</button>"
            "</form>"
            "</div>"
            "</article>"
        )
    body = (
        "<p class='devq-ready-intro dim'>"
        f"{len(ready)} ready work order(s) in {len(batches)} batch(es). "
        "Work orders that touch the same files are grouped for one terminal."
        "</p>"
        + "".join(parts)
    )
    return _dev_card(f"Ready queue · {len(ready)}", body)


def _dev_blocked_card(queue: WorkQueue) -> str:
    blocked = queue.blocked()
    if not blocked:
        return ""

    parts: List[str] = []
    for bt in blocked:
        t = bt.task
        pri = _DEV_PRIORITY_LABELS.get(int(t.priority or 3), "Normal")
        blocker_items = ""
        for b in bt.blockers:
            if b.title:
                blocker_items += (
                    f"<li><code>{_esc(b.ticket_id)}</code> "
                    f"<span class='dim'>({_esc(b.status)})</span> — "
                    f"{_esc(b.title)}</li>"
                )
            else:
                blocker_items += (
                    f"<li><code>{_esc(b.ticket_id)}</code> "
                    "<span class='dim'>(unknown — still blocking)</span></li>"
                )
        ext = f"<span class='dim'>{_esc(t.ext_id)}</span> " if t.ext_id else ""
        parts.append(
            "<article class='devq-blocked'>"
            f"<div class='devq-blocked-hd'>{ext}<strong>{_esc(t.title)}</strong>"
            f" · <span class='dim'>{_esc(pri)}</span></div>"
            f"<ul class='devq-blocked-list'>{blocker_items}</ul>"
            "</article>"
        )
    body = "".join(parts)
    return _dev_card(f"Blocked · {len(blocked)}", body, tier="warning")


def _dev_shutdown_card() -> str:
    body = (
        "<p class='dim'>Walks every in-progress work order, scans <code>git log</code> "
        "for matching commits, and writes a closeout comment via the active "
        "ProjectTracker.</p>"
        "<p class='dim'>Trigger explicitly:</p>"
        "<pre style='margin:6px 0;padding:8px;background:#111;border-radius:4px;'>"
        "<code>POST /api/dev/queue/shutdown?apply=1</code></pre>"
        "<p class='dim'>Omit <code>apply=1</code> for a dry-run preview.</p>"
    )
    return _dev_card("Shutdown protocol", body)


def _render_dispatched_banner(dispatched: str, prompt: str) -> str:
    """Banner after POST /api/dev/queue/dispatch redirects to Work Queue."""
    if not (dispatched or "").strip():
        return ""
    ids = [tid.strip() for tid in dispatched.split(",") if tid.strip()]
    return _dev_card(
        f"Dispatched {len(ids)} task(s)",
        (
            "<p>Moved to <strong>In Progress</strong>. "
            "Paste this into a fresh terminal:</p>"
            f"<pre style='margin:6px 0;padding:8px;background:#111;border-radius:4px;'>"
            f"<code>{_esc(prompt)}</code></pre>"
        ),
        tier="positive",
    )


def _render_work_queue_dispatch_hygiene(tracker: Any) -> str:
    """Collapsible dispatch/orphan/shutdown tools — former /dev/dashboard body."""
    queue = WorkQueue(tracker)
    kpi = _dev_kpi_strip(queue, len(queue.all_tasks))
    grouped: Dict[str, List[Task]] = {s: [] for s in _DEV_STATUS_ORDER}
    for t in queue.all_tasks:
        grouped.setdefault(t.status, []).append(t)
    status_sections = "".join(
        _dev_status_card(_DEV_STATUS_LABELS.get(s, s), grouped.get(s, []))
        for s in _DEV_STATUS_ORDER
    )
    inner = (
        f"{kpi}"
        f"{_dev_orphan_banner(queue.orphans())}"
        f"{_dev_ready_queue(queue)}"
        f"{_dev_blocked_card(queue)}"
        f"{_dev_shutdown_card()}"
        f"{status_sections}"
    )
    return (
        "<section id='ops-dispatch-hygiene' class='ops-wq-dispatch-hygiene ops-dev-hygiene' "
        "data-ops-region='dispatch-hygiene' aria-label='Dispatch and hygiene'>"
        "<details class='ops-wq-dispatch-details'>"
        "<summary class='ops-wq-dispatch-summary dim'>"
        "Dispatch &amp; hygiene — ready queue, orphans, shutdown…"
        "</summary>"
        f"<div class='ops-wq-dispatch-details-body'>{inner}</div>"
        "</details></section>"
    )


@router.get("/dev/dashboard")
def dev_dashboard_legacy(request: Request) -> RedirectResponse:
    """Bookmarks / founder redirect — single Tickets home is Work Queue."""
    d = dict(request.query_params)
    if not d.get("view"):
        d["view"] = "table"
    loc = f"{TICKETS_APP_ALL}?{urlencode(d)}"
    return RedirectResponse(loc, status_code=302)


@router.get("/admin/attention", response_class=HTMLResponse)
def admin_attention() -> Any:
    """wl-135: decision board for everything waiting on You — header chip
    and Overview "view all". Always all stores (PROTOCOL.md §5 review,
    needs:founder-decision, gate_type=human, stalled in-flight, embargoes).
    Persona law: page title is You, not founder. Respects attention snoozes.
    """
    now = datetime.now(timezone.utc)
    all_items = _collect_founder_attention_items(now=now)
    visible, hidden, snoozes = _partition_attention_items(all_items, now=now)
    body = (
        _render_attention_page_body(visible, snoozed=hidden, snoozes=snoozes)
        + _task_server_extra_css()
    )
    return _task_page("Waiting on You", body, nav_active="attention")


def _task_server_extra_js() -> str:
    """Additional JS for task-server-only features (wl-222: loaded from file)."""
    return (_SURFACES_DESK / "desk.js").read_text(encoding="utf-8")


def _task_server_extra_css() -> str:
    """Additional CSS for task-server-only features (wl-222: loaded from file)."""
    return (_SURFACES_DESK / "desk.css").read_text(encoding="utf-8")


# ── The living desk (wl-132 / wl-167 / wl-170 / wl-179): the kiosk interior ─
# Ratified spec (founder verdicts on wl-132): grok's structure — nameplate,
# IN-TRAY of decision slips, hold bin of quiet claims, neighborhood-ledger
# blotter, stamp pad + FILED outbox rail — with claude's liveness (stamp
# thunk on fresh receipts, real per-item paper stack heights). CITY_DNA
# sec.5 (wl-170 / pc-52): page base is the city `page` token (#faf6ec) —
# one sheet across the rooms; paper/LAND surfaces keep plaza tones.
# wl-179: plat-era live sky/sun band purged after the cabinet pivot —
# nameplate is the top edge. The desk keeps the ticket verbs on Board/Table/
# ticket pages; the scene renders around them, never replaces them.
# Engineering constraint proven live on the sibling rooms: setInterval,
# never requestAnimationFrame.


# Directory-board doors (pc-37 case #4): mode-aware — a standalone WorkLane
# install is ONE room and shows no doors to uninstalled rooms.
_CITYHALL_URL = os.environ.get("WL_CITYHALL_URL") or os.environ.get("WL_CITYHALL_URL", "http://127.0.0.1:8796")
_WORKFORCE_URL = os.environ.get("WL_WORKFORCE_URL") or os.environ.get("WL_WORKFORCE_URL", "http://127.0.0.1:8797")


_DESK_SCENE_CSS = (_SURFACES_DESK / "desk-scene.css").read_text(encoding="utf-8")  # wl-222

# setInterval only — requestAnimationFrame suspends in background panes (the
# constraint proven live on the city-hall and dispatch scenes).
_DESK_SCENE_JS = (_SURFACES_DESK / "desk-scene.js").read_text(encoding="utf-8")  # wl-222


@router.get("/admin/desk", response_class=HTMLResponse)
def admin_desk() -> str:
    """The work-order desk as a live model (wl-132): the room you walk into.
    Self-contained page polling /api/scene; founder gate clears live in the
    drawer; Board/Table/Report remain D1 power benches. Mode-aware branding
    (wl-134) and mode-aware directory doors (pc-37 case #4)."""
    # Sixth naming amendment: city D0 mast is "[Folder] Desk" (Office parity).
    # Tab <title> keeps ProtocolCity — Desk · Tickets. Standalone keeps engine brand.
    if _BRAND_MODE == "city":
        folder = _esc(_city_folder_name())
        h1 = f"<span class='folder'>{folder}</span><span class='fn'>Desk</span>"
        epithet = "ProtocolCity · powered by WorkLane"
        doors = (
            f"<a href='{_esc(_CITYHALL_URL)}'>Office</a>"
            f"<a href='{_esc(_WORKFORCE_URL)}'>Roster</a>"
        )
    else:
        h1 = "WORKLANE — <span class='fn'>TICKETS</span>"
        epithet = "the work-order desk"
        doors = ""  # standalone: one room, no doors
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(_BRAND_NAME)}</title>
<style>{_DESK_SCENE_CSS}</style>
<script>
/* Suite theme before paint — shared key with Office / Roster. */
(function(){{
  var K='protocolcity-theme';
  try{{
    var leg=localStorage.getItem('wl-theme');
    if(leg&&!localStorage.getItem(K))
      localStorage.setItem(K,leg==='dark'?'dark':'light');
    var t=localStorage.getItem(K)||'light';
    if(t!=='dark'&&t!=='light')t='light';
    document.documentElement.setAttribute('data-theme',t);
  }}catch(e){{ document.documentElement.setAttribute('data-theme','light'); }}
}})();
</script>
</head><body>
<header class="nameplate">
  <div class="chrome-mast">
    <svg class="mast-stamp" viewBox="0 0 48 40" aria-hidden="true">
      <!-- CITY_DNA §3 — Desk mast mark: rubber stamp on pad (paper room) -->
      <ellipse cx="24" cy="34" rx="18" ry="4.5" fill="var(--paper)" stroke="var(--ink)" stroke-width="1.2"/>
      <rect x="16" y="4" width="16" height="8" rx="1.5" fill="var(--verd)" stroke="var(--ink)" stroke-width="1.1"/>
      <rect x="18" y="12" width="12" height="6" fill="#5a4e38" stroke="var(--ink)" stroke-width="1.1"/>
      <rect x="14" y="18" width="20" height="10" rx="1" fill="var(--stamp)" stroke="var(--ink)" stroke-width="1.1"/>
      <line x1="17" y1="22" x2="31" y2="22" stroke="var(--sheet)" stroke-width="1.2" opacity=".7"/>
      <line x1="17" y1="25" x2="28" y2="25" stroke="var(--sheet)" stroke-width="1.2" opacity=".55"/>
    </svg>
    <div class="chrome-title">
      <h1>{h1}</h1>
      <span class="epithet">{_esc(epithet)}</span>
    </div>
  </div>
  <div class="chrome-search">
    <div class="searchlight" id="searchlight">
      <input type="search" id="searchField" placeholder="find tickets…"
             autocomplete="off" spellcheck="false" enterkeyhint="search"
             aria-label="find tickets" aria-controls="searchResults"
             aria-autocomplete="list" aria-expanded="false" role="combobox">
      <div class="search-results tucked" id="searchResults" role="listbox" aria-label="search results"></div>
    </div>
  </div>
  <div class="chrome-right">
    <div class="chrome-ops badges"><span id="clock">—:—:—</span><span id="liveChip" class="hold">NO SIGNAL</span></div>
    <button type="button" class="theme-toggle" id="theme-toggle"
            title="Switch to dark theme" aria-label="Toggle dark or light theme">&#9789;</button>
    <a class="settings-gear" href="/admin/settings" title="Settings — projects, prefixes, numbering, service" aria-label="Settings">&#9881;</a>
    <div class="suite-doors">{doors}</div>
  </div>
</header>
<!-- wl-168: the paper line — desk counter between nameplate and the three columns;
     FILED → CLAIMED → SIGN-OFF DUE → SIGNED, live counts, click opens Board. -->
<div class="paper-line" id="paperLine" aria-label="the paper line · work-order stations">
  <div class="pl-rail">
    <button type="button" class="pl-station" id="plFiled" data-status="backlog"
      title="Skim filed (backlog) on this desk">
      <div class="pl-obj" aria-hidden="true">
        <svg viewBox="0 0 48 36" fill="none" stroke="var(--ink)" stroke-width="1.2">
          <rect x="10" y="18" width="28" height="12" fill="var(--sheet)"/>
          <rect x="12" y="12" width="28" height="12" fill="var(--sheet)"/>
          <rect x="14" y="6" width="28" height="12" fill="var(--sheet)"/>
          <line x1="18" y1="10" x2="36" y2="10" stroke="var(--rule)" stroke-width=".8"/>
          <line x1="18" y1="13" x2="34" y2="13" stroke="var(--rule)" stroke-width=".8"/>
        </svg>
      </div>
      <div class="pl-label">Filed</div>
      <div class="pl-count" id="plFiledN">0</div>
    </button>
    <div class="pl-arrow" aria-hidden="true">→</div>
    <button type="button" class="pl-station" id="plClaimed" data-status="in_progress"
      title="Skim claimed (in progress) on this desk">
      <div class="pl-obj" aria-hidden="true">
        <svg viewBox="0 0 48 36" fill="none" stroke="var(--ink)" stroke-width="1.2">
          <rect x="8" y="8" width="22" height="20" fill="var(--sheet)" transform="rotate(-8 19 18)"/>
          <line x1="12" y1="14" x2="24" y2="12" stroke="var(--rule)" stroke-width=".8"/>
          <line x1="12" y1="18" x2="23" y2="16" stroke="var(--rule)" stroke-width=".8"/>
          <!-- small walking worker (ink) -->
          <circle cx="34" cy="12" r="3" fill="#d9b98c" stroke="var(--ink)" stroke-width=".7"/>
          <rect x="31" y="15.5" width="6" height="8" rx="1.5" fill="#3d7a6a" stroke="var(--ink)" stroke-width=".6"/>
          <line x1="32" y1="24" x2="31" y2="30" stroke="var(--ink)" stroke-width="1.2"/>
          <line x1="36" y1="24" x2="37" y2="30" stroke="var(--ink)" stroke-width="1.2"/>
        </svg>
      </div>
      <div class="pl-label">Claimed</div>
      <div class="pl-count" id="plClaimedN">0</div>
    </button>
    <div class="pl-arrow" aria-hidden="true">→</div>
    <button type="button" class="pl-station" id="plSignoff" data-status="in_review"
      title="Skim sign-off due (in review) on this desk">
      <div class="pl-obj" aria-hidden="true">
        <svg viewBox="0 0 48 36" fill="none" stroke="var(--ink)" stroke-width="1.2">
          <!-- spike -->
          <line x1="24" y1="4" x2="24" y2="32" stroke="var(--ink)" stroke-width="1.6"/>
          <polygon points="24,2 26.5,7 21.5,7" fill="var(--ink)"/>
          <rect x="14" y="10" width="20" height="5" fill="var(--sheet)" transform="rotate(-12 24 12.5)"/>
          <rect x="14" y="16" width="20" height="5" fill="var(--sheet)" transform="rotate(8 24 18.5)"/>
          <rect x="15" y="22" width="18" height="4" fill="var(--sheet)" transform="rotate(-4 24 24)"/>
        </svg>
      </div>
      <div class="pl-label">Sign-off due</div>
      <div class="pl-count" id="plSignoffN">0</div>
    </button>
    <div class="pl-arrow" aria-hidden="true">→</div>
    <button type="button" class="pl-station" id="plSigned" data-status="done"
      title="Skim signed (done) on this desk">
      <div class="pl-obj" aria-hidden="true">
        <svg viewBox="0 0 48 36" fill="none" stroke="var(--ink)" stroke-width="1.2">
          <rect x="6" y="10" width="20" height="16" fill="var(--sheet)"/>
          <line x1="10" y1="15" x2="22" y2="15" stroke="var(--rule)" stroke-width=".8"/>
          <line x1="10" y1="19" x2="20" y2="19" stroke="var(--rule)" stroke-width=".8"/>
          <!-- stamp pad -->
          <rect x="28" y="12" width="14" height="12" rx="1.5" fill="none" stroke="var(--stamp)" stroke-width="2"/>
          <text x="35" y="21" text-anchor="middle" fill="var(--stamp)"
            font-size="6" font-family="IBM Plex Sans,sans-serif" font-weight="800"
            letter-spacing=".5">OK</text>
        </svg>
      </div>
      <div class="pl-label">Signed</div>
      <div class="pl-count" id="plSignedN">0</div>
    </button>
  </div>
  <div class="pl-flyers" id="plFlyers" aria-hidden="true"></div>
</div>
<!-- wl-154 front window + take-a-number retired — file/claim via AI / MCP / wl -->
<main class="surface">
  <div>
    <div class="tray"><div class="tray-head">
      <h2 id="inTrayTitle">In-tray · needs you (<span id="inCount">0</span>)</h2>
    </div>
      <div id="decisionsStack"><div class="empty-note">Waiting for the morning filing…</div></div>
    </div>
    <div class="tray"><h2>Hold bin · quiet claims (<span id="holdCount">0</span>)</h2>
      <div id="staleStack"><div class="empty-note">Waiting for the morning filing…</div></div>
    </div>
  </div>
  <div>
    <div class="blotter">
      <div class="blotter-head">
        <h2>Neighborhood ledgers · open work</h2>
        <div id="blotterScope"></div>
      </div>
      <div id="hoodList"><div class="empty-note">Waiting for the morning filing…</div></div>
    </div>
  </div>
  <div>
    <div class="pad"><h2>Stamp pad</h2>
      <div class="rubber idle" id="rubberStamp">FILED</div>
      <div class="inkring" id="inkRing"></div>
      <div class="pad-stats"><span class="n" id="padCount">0</span><span id="padWindow">filed · last 24h</span></div>
    </div>
    <div class="clip"><h2>Outbox · signed &amp; filed</h2>
      <div class="clip-list" id="shippedClip"><div class="clip-item empty-note">Waiting for the morning filing…</div></div>
    </div>
  </div>
</main>
<footer class="bar">
  <div>File &amp; claim via AI / MCP / <code>wl</code> ·
    <a href="/admin/tickets/all">Board (power view)</a>
    <a href="/admin/overview">Overview</a>
    <a href="/admin/settings">Settings</a>
    <a href="/admin/docs/desk">How to read this room</a></div>
</footer>
<div id="scrim" onclick="closeWO()"></div>
<aside id="wo" aria-label="work order">
  <div class="wo-head" id="woHead"></div>
  <div class="wo-body" id="woBody"></div>
  <div class="wo-foot" id="woFoot"></div>
  <div class="wo-sign">
    <div class="sign-as" id="woSignAs"></div>
    <textarea id="woNote" placeholder="sign a note into the day book…"></textarea>
    <button id="woSignBtn" onclick="signWO()">Sign &amp; file</button>
    <div class="err-note" id="woErr"></div>
  </div>
</aside>
<script>var FOUNDER={json.dumps(_identity_config())};</script>
{_DESK_SCENE_JS}</body></html>"""


# ── The report (wl-156): the desk's strategic view ────────────────────────
# Founder rulings (2026-07-14, all four on the ticket): six widgets approved
# (verdict strip / flow / blocker split / aging rack / priority integrity /
# prune list); epics alarm like everything else (thresholds are env knobs,
# per-store overrides deferred); the allocation panel is RETIRED to the
# dispatch side (oc-22 — its helpers stay below, dormant, for that seam);
# paper voice immediately. JSON engine lives in worklane/api/report.py
# (wl-487); this page, oc-15's daily founder brief, and city hall consume
# GET /api/report (reporting doctrine, wl-139: engines compute facts,
# dashboards render). HTML placard stays here — do not delete this pass.

_REPORT_CSS = """
@font-face { font-family:"IBM Plex Sans"; font-style:normal; font-weight:400;
  font-display:swap; src:url("/static/fonts/ibm-plex-sans-400.woff2") format("woff2"); }
@font-face { font-family:"IBM Plex Sans"; font-style:normal; font-weight:600;
  font-display:swap; src:url("/static/fonts/ibm-plex-sans-600.woff2") format("woff2"); }
@font-face { font-family:"IBM Plex Sans"; font-style:normal; font-weight:700;
  font-display:swap; src:url("/static/fonts/ibm-plex-sans-700.woff2") format("woff2"); }
@font-face { font-family:"IBM Plex Mono"; font-style:normal; font-weight:400;
  font-display:swap; src:url("/static/fonts/ibm-plex-mono-400.woff2") format("woff2"); }
/* Suite daylight sheet (pc-162 / Desk Home) — Overview is D1 furniture of Desk,
   not a grey pre-theme bench. Georgia body; Plex for dense meters. */
:root, [data-theme="light"] {
  --page:#faf6ec; --paper:#fffdf8; --paper-top:#efe8d5; --line:#c4b8a4;
  --ink:#2a241c; --dim:#6b6154; --blue:#1c4f9c; --stamp:#c0392b;
  --verd:#3d7a6a; --ok:#2e7d4f; --warn:#a8681e; --fire:#a33327;
  --rule:#c9c2b0; --pink:#fbe9ea; --pinkline:#e2b6ba;
  color-scheme:light;
}
[data-theme="dark"] {
  --page:#1a1814; --paper:#252018; --paper-top:#322c24; --line:#4a4338;
  --ink:#f0eade; --dim:#a89f8e; --blue:#7a9ec4; --stamp:#d4543f;
  --verd:#5a9a88; --ok:#4caf7d; --warn:#d9a441; --fire:#d4543f;
  --rule:#4a4338; --pink:#3a2426; --pinkline:#6a4044;
  color-scheme:dark;
}
* { box-sizing:border-box; margin:0; }
html,body { height:100%; }
body { background:var(--page); color:var(--ink);
  font:15px/1.45 "Iowan Old Style","Palatino Linotype",Palatino,Georgia,serif;
  display:flex; flex-direction:column; }
a { color:var(--verd); text-decoration:none; } a:hover { color:var(--ink); text-decoration:underline; }
.dim { color:var(--dim); } .ok { color:var(--ok); } .warn { color:var(--warn); }
.mono { font-family:"IBM Plex Mono",ui-monospace,monospace; font-size:13px; }
header.nameplate { background:linear-gradient(180deg,var(--paper-top),var(--paper));
  border-bottom:1px solid var(--line); box-shadow:0 2px 8px #2a241c12;
  padding:12px 22px 11px; display:flex; align-items:center; gap:14px; flex-wrap:wrap; }
h1 { font-size:18px; letter-spacing:.06em; font-weight:700; color:var(--ink); }
h1 .fn { color:var(--verd); letter-spacing:.12em; }
.epithet { color:var(--dim); font-size:12.5px; }
.badges { margin-left:auto; display:flex; gap:12px; align-items:center; font-size:12px;
  color:var(--dim); font-family:"IBM Plex Sans",system-ui,sans-serif; }
#liveChip { border:1.5px solid var(--ok); color:var(--ok); border-radius:3px;
  font-weight:700; font-size:10px; letter-spacing:.16em; padding:2px 8px;
  text-transform:uppercase; }
#liveChip.hold { border-color:var(--warn); color:var(--warn); }
.room-back { font-size:12px; color:var(--ink); border:1px solid var(--line);
  border-radius:4px; padding:4px 11px; white-space:nowrap; background:var(--paper);
  font-family:"IBM Plex Sans",system-ui,sans-serif; font-weight:600; }
.room-back:hover { border-color:var(--verd); color:var(--verd); text-decoration:none; }
.kpis { display:flex; flex-wrap:wrap; gap:10px; margin:0 0 6px; }
.kpi { flex:1 1 120px; min-width:110px; background:var(--paper);
  border:1px solid var(--line); border-radius:4px; padding:10px 12px;
  box-shadow:0 1px 3px #2a241c0a; }
.kpi .k { font:700 9px/1 "IBM Plex Sans",system-ui,sans-serif; letter-spacing:.16em;
  text-transform:uppercase; color:var(--dim); }
.kpi .v { font:700 20px/1.2 Georgia,serif; margin-top:4px; color:var(--ink); }
.kpi .v.hot { color:var(--fire); } .kpi .v.ok { color:var(--ok); }
.kpi .s { font-size:11.5px; color:var(--dim); margin-top:2px; }
main.sheet { flex:1; overflow:auto; padding:18px 22px 28px; max-width:1180px;
  width:100%; margin:0 auto; }
h2 { font:700 10px/1 "IBM Plex Sans",system-ui,sans-serif; letter-spacing:.18em;
  color:var(--dim); text-transform:uppercase; margin:20px 0 9px; }
h2:first-child { margin-top:0; }
.card { background:var(--paper); border:1px solid var(--line); border-radius:4px;
  box-shadow:0 1px 4px #2a241c0a; padding:14px 16px; }
.verdicts { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
  gap:10px; }
.verdict { background:var(--paper); border:1px solid var(--line); border-radius:4px;
  box-shadow:0 1px 3px #2a241c0a; padding:10px 14px; position:relative; overflow:hidden; }
.verdict::before { content:""; position:absolute; left:0; top:0; bottom:0; width:4px;
  background:var(--rule); }
.verdict.aging::before { background:var(--fire); }
.verdict.growing::before { background:var(--warn); }
.verdict.keeping::before, .verdict.steady::before { background:var(--ok); }
.verdict .nm { font-weight:700; font-size:13px; }
.verdict .nm a { color:var(--ink); } .verdict .nm a:hover { color:var(--verd); }
.verdict .vw { font-size:16px; font-weight:700; margin-top:2px;
  font-family:"IBM Plex Sans",system-ui,sans-serif; letter-spacing:.04em; }
.verdict.aging .vw { color:var(--fire); } .verdict.growing .vw { color:var(--warn); }
.verdict.keeping .vw, .verdict.steady .vw { color:var(--ok); }
.verdict .meta { font-size:11.5px; color:var(--dim); margin-top:3px; }
.rows { display:grid; grid-template-columns:130px minmax(0,1fr) 90px;
  gap:6px 12px; align-items:center; font-size:13px;
  font-family:"IBM Plex Sans",system-ui,sans-serif; }
.rows .lbl { color:var(--dim); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.rows .num { color:var(--dim); text-align:right; font-variant-numeric:tabular-nums; }
/* chart bars are .meter, never .bar — footer.bar wears that class (wl-163) */
.meter { display:block; height:9px; border-radius:2px; background:var(--rule); }
.meter.g { background:#a8d4b8; }
.meter + .meter { margin-top:2px; }
.split { display:flex; height:22px; border-radius:3px; overflow:hidden;
  border:1px solid var(--line); }
.split .you { background:var(--fire); } .split .rdy { background:#a8d4b8; }
.split .oth { background:var(--rule); }
.legend { display:flex; gap:18px; font-size:12.5px; margin-top:8px; flex-wrap:wrap;
  font-family:"IBM Plex Sans",system-ui,sans-serif; }
.li { display:flex; justify-content:space-between; gap:10px; padding:7px 2px;
  border-bottom:1px dotted var(--line); font-size:13px; }
.li:last-child { border-bottom:0; }
.li .age { color:var(--warn); white-space:nowrap; font-variant-numeric:tabular-nums;
  font-family:"IBM Plex Sans",system-ui,sans-serif; }
.tag { border:1px solid var(--line); border-radius:3px; padding:0 6px; font-size:11px;
  color:var(--dim); background:var(--page);
  font-family:"IBM Plex Sans",system-ui,sans-serif; }
.note { font-size:12px; color:var(--dim); margin-top:8px; }
.stamp-count { display:inline-block; border:2.5px solid var(--stamp); color:var(--stamp);
  border-radius:4px; font-weight:800; padding:6px 12px; transform:rotate(-3deg);
  font-size:15px; letter-spacing:.06em;
  font-family:"IBM Plex Sans",system-ui,sans-serif; }
.grid2 { display:grid; grid-template-columns:1fr 1fr; gap:18px; align-items:start; }
@media (max-width:900px){ .grid2 { grid-template-columns:1fr; } }
footer.bar { border-top:1px solid var(--line); background:var(--paper);
  padding:9px 22px; display:flex; justify-content:space-between; gap:14px;
  flex-wrap:wrap; font-size:12px; color:var(--dim);
  font-family:"IBM Plex Sans",system-ui,sans-serif; }
footer.bar a { margin-right:14px; color:var(--verd); }
footer.bar a.quiet { color:var(--dim); }
"""

# setInterval only — never requestAnimationFrame (city constraint).
_REPORT_JS = """
<script>
"use strict";
var R=null;
function esc(s){return String(s==null?"":s).replace(/[&<>"']/g,function(c){
  return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];});}
function $(id){return document.getElementById(id);}
function pct(n,d){return d>0?Math.max(n>0?2:0,Math.round(n/d*100)):0;}
function deskOpen(id){return "/admin/desk?open="+encodeURIComponent(id);}
function deskCab(slug){return "/admin/desk?cabinet="+encodeURIComponent(slug);}
function render(){
  var d=R; if(!d)return;
  var b=d.blocker||{};
  /* City pulse — strategic facts at a glance (wl-156). */
  if($("kpiOpen")) $("kpiOpen").textContent=String(d.open_total||0);
  if($("kpiYou")){
    $("kpiYou").textContent=String(b.waiting_on_you||0);
    $("kpiYou").className="v"+((b.waiting_on_you|0)>0?" hot":"");
  }
  if($("kpiReady")){
    $("kpiReady").textContent=String(b.worker_ready||0);
    $("kpiReady").className="v"+((b.worker_ready|0)>0?" ok":"");
  }
  if($("kpiWin")) $("kpiWin").textContent=(d.window_days||7)+"d";
  if($("w1")) $("w1").textContent=String(d.window_days||7);

  var vz="";
  (d.stores||[]).forEach(function(s){
    var cls=s.verdict==="keeping up"?"keeping":esc(s.verdict);
    var extra=s.verdict==="aging"
      ? esc(s.over_aging)+" orders past "+esc(d.aging_days)+"d"
      : "filed "+esc(s.filed)+" \\u00b7 signed "+esc(s.signed);
    /* Store → living Desk skim (?cabinet=), never Board table. */
    vz+='<div class="verdict '+cls+'"><div class="nm"><a href="'+deskCab(s.slug)+
      '" title="Open Desk skim for this cabinet">'+esc(s.display||s.slug)+'</a></div>'+
      '<div class="vw">'+esc(s.verdict)+'</div>'+
      '<div class="meta">'+extra+' \\u00b7 net '+(s.net>=0?"+":"")+esc(s.net)+
      ' \\u00b7 '+esc(s.open)+' open</div></div>';});
  $("verdictStrip").innerHTML=vz||'<div class="note">no ledgers with activity</div>';

  var maxF=1; (d.stores||[]).forEach(function(s){maxF=Math.max(maxF,s.filed,s.signed);});
  var fz="";
  (d.stores||[]).forEach(function(s){
    fz+='<span class="lbl"><a href="'+deskCab(s.slug)+'">'+esc(s.display||s.slug)+'</a></span>'+
      '<span><span class="meter" style="width:'+pct(s.filed,maxF)+'%"></span>'+
      '<span class="meter g" style="width:'+pct(s.signed,maxF)+'%"></span></span>'+
      '<span class="num">'+esc(s.filed)+' / '+esc(s.signed)+'</span>';});
  $("flowRows").innerHTML=fz;

  var tot=Math.max(1,d.open_total||0);
  $("splitBar").innerHTML=
    '<div class="you" style="width:'+pct(b.waiting_on_you,tot)+'%"></div>'+
    '<div class="rdy" style="width:'+pct(b.worker_ready,tot)+'%"></div>'+
    '<div class="oth" style="flex:1"></div>';
  $("splitLegend").innerHTML=
    '<span><b style="color:var(--fire)">'+esc(b.waiting_on_you)+' waiting on you</b></span>'+
    '<span><b style="color:var(--ok)">'+esc(b.worker_ready)+' worker-ready</b></span>'+
    '<span class="dim">'+esc(b.other)+' in flight / gated</span>'+
    '<span class="dim">'+esc(d.open_total)+' open in all</span>';

  var ab=d.aging_buckets||[0,0,0,0], maxA=Math.max.apply(null,ab.concat([1]));
  var lbls=["&lt; 1 day","1\\u20133 days","3\\u2013"+esc(d.aging_days)+" days","&gt; "+esc(d.aging_days)+" days"];
  var az="";
  ab.forEach(function(n,i){
    az+='<span class="lbl">'+lbls[i]+'</span>'+
      '<span><span class="meter'+(i<2?" g":"")+'" style="width:'+pct(n,maxA)+
      '%'+(i===3&&n?';background:var(--pinkline)':'')+'"></span></span>'+
      '<span class="num">'+esc(n)+'</span>';});
  $("agingRows").innerHTML=az;
  var oz=(d.oldest||[]).map(function(e){
    return '<a href="'+deskOpen(e.id)+'" class="mono">'+esc(e.id)+'</a> ('+
      esc(Math.round(e.age_days))+'d)';}).join(" \\u00b7 ");
  $("agingNote").innerHTML=oz?("oldest on the rack: "+oz):"the rack is fresh";

  var uz="";
  (d.urgent_unclaimed||[]).forEach(function(e){
    uz+='<div class="li"><span><a href="'+deskOpen(e.id)+'" class="mono">'+
      esc(e.id)+'</a> <span class="tag">'+esc(e.store)+'</span> '+
      esc(String(e.title).slice(0,64))+'</span>'+
      '<span class="age">P'+esc(e.priority)+' \\u00b7 '+esc(Math.round(e.age_days))+'d</span></div>';});
  $("urgentList").innerHTML=uz||'<div class="note">nothing urgent sits unclaimed \\u2014 priority holds</div>';

  var p=d.prune||{count:0,items:[]};
  $("pruneStamp").textContent=p.count+" TO PRUNE";
  var pz=(p.items||[]).slice(0,5).map(function(e){
    return '<a href="'+deskOpen(e.id)+'" class="mono">'+esc(e.id)+'</a> ('+
      esc(Math.round(e.quiet_days))+'d quiet)';}).join(" \\u00b7 ");
  $("pruneNote").innerHTML=p.count
    ? "quiet past "+esc(Math.round(d.prune_quiet_hours/24))+"d: "+pz+
      ' \\u2014 cancel, demote, or re-label for a lane'
    : "nothing is being carried silently";
}
function poll(){
  fetch("/api/report",{cache:"no-store"}).then(function(r){
    if(!r.ok)throw 0; return r.json();
  }).then(function(d){R=d; render();
    $("liveChip").className=""; $("liveChip").textContent="LIVE";
  }).catch(function(){
    $("liveChip").className="hold"; $("liveChip").textContent=R?"HOLDING":"NO SIGNAL";});}
poll(); setInterval(poll,30000);
setInterval(function(){var n=new Date();
  $("clock").textContent=n.toLocaleTimeString([],{hour:"2-digit",minute:"2-digit",second:"2-digit"});
},1000);
</script>
"""


# Non-f-string: empty {{}} would break an f-string (port-return room-back).
_ROOM_BACK_JS = """
<script>
/* Port/path ownership: Overview is Desk D1 (:8799). If Office sent us
   (?from=office&return=…), room-back re-enters Office. Browser Back also
   works when history has the foyer. Never invent a fourth suite peer. */
(function(){
  try {
    var p = new URLSearchParams(location.search||"");
    var back = document.getElementById("roomBack");
    if(!back) return;
    var ret = p.get("return") || "";
    var from = (p.get("from")||"").toLowerCase();
    var officeOk = /^https?:\\/\\/127\\.0\\.0\\.1:8796\\/?$/.test(ret)
      || /^https?:\\/\\/localhost:8796\\/?$/.test(ret);
    if(from==="office" && ret && officeOk){
      back.href = ret; back.textContent = "\\u2190 Office";
    } else if(document.referrer && /:8796(?:\\/|$)/.test(document.referrer)){
      back.href = "http://127.0.0.1:8796/"; back.textContent = "\\u2190 Office";
    }
  } catch(e){}
})();
</script>
"""


def _render_report_page() -> str:
    """Desk D1 Overview — strategic facts sheet (wl-156 / pc-183).
    Suite daylight chrome (pc-162); deep links land on the living Desk room
    (?cabinet= / ?open=), never the Board table as primary."""
    if _BRAND_MODE == "city":
        h1 = "OVERVIEW <span class='fn'>· Desk</span>"
        epithet = "ProtocolCity · powered by WorkLane · strategic facts"
    else:
        h1 = "OVERVIEW <span class='fn'>· WorkLane</span>"
        epithet = "the strategic view of your work orders"
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Overview · {_esc(_BRAND_NAME)}</title>
<style>{_REPORT_CSS}</style>
<script>
(function(){{
  var K='protocolcity-theme';
  try{{
    var t=localStorage.getItem(K)||localStorage.getItem('wl-theme')||'light';
    if(t!=='dark'&&t!=='light')t='light';
    document.documentElement.setAttribute('data-theme',t);
  }}catch(e){{ document.documentElement.setAttribute('data-theme','light'); }}
}})();
</script>
</head><body>
<header class="nameplate">
  <a class="room-back" id="roomBack" href="/admin/desk">← Desk</a>
  <div>
    <h1>{h1}</h1>
    <div class="epithet">{_esc(epithet)}</div>
  </div>
  <div class="badges"><span id="clock">—:—:—</span><span id="liveChip" class="hold">NO SIGNAL</span></div>
</header>
{_ROOM_BACK_JS}
<main class="sheet">
  <div class="kpis" aria-label="City pulse">
    <div class="kpi"><div class="k">Open</div><div class="v" id="kpiOpen">—</div>
      <div class="s">across all ledgers</div></div>
    <div class="kpi"><div class="k">Waiting on you</div><div class="v" id="kpiYou">—</div>
      <div class="s">founder-gated</div></div>
    <div class="kpi"><div class="k">Worker-ready</div><div class="v" id="kpiReady">—</div>
      <div class="s">lanes can claim</div></div>
    <div class="kpi"><div class="k">Window</div><div class="v" id="kpiWin">—</div>
      <div class="s">filed vs signed</div></div>
  </div>
  <h2>Verdicts · one word per ledger</h2>
  <div class="verdicts" id="verdictStrip"><div class="note">pulling the morning figures…</div></div>
  <div class="grid2" style="margin-top:6px">
    <div>
      <h2>Flow · filed vs signed off, last <span id="w1">7</span> days</h2>
      <div class="card"><div class="rows" id="flowRows"></div>
        <div class="note">top bar = filed in · green = signed off · the gap is backlog growth</div></div>
      <h2>Who's the blocker</h2>
      <div class="card"><div class="split" id="splitBar"></div>
        <div class="legend" id="splitLegend"></div></div>
      <h2>Aging rack · filed work orders by age</h2>
      <div class="card"><div class="rows" id="agingRows"></div>
        <div class="note" id="agingNote"></div></div>
    </div>
    <div>
      <h2>Priority integrity · urgent, filed, unclaimed</h2>
      <div class="card" id="urgentList"></div>
      <h2>Prune list · carried but untouched</h2>
      <div class="card"><span class="stamp-count" id="pruneStamp">—</span>
        <div class="note" id="pruneNote"></div></div>
    </div>
  </div>
</main>
<footer class="bar">
  <div>Desk Overview —
    <a href="/admin/desk">← back to Desk</a>
    <a href="/admin/settings">Settings</a>
    <a class="quiet" href="/admin/tickets/all">Board (power)</a>
    <a class="quiet" href="/admin/docs/desk">How to read this room</a></div>
  <div><span class="dim">facts: <a href="/api/report">/api/report</a></span></div>
</footer>
{_REPORT_JS}</body></html>"""


# ── App factory ────────────────────────────────────────────────────────

def create_app():
    """Build the standalone task-board FastAPI app."""
    from fastapi import FastAPI, Request
    from fastapi.staticfiles import StaticFiles

    app = FastAPI(
        title="WorkLane",
        description="WorkLane — standalone local-first ticketing service.",
        docs_url="/api/docs",
        redoc_url=None,
    )
    # ONE DOOR: default API-only — citizen UI is suite :8801/desk.
    # Opt out for host debug: WORKLANE_API_ONLY=0 (or WL_API_ONLY=0).
    _api_raw = (
        os.environ.get("WORKLANE_API_ONLY")
        or os.environ.get("WL_API_ONLY")
        or "1"
    ).strip().lower()
    api_only = _api_raw not in ("0", "false", "no", "off")
    suite_url = (os.environ.get("SUITE_URL") or "http://127.0.0.1:8801").rstrip("/")

    if api_only:

        @app.middleware("http")
        async def _api_only_gate(request: Request, call_next):  # type: ignore[no-untyped-def]
            path = request.url.path or "/"
            # Keep APIs, OpenAPI, and static assets for API clients.
            if (
                path.startswith("/api/")
                or path.startswith("/static/")
                or path in ("/openapi.json", "/docs", "/redoc", "/health")
            ):
                return await call_next(request)
            accept = (request.headers.get("accept") or "").lower()
            suite = suite_url + "/desk"
            if "application/json" in accept and "text/html" not in accept:
                return JSONResponse(
                    {
                        "ok": False,
                        "error": "worklane HTML retired (WORKLANE_API_ONLY); "
                        "open suite at %s" % suite,
                        "api": "/api/scene",
                        "suite": suite,
                    },
                    status_code=404,
                )
            # Soft land: small HTML pointer (not a maintained product page).
            body = (
                "<!doctype html><meta charset='utf-8'>"
                "<title>WorkLane API</title>"
                "<p>WorkLane HTML is retired — open the suite: "
                f"<a href='{suite}'>{suite}</a></p>"
                "<p>This port serves <code>/api/*</code> only. "
                "Set <code>WORKLANE_API_ONLY=0</code> for legacy desk HTML.</p>"
            )
            return HTMLResponse(body, status_code=200)

    app.include_router(router)
    # Self-hosted IBM Plex (wl-37) — no CDN, must render identically offline.
    static_dir = Path(__file__).resolve().parent / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("TASK_HOST", "127.0.0.1")
    port = int(os.environ.get("TASK_PORT", "8799"))
    print(f"Starting Ticketing on http://{host}:{port} ...")
    uvicorn.run(app, host=host, port=port)
