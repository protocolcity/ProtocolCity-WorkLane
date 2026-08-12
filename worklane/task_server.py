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

import asyncio
import json
import logging
import os
import re
import sqlite3

# :mod:`worklane.trackers.sqlite` mirrors to Ops Cockpit HTTP; when this
# server binds the same port as ``TASK_PORT``, suppress self-POST.
os.environ.setdefault("TRADEOS_SKIP_OPS_MIRROR", "1")
import sys
import urllib.request
from urllib.parse import quote, urlencode
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Ensure project root is on sys.path so `core.*` imports resolve.
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

from worklane.devqueue import (
    BlockedTask,
    WorkQueue,
    build_dispatch_prompt,
    group_by_file_conflict,
    run_shutdown,
)
from worklane.trackers import (
    Task,
    TaskComment,
    TaskStatus,
    get_default_tracker,
    task_is_gated,
)
from worklane import archival
from worklane.products import (
    ProductSpec,
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
from worklane.rendering import _badge, _css, _esc, _label_chip, render_markdown

from worklane.board import (
    _board_styles,
    _BOARD_COLUMNS,
    _claim_stale_minutes,
    _client_js,
    _detect_owner,
    _label_tier,
    _OWNER_LINE_RE,
    get_ops_ticket_tracker,
    _load_preview_comments,
    _load_preview_comments_multi,
    list_tasks_for_wq_multi,
    list_tasks_for_scope_multi,
    tickets_app_path,
    TASK_ID_PREFIX_OPS,
    TASK_ID_PREFIX_TRADEOS,
    parse_wq_priority,
    parse_wq_product,
    resolve_wq_product,
    product_scope_from_list_path,
    _PRIORITY_LABELS,
    _PRIORITY_TIERS,
    _render_comments,
    _render_labels,
    _scoped_labels,
    _render_priority_badge,
    _render_status_badge,
    _render_task_board,
    _render_task_card,
    _render_work_queue_filters,
    _owner_claim_html,
    _parse_iso_ts,
    _STATUS_LABELS,
    _STATUS_TIERS,
    TICKETS_APP_ALL,
    TICKETS_APP_OPS,
    TICKETS_APP_TRADEOS,
    _WORK_QUEUE_PATH,
    _wq_query_for_view,
    _wq_column_counts,
    _wq_status_counts,
    ops_tickets_db_path,
)

# Service start time — recorded at module load, used by the service-health pane (#485).
_SERVER_START: datetime = datetime.now(timezone.utc)

# Path to extracted surface assets (wl-222).
_SURFACES_DESK = Path(__file__).parent / "surfaces" / "desk"

# Canonical ticket store label (wl-90: Board and Table are sibling top-level
# views in the header; this names the store itself in card copy).
_TICKETS_SYSTEM_LABEL = "Tickets"
_OPS_TASK_LIST_PATH = TICKETS_APP_ALL

# UI quick-create removed 2026-07-10 (wl-26): tickets are filed by agents
# via the API/CLI/MCP with signed intake — not from the board chrome.
_OPS_WORKSPACE_OPEN = (
    "<section class='ops-workspace' data-ops-region='workspace' "
    "aria-label='Scope, filters, and detail'>"
)
_OPS_WORKSPACE_CLOSE = "</section>"
# Page tools + workspace = one “reading sheet” (see ui-chrome.md).
_OPS_READING_SHEET_OPEN = "<div class='ops-reading-sheet' data-ops-region='reading-sheet'>"
_OPS_READING_SHEET_CLOSE = "</div>"


def _render_tickets_context_strip() -> str:
    """Notification module: three human-readable status pills (see ui-chrome.md, wl-28)."""
    return (
        '<div class="ops-context-strip ops-notification-module" id="ops-context-strip" '
        'data-ops-region="notification-module" role="region" '
        'aria-label="Queue status">'
        '<div class="ops-context-strip-inner">'
        '<div class="ops-context-badges">'
        '<span id="ts-ready-badge" class="ts-ready-badge" hidden '
        'title="Backlog tickets with no unresolved blockers."></span>'
        '<span id="ts-inflight-badge" class="ts-inflight-badge" hidden '
        'title="Tickets actively in progress or in review."></span>'
        '<span id="ts-stalled-badge" class="ts-stalled-badge" hidden '
        'title="In-progress/in-review tickets with no update in over 90 minutes (PROTOCOL.md §4)."></span>'
        '<span id="ts-last-updated" class="ts-last-updated dim"></span>'
        "</div>"
        "</div></div>"
    )


# UI region vocabulary: docs/operations/ui-chrome.md (data-ops-region in HTML).

# ── Dashboard branding (pc-24 / wl-134, 2026-07-14) ─────────────────────
# In a founded city the Tickets dashboard fronts the suite brand
# ("ProtocolCity — Tickets", engine attributed as a subtitle); a standalone
# WorkLane install fronts the engine brand ("WorkLane — Tickets").
# WL_BRAND=city|standalone selects the mode (WL_BRAND is a back-compat alias).
# This internal checkout IS the city instance, so the default here is "city";
# the WorkLane public export must default to "standalone" (wl-134).
_BRAND_MODE = os.environ.get("WL_BRAND") or os.environ.get("WL_BRAND", "city")
# Sixth naming amendment (founder, 2026-07-15): city D0 mast is
# "[Folder] Desk" (parity with Office / Roster). Tab <title> keeps the
# suite · function form. Standalone stays engine-branded.
_BRAND_NAME = (
    "ProtocolCity — Desk · Tickets" if _BRAND_MODE == "city" else "WorkLane — Tickets"
)
# Third naming amendment (pc-39, 2026-07-14): the long epithet sentence
# retired from headers — the room guide (docs/TICKET_DESK.md) holds the
# words. Bench chrome mirrors the nameplate: room name leads, suite +
# engine share one quiet subtitle; standalone keeps the short epithet.
_BRAND_SUBTITLE = (
    "ProtocolCity · powered by WorkLane" if _BRAND_MODE == "city"
    else "the work-order desk"
)
_BRAND_HEADER_HTML = (
    "<span class='brand-room'>DESK</span>" if _BRAND_MODE == "city"
    else "WORKLANE — <span class='brand-room'>TICKETS</span>"
)

# ── Lightweight page wrapper ────────────────────────────────────────────
# Mirrors the design tokens and card/badge CSS from the main app but skips
# the nav bar, SSE bus, CSRF middleware, setup banner, and everything else
# that ties _page() to the full tradeOS stack.


def _split_for_middle_truncate(display: str, tail_len: int = 12) -> Tuple[str, str]:
    """Split a scope display name into a shrinkable head and an always-visible tail
    so long names truncate in the middle (wl-117 design req 2) instead of losing the
    public-facing half — the internal→public arrow convention (wl-113/wl-115, e.g.
    "WorkLane → WorkLane") puts the interesting part at the end."""
    s = display or ""
    if len(s) <= tail_len:
        return s, ""
    arrow = " → "
    idx = s.rfind(arrow)
    if idx != -1 and len(s) - idx <= tail_len + 8:
        return s[:idx], s[idx:]
    return s[:-tail_len], s[-tail_len:]


def _seg_label_html(display: str) -> str:
    """Render a scope pill's label as a head/tail split that CSS truncates in the
    middle (min-width:0 + ellipsis on the head, fixed-width tail) — no JS text
    measurement needed."""
    head, tail = _split_for_middle_truncate(display)
    if not tail:
        return f"<span class='ts-seg-label'><span class='ts-seg-head'>{_esc(head)}</span></span>"
    return (
        "<span class='ts-seg-label'>"
        f"<span class='ts-seg-head'>{_esc(head)}</span>"
        f"<span class='ts-seg-tail'>{_esc(tail)}</span>"
        "</span>"
    )


# Pills shown inline before the rest collapse into "More" — covers today's 6 stores
# (All + 6) with room to spare; scales to ~20 without the row growing unbounded
# (wl-117 design req 1, 4). Tune here if the steady-state store count grows.
_SCOPE_NAV_MAX_INLINE = 6


def _render_scope_nav(
    items: List[Tuple[str, str, bool, str, str]],
) -> str:
    """Render a project-scope switcher from ``(href, display, is_active, title, slug)``
    tuples (items[0] is always "All", slug "all"). Beyond :data:`_SCOPE_NAV_MAX_INLINE`
    pills the rest collapse into a keyboard/click-accessible "More" disclosure so the
    row stays bounded at any store count instead of overflowing or endlessly scrolling
    (wl-117 — the flat tab row broke past ~6 stores, had no answer for 20). Each pill
    carries a hidden count badge (``data-scope-badge``) that tsFetchScopeNavCounts()
    populates client-side from the batch counts endpoint (wl-120)."""
    inline, overflow = items[: _SCOPE_NAV_MAX_INLINE + 1], items[_SCOPE_NAV_MAX_INLINE + 1 :]

    def _badge(slug: str) -> str:
        return f"<span class='ts-seg-badge' data-scope-badge='{_esc(slug)}' hidden></span>"

    def _pill(href: str, display: str, on: bool, title: str, slug: str) -> str:
        cls = "ts-seg ts-seg--on" if on else "ts-seg"
        ac = ' aria-current="page"' if on else ""
        return (
            f"<a href='{_esc(href)}' class='{cls}' title='{_esc(title)}'{ac}>"
            f"{_seg_label_html(display)}{_badge(slug)}</a>"
        )

    html = ["".join(_pill(h, d, on, t, s) for h, d, on, t, s in inline)]
    if overflow:
        active = next((it for it in overflow if it[2]), None)
        summary_cls = "ts-seg ts-seg-more" + (" ts-seg--on" if active else "")
        summary_label = _seg_label_html(active[1]) if active else "<span class='ts-seg-label'><span class='ts-seg-head'>More</span></span>"
        rows = "".join(
            f"<a href='{_esc(h)}' class='ts-seg-more-item{' ts-seg-more-item--on' if on else ''}' "
            f"title='{_esc(t)}'>{_esc(d)}{_badge(s)}</a>"
            for h, d, on, t, s in overflow
        )
        html.append(
            f"<details class='ts-seg-more-wrap'><summary class='{summary_cls}'>"
            f"{summary_label}<span class='ts-seg-more-caret'>&#9662;</span></summary>"
            f"<div class='ts-seg-more-menu' role='menu'>{rows}</div></details>"
        )
    return (
        '<nav class="ts-tickets-surface-nav" aria-label="Project scopes">'
        '<div class="ts-segmented ts-segmented--tools" role="tablist">'
        + "".join(html)
        + "</div></nav>"
    )


def _render_tickets_surface_nav(
    current_path: str,
    view: str,
    status: str,
    label: str,
    priority: Optional[int],
) -> str:
    """Scope switcher for Ticketing surfaces (All + every discovered project store)."""
    cur = (current_path or "").rstrip("/")

    def _href(dest: str) -> str:
        q = _wq_query_for_view(view, status, label, priority, list_path=dest)
        return f"{dest}?{q}"

    items: List[Tuple[str, str, bool, str, str]] = [
        (
            _href(TICKETS_APP_ALL),
            "All",
            cur == TICKETS_APP_ALL.rstrip("/"),
            "Every work order across all project stores",
            "all",
        )
    ]
    for spec in discover_products():
        dest = f"/admin/tickets/{spec.slug}"
        items.append(
            (
                _href(dest),
                spec.display,
                cur == dest.rstrip("/"),
                f"{spec.display} work orders ({spec.db_path.name})",
                spec.slug,
            )
        )
    return _render_scope_nav(items)


def _render_overview_scope_nav(scope: str) -> str:
    """Scope switcher for the Overview landing — same scopes as the Board/Table
    (All + every discovered project store), each filtering the whole page."""
    items: List[Tuple[str, str, bool, str, str]] = [
        (
            "/admin/overview/all",
            "All",
            (scope or "") == "all" or not scope,
            "Every work order across all project stores",
            "all",
        )
    ]
    for spec in discover_products():
        items.append(
            (
                f"/admin/overview/{spec.slug}",
                spec.display,
                (scope or "") == spec.slug,
                f"Overview for the {spec.display} project ({spec.db_path.name})",
                spec.slug,
            )
        )
    return _render_scope_nav(items)


def _ticket_create_surface_from_scope(scope: str) -> str:
    """Where new tasks go from the quick-add / create form for this Tickets tab."""
    s = (scope or "").strip().lower()
    if s and get_product(s) is not None:
        return s
    return default_product_slug()


def _tickets_shell_kwargs(
    *,
    product: str,
    scope_path: str,
    view: str,
    status: str,
    label: str,
    priority: Optional[int],
) -> Dict[str, Any]:
    """Keyword args for :func:`_task_page` on ticket pages (Work Queue)."""
    return {
        "page_scope": product or "",
        "tickets_product": product or "",
        "tickets_scope_path": scope_path,
        "tickets_view": view,
        "tickets_status": status or "",
        "tickets_label": label or "",
        "tickets_priority": priority,
        "tickets_create_surface": _ticket_create_surface_from_scope(product or ""),
    }


def _tickets_product_from_labels(labels: Optional[List[str]]) -> str:
    """Prefer ``product:*`` on the task when choosing a default scope for the Tickets shell."""
    for lb in labels or []:
        s = (lb or "").strip().lower()
        if s.startswith("product:") and get_product(s[len("product:"):]):
            return s[len("product:"):]
    return ""


def _tickets_path_for_scope_key(scope_key: str) -> str:
    # Retired surfaces (ops) and unknown scopes land on All.
    return tickets_app_path(scope_key)


def _task_page(
    title: str,
    body: str,
    *,
    nav_active: str = "",
    shell: str = "overview",
    page_scope: str = "",
    product_tab: str = "",
    tickets_product: str = "",
    tickets_scope_path: str = "",
    tickets_view: str = "",
    tickets_status: str = "",
    tickets_label: str = "",
    tickets_priority: Optional[int] = None,
    tickets_create_surface: str = "",
) -> str:
    """Render the Ticketing shell for the Pool (work queue) app."""
    _seg = lambda on: "ts-seg ts-seg--on" if on else "ts-seg"
    _is_landing = shell == "overview" and nav_active == "overview"
    _brand_cls = "task-server-brand active" if _is_landing else "task-server-brand"
    _show_ticket_tools = shell == "tickets"
    _ticket_tools = ""
    _product_scope = ""
    if _show_ticket_tools and tickets_scope_path:
        _product_scope = _render_tickets_surface_nav(
            tickets_scope_path,
            tickets_view or "board",
            tickets_status,
            tickets_label,
            tickets_priority,
        )
    elif shell == "overview" and nav_active == "overview":
        _product_scope = _render_overview_scope_nav(page_scope)
    # Status badges: Overview header only. Tickets shell uses per-page context strips.
    _ticket_header_widgets = ""
    if shell == "overview":
        _ticket_header_widgets = (
            """        <span id="ts-ready-badge" class="ts-ready-badge" hidden """
            """title="Backlog tickets with no unresolved blockers."></span>
        <span id="ts-inflight-badge" class="ts-inflight-badge" hidden
              title="Tickets actively in progress or in review."></span>
        <span id="ts-stalled-badge" class="ts-stalled-badge" hidden
              title="In-progress/in-review tickets with no update in over 90 minutes (PROTOCOL.md §4)."></span>
        <span id="ts-attention-badge" class="ts-attention-badge" hidden
              title="Everything waiting on You, across every store: in review, needs-your-decision, human-gated, stalled, embargoed."></span>
        <span id="ts-last-updated" class="ts-last-updated dim"></span>
"""
        )
    # Product tabs live inline in the primary header row (wl-36) — the
    # subnav band is gone; one row of chrome instead of two.
    _subnav_html = ""
    _port = os.environ.get("TASK_PORT", "8799")
    # wl-128 / pc-160: suite peer doors belong on D0 (/admin/desk) only.
    # Board/ops shell is D1 furniture — never a partial Dispatch link here.
    _workforce_url = ""
    # Suite dark/light toggle on all brands (Office · Desk · Roster share
    # protocolcity-theme). Standalone WorkLane keeps the same control.
    _show_theme_toggle = True
    # wl-184 / pc-177: port numeral + WL badge are anti-pattern #5 on citizen
    # chrome — keep them for standalone WorkLane installs only.
    _show_service_chrome = _BRAND_MODE != "city"
    _view_scope_path = tickets_scope_path or TICKETS_APP_ALL
    if shell == "tickets":
        _board_href = f"{_view_scope_path}?" + _wq_query_for_view(
            "board", tickets_status, tickets_label, tickets_priority,
            product=tickets_product, list_path=_view_scope_path,
        )
        _table_href = f"{_view_scope_path}?" + _wq_query_for_view(
            "table", tickets_status, tickets_label, tickets_priority,
            product=tickets_product, list_path=_view_scope_path,
        )
    else:
        _board_href = f"{_view_scope_path}?view=board"
        _table_href = f"{_view_scope_path}?view=table"
    _table_on = shell == "tickets" and (tickets_view or "board") == "table"
    _board_on = shell == "tickets" and not _table_on
    _board_cur = ' aria-current="page"' if _board_on else ""
    _table_cur = ' aria-current="page"' if _table_on else ""
    _port_html = (
        f'<span class="task-server-hint dim">port {_esc(_port)}</span>'
        if _show_service_chrome else ""
    )
    _wl_badge_html = (
        '<span class="ts-dev-badge">WL</span>' if _show_service_chrome else ""
    )
    # No app footer: it was fixed-position chrome duplicating the header
    # (brand · Cockpit · port) and overlapped page content (wl-25).
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{_BRAND_NAME if _is_landing else f"{_esc(title)} · {_BRAND_NAME}"}</title>
  <style>{_css()}</style>
  <script>
  /* Theme init (before paint). Suite key protocolcity-theme; wl-theme kept
     in sync for legacy Desk D1 pages. light|dark only (binary toggle). */
  (function() {{
    /* wl-84: key renamed from 'tradeos-theme' — migrate old prefs once. */
    var legacy = localStorage.getItem('tradeos-theme');
    if (legacy && !localStorage.getItem('wl-theme') && !localStorage.getItem('protocolcity-theme')) {{
      localStorage.setItem('wl-theme', legacy === 'dark' ? 'dark' : 'light');
      localStorage.removeItem('tradeos-theme');
    }}
    var suite = localStorage.getItem('protocolcity-theme');
    var stored = suite || localStorage.getItem('wl-theme') || 'light';
    if (stored === 'system') {{
      stored = window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
    }}
    if (stored !== 'dark' && stored !== 'light') stored = 'light';
    try {{
      localStorage.setItem('protocolcity-theme', stored);
      localStorage.setItem('wl-theme', stored);
    }} catch (e) {{}}
    document.documentElement.setAttribute('data-theme', stored);
  }})();

  /* Toast system */
  function showToast(msg, type, duration) {{
    type = type || 'success';
    duration = duration || 2500;
    if (!window._toastContainer) {{
      var c = document.createElement('div');
      c.className = 'toast-container';
      document.body.appendChild(c);
      window._toastContainer = c;
    }}
    var t = document.createElement('div');
    t.className = 'toast ' + type;
    t.textContent = msg;
    window._toastContainer.appendChild(t);
    setTimeout(function() {{ t.classList.add('out'); }}, duration);
    setTimeout(function() {{ t.remove(); }}, duration + 350);
  }}
  </script>
</head>
<body data-ops-shell="{_esc(shell)}" data-ops-scope="{_esc(page_scope)}" data-perimeter="d1">
  <header class="task-server-header task-server-header--stack">
    <div class="task-server-header-primary ops-main-nav" data-ops-region="main-nav">
      <a href="/admin/desk" class="ts-back-desk" title="← Desk — room home">← Desk</a>
      <a href="/admin/desk" class="{_brand_cls}" title="The desk — the room you walk into">{_BRAND_HEADER_HTML}</a>{f'<span class="task-server-hint dim">{_BRAND_SUBTITLE}</span>' if _BRAND_SUBTITLE else ''}
      <nav class="ts-primary-shell ts-segmented" aria-label="Desk benches">
        <a href="/admin/overview/{_esc(page_scope or 'all')}" class="{_seg(shell == 'overview' and nav_active == 'overview')}"
           title="Overview — strategic view of the backlog (bench of the desk room)"{' aria-current="page"' if (shell == 'overview' and nav_active == 'overview') else ''}>Overview</a>
        <a href="{_board_href}" class="{_seg(_board_on)}"
           title="Power board — cards by status column (bench of the desk room)"{_board_cur}>Board</a>
        <a href="{_table_href}" class="{_seg(_table_on)}"
           title="Power table — dense timetable rows (bench of the desk room)"{_table_cur}>Table</a>
      </nav>
{_product_scope}
      <div class="task-server-header-end">
{_ticket_header_widgets}
        {_port_html}
        <a href="/admin/docs" title="Docs — PROCESS/ARCHITECTURE/README + per-agent instruction files rendered in-app"
           style="text-decoration:none; color:{'var(--text)' if nav_active == 'docs' else 'var(--dim)'}; font-size:16px; padding:4px 6px;">&#128220;</a>
        <a href="/admin/settings" title="Settings — projects, prefixes, numbering, service"
           style="text-decoration:none; color:{'var(--text)' if nav_active == 'settings' else 'var(--dim)'}; font-size:16px; padding:4px 6px;">&#9881;</a>
        {_wl_badge_html}{f'''
        <button id="theme-toggle" onclick="cycleTheme()" title="Toggle theme"
                style="background:none;border:0;color:var(--dim);cursor:pointer;font-size:16px;padding:4px 8px;">&#9789;</button>''' if _show_theme_toggle else ''}
      </div>
    </div>
{_subnav_html}
  </header>
  <div class="page page-full ts-ops-page">
    {body}
  </div>
  <script>
  /* Binary suite toggle — moon means "go dark", sun means "go light". */
  function cycleTheme() {{
    var cur = localStorage.getItem('protocolcity-theme')
           || localStorage.getItem('wl-theme') || 'light';
    if (cur === 'system') {{
      cur = window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
    }}
    var next = (cur === 'dark') ? 'light' : 'dark';
    try {{
      localStorage.setItem('protocolcity-theme', next);
      localStorage.setItem('wl-theme', next);
    }} catch (e) {{}}
    document.documentElement.setAttribute('data-theme', next);
    var btn = document.getElementById('theme-toggle');
    if (btn) {{
      btn.textContent = next === 'dark' ? '\\u2600' : '\\u263D';
      btn.title = next === 'dark' ? 'Switch to light theme' : 'Switch to dark theme';
    }}
  }}
  (function() {{
    var pref = localStorage.getItem('protocolcity-theme')
            || localStorage.getItem('wl-theme') || 'light';
    if (pref === 'system') {{
      pref = window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
    }}
    if (pref !== 'dark' && pref !== 'light') pref = 'light';
    var btn = document.getElementById('theme-toggle');
    if (btn) {{
      btn.textContent = pref === 'dark' ? '\\u2600' : '\\u263D';
      btn.title = pref === 'dark' ? 'Switch to light theme' : 'Switch to dark theme';
    }}
  }})();
  </script>
  <style>
  /* Shell: column layout; the page area is the single scroll surface. */
  body[data-ops-shell] {{
    min-height: 100vh;
    display: flex;
    flex-direction: column;
    margin: 0;
    font-family: var(--font-sans, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif);
  }}
  body[data-ops-shell] > .page.page-full.ts-ops-page {{
    flex: 1 1 auto;
    min-height: 0;
    overflow-y: auto;
    -webkit-overflow-scrolling: touch;
  }}
  /* Main nav + sub nav = one chrome block (toolbar, not stacked cards). */
  .ts-back-desk {{
    flex:none; font-size:12px; font-weight:700; letter-spacing:.04em;
    color:var(--dim); text-decoration:none; padding:4px 8px; margin-right:4px;
    border:1px solid transparent; border-radius:8px; white-space:nowrap;
  }}
  .ts-back-desk:hover {{ color:var(--text); border-color:var(--line, #c4b8a4); }}
  .task-server-header.task-server-header--stack {{
    display: flex;
    flex-direction: column;
    padding: 0;
    position: sticky;
    top: 0;
    z-index: 100;
    border-bottom: 1px solid color-mix(in srgb, var(--border) 88%, transparent);
    background: color-mix(in srgb, var(--bg2) 96%, rgba(0, 0, 0, 0.03));
    box-shadow: 0 1px 0 rgba(0,0,0,.04);
  }}
  [data-theme="light"] .task-server-header.task-server-header--stack {{
    background: color-mix(in srgb, var(--bg2) 99%, rgba(0, 0, 0, 0.02));
  }}
  .task-server-header-primary {{
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 8px 12px;
    padding: 4px clamp(12px, 3vw, 20px);
    border-top: none;
    background: transparent;
    box-shadow: none;
  }}
  /* Product tabs inline in the primary row (wl-36). min-width:0 overrides the
     flex-item content-based default so the pill row can shrink and scroll
     instead of forcing the header (and page) wider than the viewport as
     more project stores get discovered (wl-111). */
  .task-server-header-primary .ts-tickets-surface-nav {{
    margin: 0;
    min-width: 0;
    max-width: 100%;
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
  }}
  .task-server-header-primary .ts-segmented--tools {{
    padding: 2px;
    border-radius: 8px;
  }}
  .task-server-header-primary .ts-segmented--tools .ts-seg {{
    min-height: 26px !important;
    padding: 2px 10px !important;
    font-size: 12px !important;
    font-weight: 500;
  }}
  [data-theme="light"] .task-server-header-primary {{
    background: transparent;
  }}
  .task-server-header-end {{
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 8px 12px;
    margin-left: auto;
  }}
  .task-server-subnav {{
    display: flex;
    justify-content: center;
    align-items: center;
    flex-wrap: wrap;
    padding: 3px clamp(12px, 3vw, 20px) 5px;
    border-top: none;
    background: transparent;
  }}
  [data-theme="light"] .task-server-subnav {{
    background: transparent;
  }}
  .task-server-subnav-inner {{
    display: inline-flex;
    align-items: center;
    justify-content: center;
    flex-wrap: wrap;
    gap: 8px 10px;
    max-width: 100%;
  }}
  /* Sub-nav segments smaller than main nav (secondary tier). */
  .task-server-subnav .ts-segmented--tools {{
    padding: 2px;
    border-radius: 8px;
  }}
  .task-server-subnav .ts-segmented--tools .ts-seg {{
    min-height: 28px !important;
    padding: 3px 11px !important;
    font-size: 13px !important;
    font-weight: 500;
  }}
  .task-server-subnav .ts-tickets-nav,
  .task-server-subnav .ts-product-nav,
  .task-server-subnav .ts-tickets-surface-nav {{
    margin: 0;
  }}
  .task-server-tickets-subnav {{
    flex-direction: column;
    align-items: center;
    gap: 6px;
  }}
  .ts-product-scope {{
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 6px 10px;
    justify-content: center;
  }}
  .ts-product-scope-h {{
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    margin-right: 4px;
  }}
  a.ts-product-scope-pill {{
    font-size: 12px;
    padding: 4px 12px;
    border-radius: 999px;
    border: 1px solid var(--border);
    color: var(--muted);
    text-decoration: none;
    background: var(--bg2);
  }}
  a.ts-product-scope-pill:hover {{
    color: var(--fg);
    border-color: var(--neon);
  }}
  .ts-product-scope-pill--on {{
    color: var(--neon);
    border-color: color-mix(in srgb, var(--neon) 35%, transparent);
    background: color-mix(in srgb, var(--neon) 8%, transparent);
    font-weight: 600;
  }}
  /* Notification module — own “card”, equal gap below header (uses --ops-module-gap). */
  /* Status strip (wl-28): three human pills — ready / in flight / stalled. No feed. */
  .ops-context-strip {{
    margin: 4px 0 0;
    padding: 0 clamp(12px, 3vw, 24px);
    border: 0;
    background: transparent;
  }}
  .ops-context-strip-inner {{
    display: flex;
    align-items: center;
    justify-content: flex-start;
    gap: 10px;
    flex-wrap: nowrap;
    padding: 2px 12px;
    border-radius: 8px;
    border: 1px solid var(--ops-paper-edge, color-mix(in srgb, var(--border) 82%, transparent));
    background: var(--ops-paper, var(--bg2));
    box-shadow: 0 1px 2px rgba(0,0,0,.05);
    min-height: 26px;
    overflow: hidden;
  }}
  .ops-context-badges {{
    display: flex;
    align-items: center;
    flex-wrap: nowrap;
    gap: 8px;
    flex: 0 0 auto;
  }}
  /* Reading sheet: page tools + workspace = one paper surface. */
  .ts-wq-shell .ops-reading-sheet {{
    margin-top: var(--ops-module-gap, 12px);
    padding: 0;
    border-radius: 12px;
    border: 1px solid var(--ops-paper-edge, color-mix(in srgb, var(--border) 82%, transparent));
    background: var(--ops-paper, var(--bg2));
    box-shadow: 0 1px 3px rgba(0,0,0,.06);
    overflow: hidden;
  }}
  .ops-reading-sheet .ops-third-nav {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    flex-wrap: wrap;
    padding: 8px 12px;
    border-bottom: 1px solid color-mix(in srgb, var(--border) 90%, transparent);
    background: color-mix(in srgb, var(--bg2) 93%, rgba(255,255,255,.4));
  }}
  [data-theme="dark"] .ops-reading-sheet .ops-third-nav {{
    background: color-mix(in srgb, var(--bg2) 96%, #000 4%);
  }}
  .ops-third-nav-actions {{
    display: flex;
    align-items: center;
    gap: 8px;
    flex: 0 0 auto;
  }}
  .ops-third-nav-views {{
    display: flex;
    align-items: center;
    justify-content: flex-end;
    flex: 1 1 auto;
    min-width: 0;
    margin-left: auto;
  }}
  .ops-third-nav-views .tb-toolbar {{
    margin-bottom: 0;
  }}
  .ts-dev-badge {{
    font-size: 9px; font-weight: 700; letter-spacing: .1em;
    padding: 1px 6px; border-radius: var(--r-md, 4px);
    background: rgba(232,98,44,.12); color: var(--neon, #e8622c);
    border: 1px solid rgba(232,98,44,.25);
    vertical-align: middle;
  }}
  .task-server-qa-btn {{
    font-size: var(--text-badge, 12px);
    padding: 4px 12px;
  }}
  .ops-third-nav-actions .task-server-qa-btn {{
    flex-shrink: 0;
    white-space: nowrap;
  }}
  @media (max-width: 560px) {{
    .ops-third-nav {{ flex-direction: column; align-items: stretch; }}
    .ops-third-nav-views {{ margin-left: 0; justify-content: center; }}
    .ops-context-strip-inner {{ flex-direction: column; align-items: stretch; }}
  }}
  .task-server-backlink {{
    font-size: var(--text-body, 14px);
    color: var(--dim);
    text-decoration: none;
    padding: 2px 8px;
    border-radius: 4px;
  }}
  .task-server-backlink:hover {{
    background: var(--bg3, #2b261e);
    color: var(--neon);
  }}
  .task-server-sep {{
    color: var(--border);
    font-size: 14px;
    user-select: none;
  }}
  .task-server-brand {{
    display: inline-flex; align-items: center;
    font-size: var(--text-section); font-weight: 700;
    color: var(--fg); text-decoration: none;
    letter-spacing: .14em; text-transform: uppercase;
    border: 1.5px solid var(--fg); border-radius: 2px;
    padding: 3px 10px;
    white-space: pre;
  }}
  .task-server-brand .brand-room {{
    color: var(--stamp, #c0392b);
  }}
  .task-server-brand.active {{
    border-color: var(--neon);
    color: var(--neon);
  }}
  .task-server-nav {{
    font-size: var(--text-body, 14px);
    color: var(--dim);
    text-decoration: none;
    padding: 2px 8px;
    border-radius: 4px;
  }}
  .task-server-nav:hover {{
    background: var(--bg3, #2b261e);
    color: var(--fg, #eee);
  }}
  .task-server-nav.active {{
    color: var(--neon, #e8622c);
    font-weight: 600;
    background: color-mix(in srgb, var(--neon) 8%, transparent);
  }}
  /* Segmented controls — soft “pill” buttons, no heavy outer chrome */
  .ts-segmented {{
    display: inline-flex;
    align-items: stretch;
    gap: 2px;
    padding: 3px;
    border-radius: 999px;
    background: color-mix(in srgb, var(--bg) 88%, var(--border));
    box-shadow: inset 0 1px 0 rgba(255,255,255,.04), inset 0 -1px 0 rgba(0,0,0,.12);
    border: 1px solid color-mix(in srgb, var(--border) 70%, transparent);
  }}
  [data-theme="light"] .ts-segmented {{
    background: color-mix(in srgb, var(--bg2) 92%, #000 4%);
    box-shadow: inset 0 1px 2px rgba(0,0,0,.06);
    border-color: color-mix(in srgb, var(--border) 55%, transparent);
  }}
  .ts-segmented--tools {{
    border-radius: 10px;
  }}
  .ts-tickets-nav-active .ts-segmented--tools {{
    border-color: color-mix(in srgb, var(--border) 55%, transparent);
    box-shadow: none;
  }}
  .ts-seg {{
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-height: 36px;
    padding: 6px 14px;
    font-size: clamp(13px, 1.1vw, 14px);
    font-weight: 500;
    letter-spacing: .01em;
    text-decoration: none;
    color: var(--dim);
    border-radius: inherit;
    border: 0;
    background: transparent;
    transition: background .15s ease, color .15s ease, box-shadow .15s ease;
    white-space: nowrap;
  }}
  .ts-segmented--tools .ts-seg {{
    min-height: 40px;
    padding: 8px 14px;
  }}
  .ts-seg:hover {{
    color: var(--fg);
    background: color-mix(in srgb, var(--bg2) 70%, transparent);
  }}
  .ts-seg--on {{
    color: var(--fg);
    font-weight: 600;
    background: color-mix(in srgb, var(--bg2) 94%, var(--neon, #e8622c) 8%);
    box-shadow: 0 1px 2px rgba(0,0,0,.12);
  }}
  [data-theme="light"] .ts-seg--on {{
    background: #fff;
    box-shadow: 0 1px 3px rgba(0,0,0,.08);
    color: var(--fg);
  }}
  /* Primary nav (wl-37): active item also gets a signal-orange underline. */
  .ts-primary-shell .ts-seg--on {{
    border-bottom: 2px solid var(--neon);
  }}
  .ts-primary-shell {{
    margin-left: 4px;
  }}
  .ts-tickets-nav, .ts-product-nav {{
    display: inline-flex;
    align-items: center;
    gap: 8px;
  }}
  /* Project-scope switcher (wl-117): middle-truncating labels + a "More" overflow
     disclosure so the pill row stays bounded at any discovered-store count instead
     of overflowing or relying on horizontal scroll alone (wl-111 stopgap). */
  .ts-tickets-surface-nav .ts-seg-label {{
    display: flex;
    min-width: 0;
    max-width: 132px;
  }}
  .ts-tickets-surface-nav .ts-seg-head {{
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    min-width: 0;
    flex: 1 1 auto;
  }}
  .ts-tickets-surface-nav .ts-seg-tail {{
    flex: 0 0 auto;
    white-space: nowrap;
  }}
  .ts-seg-more-wrap {{
    position: relative;
    display: inline-flex;
  }}
  .ts-seg-more-wrap > summary.ts-seg {{
    cursor: pointer;
    list-style: none;
    gap: 4px;
  }}
  .ts-seg-more-wrap > summary.ts-seg::-webkit-details-marker {{
    display: none;
  }}
  .ts-seg-more-caret {{
    font-size: 10px;
    opacity: .7;
  }}
  .ts-seg-more-wrap[open] > summary.ts-seg-more {{
    color: var(--fg);
    background: color-mix(in srgb, var(--bg2) 70%, transparent);
  }}
  .ts-seg-more-menu {{
    position: absolute;
    top: calc(100% + 4px);
    right: 0;
    z-index: 30;
    display: flex;
    flex-direction: column;
    min-width: 200px;
    max-width: 320px;
    max-height: 60vh;
    overflow-y: auto;
    background: var(--card, #fff);
    border: 1px solid var(--border);
    border-radius: 8px;
    box-shadow: 0 4px 16px rgba(0, 0, 0, 0.18);
    padding: 4px;
  }}
  .ts-seg-more-item {{
    display: block;
    padding: 6px 10px;
    border-radius: 6px;
    font-size: 13px;
    color: var(--dim);
    text-decoration: none;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }}
  .ts-seg-more-item:hover {{
    color: var(--fg);
    background: color-mix(in srgb, var(--bg2) 70%, transparent);
  }}
  .ts-seg-more-item--on {{
    color: var(--fg);
    font-weight: 600;
    background: color-mix(in srgb, var(--bg2) 94%, var(--neon, #e8622c) 8%);
  }}
  /* Per-scope ready/stalled counts (wl-120): compact badges next to each
     pill and "More" row, populated client-side from the batch counts
     endpoint so the switcher stays a single request regardless of store count. */
  .ts-seg-badge {{
    display: inline-flex;
    align-items: center;
    gap: 3px;
    margin-left: 6px;
  }}
  .ts-seg-count {{
    font-size: 10px;
    font-weight: 700;
    line-height: 1;
    padding: 1px 5px;
    border-radius: 999px;
  }}
  .ts-seg-count--ready {{
    background: color-mix(in srgb, var(--neon) 12%, transparent);
    color: var(--neon, #e8622c);
  }}
  .ts-seg-count--stalled {{
    background: rgba(255,59,59,.15);
    color: var(--red, #ff3b3b);
  }}
  /* Main content — full-bleed fluid (wl-87): every page uses the real
     viewport, like the board always did. No static max-width clamp;
     line-length lives in content measures (.ts-doc-body etc.), not chrome.
     --ops-module-gap aligns notification / sheet / footer rhythm.
     margin:0, not auto — auto cross-axis margins defeat flex stretch and
     let nowrap ticker content inflate the page to its min-content width. */
  .ts-ops-page.page-full {{
    --ops-module-gap: 12px;
    --ops-paper: color-mix(in srgb, var(--bg2) 97%, #fff 3%);
    --ops-paper-edge: color-mix(in srgb, var(--border) 82%, transparent);
    --ops-footer-h: 48px;
    margin: 0;
    width: 100%;
    padding: var(--ops-module-gap) clamp(12px, 3vw, 24px)
      calc(var(--ops-module-gap) * 2 + var(--ops-footer-h));
    box-sizing: border-box;
  }}
  /* Board (wl-36): the board view tightens the module gap. */
  .ts-ops-page.page-full:has(.ts-wq-shell) {{
    --ops-module-gap: 4px;
    padding-top: 4px;
  }}
  .ts-ops-page.page-full:has(.ts-wq-shell) .ops-workspace {{
    padding-top: 0;
  }}
  [data-theme="dark"] .ts-ops-page.page-full {{
    --ops-paper: color-mix(in srgb, var(--bg2) 94%, #0a0c10 6%);
  }}
  .ts-wq-shell .ops-workspace {{
    padding: var(--ops-module-gap) var(--ops-module-gap) calc(var(--ops-module-gap) * 1.25);
  }}
  .ts-wq-shell .ops-workspace > .tos-card:first-child {{
    margin-top: 0;
  }}
  /* Dev Queue — KPI strip + batch cards */
  .devq-kpi {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
    gap: 10px;
    margin-bottom: 18px;
  }}
  .devq-kpi-tile {{
    border-radius: 10px;
    padding: 10px 12px;
    border: 1px solid color-mix(in srgb, var(--border) 80%, transparent);
    background: color-mix(in srgb, var(--bg2) 92%, transparent);
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-height: 56px;
    justify-content: center;
  }}
  .devq-kpi-tile--accent {{
    border-color: color-mix(in srgb, var(--neon, #e8622c) 35%, var(--border));
    background: color-mix(in srgb, var(--bg2) 88%, var(--clr-interactive-bg));
  }}
  .devq-kpi-tile--warn {{
    border-color: color-mix(in srgb, #ff9f43 40%, var(--border));
    background: color-mix(in srgb, var(--bg2) 90%, rgba(255,159,67,.08));
  }}
  .devq-kpi-val {{
    font-size: clamp(1.05rem, 2.6vw, 1.35rem);
    font-weight: 700;
    line-height: 1.1;
    font-variant-numeric: tabular-nums;
  }}
  .devq-kpi-lbl {{
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: .06em;
    color: var(--dim);
  }}
  .devq-ready-intro {{
    font-size: clamp(12px, 1.5vw, 13px);
    line-height: 1.45;
    margin: 0 0 12px;
  }}
  .devq-batch {{
    border: 1px solid color-mix(in srgb, var(--border) 85%, transparent);
    border-radius: 10px;
    overflow: hidden;
    margin-bottom: 12px;
    background: var(--bg);
  }}
  .devq-batch-hd {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 8px;
    padding: 10px 12px;
    background: color-mix(in srgb, var(--bg2) 88%, transparent);
    border-bottom: 1px solid color-mix(in srgb, var(--border) 70%, transparent);
    font-size: clamp(13px, 1.8vw, 15px);
    font-weight: 600;
  }}
  .devq-batch-ids {{
    font-size: 12px;
    font-family: var(--font-mono, ui-monospace, monospace);
  }}
  .devq-batch-files {{
    font-size: 12px;
    line-height: 1.5;
    padding: 8px 12px;
    border-bottom: 1px solid color-mix(in srgb, var(--border) 55%, transparent);
  }}
  .devq-files-label {{
    display: block;
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: .06em;
    margin-bottom: 4px;
    color: var(--dim);
  }}
  .devq-batch-files code {{
    font-size: 11px;
    word-break: break-all;
  }}
  .devq-batch .tos-table {{
    margin: 0;
    font-size: clamp(12px, 1.4vw, 13px);
    width: 100%;
  }}
  .devq-batch .tos-table th {{
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: .04em;
    color: var(--dim);
    padding: 8px 10px;
  }}
  .devq-batch .tos-table td {{
    padding: 10px;
    vertical-align: top;
  }}
  .devq-batch-actions {{
    padding: 10px 12px;
    background: color-mix(in srgb, var(--bg2) 40%, transparent);
  }}
  .devq-batch-actions .btn {{
    min-height: 40px;
    padding: 8px 16px;
  }}
  .devq-blocked {{
    border: 1px solid color-mix(in srgb, var(--border) 85%, transparent);
    border-radius: 10px;
    padding: 10px 12px;
    margin-bottom: 10px;
    background: color-mix(in srgb, var(--bg2) 50%, transparent);
  }}
  .devq-blocked-hd {{
    font-size: clamp(13px, 1.6vw, 14px);
    line-height: 1.4;
    margin-bottom: 6px;
  }}
  .devq-blocked-list {{
    margin: 0;
    padding-left: 1.1rem;
    font-size: clamp(12px, 1.4vw, 13px);
    line-height: 1.45;
  }}
  @media (max-width: 640px) {{
    .devq-batch .tos-table thead {{ display: none; }}
    .devq-batch .tos-table tr {{
      display: block;
      border-bottom: 1px solid color-mix(in srgb, var(--border) 50%, transparent);
      padding: 10px 0;
    }}
    .devq-batch .tos-table td {{
      display: block;
      padding: 4px 12px;
      border: 0;
    }}
    .devq-batch .tos-table td::before {{
      content: attr(data-h);
      display: block;
      font-size: 10px;
      text-transform: uppercase;
      letter-spacing: .05em;
      color: var(--dim);
      margin-bottom: 2px;
    }}
  }}
  /* Card titles: match primary nav weight/size — avoid outshouting .ts-seg */
  .ts-ops-page .tos-card-title {{
    font-size: clamp(0.8125rem, 1.45vw, 0.9375rem);
    font-weight: 600;
    letter-spacing: 0.02em;
  }}
  .task-server-hint {{
    font-size: var(--text-badge);
    letter-spacing: .04em;
  }}
  /* Last-updated indicator */
  .ts-last-updated {{
    font-size: 10px;
    font-family: var(--font-mono, monospace);
    letter-spacing: .03em;
  }}
  /* Ready / in-flight / stalled header pills (wl-28) */
  .ts-ready-badge, .ts-inflight-badge, .ts-stalled-badge {{
    font-size: 10px; font-weight: 700;
    padding: 2px 8px; border-radius: 999px;
    cursor: pointer; text-decoration: none;
  }}
  .ts-ready-badge {{
    background: color-mix(in srgb, var(--neon) 10%, transparent); color: var(--neon, #e8622c);
    border: 1px solid color-mix(in srgb, var(--neon) 28%, transparent);
  }}
  .ts-inflight-badge {{
    background: var(--bg2); color: var(--muted);
    border: 1px solid var(--border);
  }}
  .ts-stalled-badge {{
    background: rgba(255,59,59,.15); color: var(--red, #ff3b3b);
    border: 1px solid rgba(255,59,59,.3);
  }}
  .ts-attention-badge {{
    background: linear-gradient(180deg, rgba(255,231,168,.95), rgba(245,200,74,.9));
    color: #5c3a08; font-weight: 700;
    border: 1px solid #a8681e;
    box-shadow: 0 0 0 0 rgba(245,158,11,.0);
    animation: tsYouPulse 2.4s ease-in-out infinite;
  }}
  @keyframes tsYouPulse {{
    0%, 100% {{ box-shadow: 0 0 0 0 rgba(245,158,11,0); }}
    50% {{ box-shadow: 0 0 0 4px rgba(245,158,11,.28); }}
  }}
  /* Slide-down create panel (full form — same as former workspace card) */
  /* Filter form — consistent with main app */
  .ts-filter-form {{
    display: flex; gap: 10px; align-items: end; flex-wrap: wrap;
  }}
  .ts-filter-field {{
    display: flex; flex-direction: column; gap: 2px;
  }}
  .ts-filter-field label {{
    font-size: var(--fs-xs, 11px); margin-bottom: 0;
  }}
  .ts-filter-select, .ts-filter-input {{
    padding: 6px 10px; font-size: var(--fs-base, 14px);
    background: var(--bg); color: var(--fg);
    border: 1px solid var(--border); border-radius: var(--r-md, 6px);
    font-family: inherit;
  }}
  .ts-filter-select:focus, .ts-filter-input:focus {{
    border-color: var(--neon); outline: none;
  }}
  .ts-filter-select {{ min-width: 130px; }}
  .ts-filter-input  {{ min-width: 180px; }}
  .ts-filter-chips {{
    display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px;
  }}
  /* Card transition animations */
  .tb-card {{ transition: all .3s ease, opacity .3s ease; }}
  .tb-card.ts-card-entering {{
    animation: tsCardSlideIn .35s ease-out;
  }}
  @keyframes tsCardSlideIn {{
    from {{ opacity: 0; transform: translateX(-20px) scale(.95); }}
    to   {{ opacity: 1; transform: translateX(0) scale(1); }}
  }}
  </style>
</body>
</html>"""


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


def _plugin_wrap(html: str, plugin_id: str) -> str:
    pid = _esc(plugin_id)
    return f'<div class="ts-plugin" data-plugin="{pid}">{html}</div>'


def _dev_wq_table_href(status: str) -> str:
    return f"{_OPS_TASK_LIST_PATH}?view=table&status={_esc(status)}"


# wl-225: plugin shims + shared helpers extracted to server_helpers.py
from worklane.server_helpers import (  # noqa: E402
    _ATTENTION_PREFS_NAME,
    _FOUNDER_DECISION_LABELS,
    _active_attention_snoozes,
    _allocation_author_rows,
    _allocation_lane_rows,
    _archive_tracker_for_hot_db,
    _attention_item,
    _collect_founder_attention_items,
    _fetch_tradeos_json,
    _fetch_tradeos_ops_snapshot,
    _fetch_tradeos_tasks_via_http,
    _get_task_hot_or_archive,
    _item_is_snoozed,
    _list_tasks_for_wq_multi_resolved,
    _load_attention_prefs,
    _merged_in_flight_tasks,
    _merged_ready_count,
    _merged_scope_tasks_for_filters,
    _parse_task_date_utc,
    _parse_until_iso,
    _partition_attention_items,
    _public_prefix_for_surface,
    _pulse_relative_time,
    _request_tradeos_json,
    _resolve_product_tracker,
    _save_attention_prefs,
    _scoped_product_trackers,
    _stale_inflight,
    _task_from_tradeos_api_row,
    _task_relations_dicts,
    _tracker_db_path,
    _tradeos_api_base,
    _tradeos_preview_map_from_api_tasks,
    _tradeos_tickets_use_http_feed,
    _FOUNDER_ID_RE,
    _identity_config,
    _identity_config_path,
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




def _health_pill_class(overall: str) -> str:
    u = (overall or "").upper()
    if "RED" in u:
        return "ts-tw-pill ts-tw-pill--red"
    if "YELLOW" in u:
        return "ts-tw-pill ts-tw-pill--yellow"
    if "GREEN" in u:
        return "ts-tw-pill ts-tw-pill--green"
    return "ts-tw-pill ts-tw-pill--muted"


def _render_tradeos_widgets() -> str:
    snap = _fetch_tradeos_ops_snapshot()
    status = snap.get("status")
    positions = snap.get("positions") or {}
    trades = snap.get("trades") or {}
    signals = snap.get("signals") or {}
    base = _tradeos_api_base()
    app_href = _esc(base + "/")

    if status is None:
        offline = (
            f"<p class='ts-tw-offline'><strong>tradeOS is not reachable</strong> at "
            f"<code>{_esc(base)}</code>. Start the main web app "
            f"(for example <code>./tradeos</code> or <code>./tradeos dev</code>) to show "
            "mode, health, positions, and recent fills. "
            f"<a href='{app_href}' rel='noopener'>Open tradeOS</a> when it is running.</p>"
        )
        return _plugin_wrap(_task_card("tradeOS (live)", offline), "tradeos")

    mode = _esc(str(status.get("mode") or "—"))
    health_obj = status.get("health")
    overall_raw = "—"
    if isinstance(health_obj, dict):
        overall_raw = str(health_obj.get("overall") or "—")
    overall_esc = _esc(overall_raw)
    pill_cls = _health_pill_class(overall_raw)

    live_list = positions.get("live") if isinstance(positions, dict) else None
    paper_list = positions.get("paper") if isinstance(positions, dict) else None
    live_n = len(live_list) if isinstance(live_list, list) else 0
    paper_n = len(paper_list) if isinstance(paper_list, list) else 0
    tot = positions.get("total_count") if isinstance(positions, dict) else None
    if tot is None:
        tot = live_n + paper_n
    tot_s = _esc(str(tot))

    stat_grid = (
        "<div class='ts-tw-stat-grid'>"
        "<div class='ts-tw-stat'><div class='ts-tw-stat-label'>Mode</div>"
        f"<div class='ts-tw-stat-value'>{mode}</div></div>"
        "<div class='ts-tw-stat'><div class='ts-tw-stat-label'>System health</div>"
        f"<div><span class='{pill_cls}'>{overall_esc}</span></div></div>"
        "<div class='ts-tw-stat'><div class='ts-tw-stat-label'>Open positions</div>"
        f"<div class='ts-tw-stat-value'>{tot_s} <span class='dim ts-tw-stat-sub'>"
        f"{live_n} live · {paper_n} paper</span></div></div>"
        "</div>"
    )

    trade_rows: List[str] = []
    for t in (trades.get("trades") or [])[:8]:
        if not isinstance(t, dict):
            continue
        asset = _esc(str(t.get("asset") or ""))
        side = _esc(str(t.get("side") or ""))
        qty = _esc(str(t.get("qty") if t.get("qty") is not None else ""))
        src = _esc(str(t.get("source") or ""))
        tm = str(t.get("time") or "")
        tm = _esc(tm[:22] if len(tm) > 22 else tm)
        trade_rows.append(
            f"<tr><td>{asset}</td><td>{side}</td><td class='dim'>{qty}</td>"
            f"<td><span class='ts-tw-src'>{src}</span></td>"
            f"<td class='dim'>{tm}</td></tr>"
        )
    trades_body = (
        "<table class='tos-table ts-tw-table'><thead><tr>"
        "<th>Asset</th><th>Side</th><th>Qty</th><th>Src</th><th>Time</th>"
        "</tr></thead><tbody>"
        + (
            "".join(trade_rows)
            if trade_rows
            else "<tr><td colspan='5' class='dim'>No recent fills in the journal window.</td></tr>"
        )
        + "</tbody></table>"
    )

    sig_rows: List[str] = []
    for s in (signals.get("signals") or [])[:6]:
        if not isinstance(s, dict):
            continue
        aid = _esc(str(s.get("asset") or ""))
        sig = _esc(str(s.get("signal") or ""))
        tick = str(s.get("last_tick") or "")
        tick = _esc(tick[:22] if len(tick) > 22 else tick)
        sig_rows.append(f"<tr><td>{aid}</td><td>{sig}</td><td class='dim'>{tick}</td></tr>")
    signals_body = (
        "<table class='tos-table ts-tw-table'><thead><tr>"
        "<th>Asset</th><th>Signal</th><th>Last tick</th>"
        "</tr></thead><tbody>"
        + (
            "".join(sig_rows)
            if sig_rows
            else "<tr><td colspan='3' class='dim'>No loop signals yet (or loops idle).</td></tr>"
        )
        + "</tbody></table>"
    )

    meta = (
        "<p class='ts-tw-meta dim'>"
        "<span class='ts-plugin-tag'>plugin</span> "
        f"Read-only <code>/api/ops/*</code> on <code>{_esc(base)}</code> · "
        f"<a href='{app_href}' rel='noopener'>Open tradeOS</a></p>"
    )
    split = (
        "<div class='ts-tw-split'>"
        "<div class='ts-tw-panel'><h3 class='ts-tw-panel-title'>Recent fills</h3>"
        f"{trades_body}</div>"
        "<div class='ts-tw-panel'><h3 class='ts-tw-panel-title'>Loop signals</h3>"
        f"{signals_body}</div>"
        "</div>"
    )

    body = meta + stat_grid + split
    return _plugin_wrap(_task_card("tradeOS (live)", body), "tradeos")


def _ticket_status_chips_row(grouped: Dict[str, List[Task]], total: int) -> str:
    def _chip(label: str, count: int, href: str) -> str:
        return (
            f"<a class='devq-stat-chip' href='{href}'>"
            f"<span class='devq-stat-num'>{count}</span>"
            f"<span class='devq-stat-lbl'>{_esc(label)}</span>"
            "</a>"
        )

    board_home = f"{_OPS_TASK_LIST_PATH}?view=board"
    chips = [
        _chip("Total", total, board_home),
        _chip(
            "Backlog",
            len(grouped.get(TaskStatus.BACKLOG, [])),
            _dev_wq_table_href(TaskStatus.BACKLOG),
        ),
        _chip(
            "In progress",
            len(grouped.get(TaskStatus.IN_PROGRESS, [])),
            _dev_wq_table_href(TaskStatus.IN_PROGRESS),
        ),
        _chip(
            "In review",
            len(grouped.get(TaskStatus.IN_REVIEW, [])),
            _dev_wq_table_href(TaskStatus.IN_REVIEW),
        ),
        _chip(
            "Done",
            len(grouped.get(TaskStatus.DONE, [])),
            _dev_wq_table_href(TaskStatus.DONE),
        ),
    ]
    return f"<div class='devq-stat-row'>{''.join(chips)}</div>"


def _render_tickets_module() -> str:
    tracker = get_default_tracker()
    queue = WorkQueue(tracker)
    tasks = queue.all_tasks
    grouped: Dict[str, List[Task]] = {}
    for t in tasks:
        grouped.setdefault(t.status, []).append(t)
    total = len(tasks)

    wq_board = f"{_OPS_TASK_LIST_PATH}?view=board"
    wq_tbl = f"{_OPS_TASK_LIST_PATH}?view=table"

    strip = (
        "<div class='ts-wq-strip'>"
        "<p class='ts-ticket-strip-lead dim'>"
        "<span class='ts-plugin-tag'>plugin</span> "
        f"Same SQLite <strong>{_esc(_TICKETS_SYSTEM_LABEL)}</strong> store as "
        f"<a href='{wq_board}'>board</a> and "
        f"<a href='{wq_tbl}'>table</a> — jump by status or pick up a row below."
        "</p>"
        f"{_ticket_status_chips_row(grouped, total)}"
        "</div>"
    )

    open_tasks = [t for t in tasks if t.status not in (TaskStatus.DONE, TaskStatus.CANCELED)]
    open_sorted = sorted(
        open_tasks,
        key=lambda x: (x.updated_at or "") or "",
        reverse=True,
    )[:10]

    rows: List[str] = []
    for t in open_sorted:
        tid = _esc(str(t.id))
        link = f"/admin/desk?open={tid}"
        title = _esc(t.title)
        st_badge = _render_status_badge(t.status)
        pri = _render_priority_badge(int(t.priority or 3))
        upd = _esc((t.updated_at or "")[:19])
        rows.append(
            f"<tr><td><a href='{link}'><strong>#{tid}</strong> {title}</a></td>"
            f"<td>{st_badge}</td><td>{pri}</td><td class='dim'>{upd}</td></tr>"
        )

    tbl = (
        "<table class='tos-table ts-tw-table ts-ticket-recent'>"
        "<thead><tr><th>Work order</th><th>Status</th><th>Pri</th><th>Updated</th></tr></thead>"
        "<tbody>"
        + (
            "".join(rows)
            if rows
            # wl-91: quick-add is gone (wl-26) — don't advertise it.
            else "<tr><td colspan='4' class='dim'>No open work orders — "
            "check the <a href='" + wq_board + "'>Board</a> for Done.</td></tr>"
        )
        + "</tbody></table>"
    )

    foot = (
        f"<p class='ts-ticket-mod-foot dim'>"
        f"<a href='{wq_board}'>Board</a> · "
        f"<a href='{wq_tbl}'>Table</a>"
        "</p>"
    )

    inner = (
        "<h3 class='ts-tw-panel-title'>Open work (most recently touched)</h3>"
        + tbl
        + foot
    )
    card = _task_card(_TICKETS_SYSTEM_LABEL, strip + inner)
    return _plugin_wrap(card, "tickets")




def _render_count_bars(
    rows: List[Tuple[str, int, str, str, str]],
    *,
    empty_text: str = "No data.",
) -> str:
    total = sum(v for _, v, _, _, _ in rows)
    max_v = max([v for _, v, _, _, _ in rows] + [1])
    if total <= 0:
        return f"<p class='dim'>{_esc(empty_text)}</p>"
    html_rows: List[str] = []
    for label, value, href, key, fill_style in rows:
        pct = (float(value) / float(total)) * 100.0 if total > 0 else 0.0
        pct_rel = (float(value) / float(max_v)) * 100.0 if max_v > 0 else 0.0
        fill_pct = pct if pct > 0 else (2.0 if value > 0 else 0.0)
        html_rows.append(
            "<div style='display:grid;grid-template-columns:110px 1fr 44px 52px;gap:8px;align-items:center;' "
            f"data-cockpit-row='{_esc(key)}'>"
            f"<a href='{_esc(href)}' class='dim' style='text-decoration:none'>{_esc(label)}</a>"
            "<div style='height:12px;background:var(--bg3);border:1px solid var(--border);border-radius:999px;overflow:hidden;'>"
            f"<div data-cockpit-fill='{_esc(key)}' data-cockpit-rel='{pct_rel:.3f}' "
            f"style='height:100%;width:{fill_pct:.2f}%;{fill_style}'></div>"
            "</div>"
            f"<div class='dim' data-cockpit-count='{_esc(key)}' style='text-align:right;font-variant-numeric:tabular-nums'>{int(value)}</div>"
            f"<div class='dim' data-cockpit-pct='{_esc(key)}' style='text-align:right;font-variant-numeric:tabular-nums'>{pct:.1f}%</div>"
            "</div>"
        )
    return "<div style='display:grid;gap:8px;'>" + "".join(html_rows) + "</div>"


def _render_activity_chart(tasks: List[Task], *, days: int = 14) -> str:
    today = datetime.now(timezone.utc).date()
    day_list = [today - timedelta(days=(days - 1 - i)) for i in range(days)]
    counts: Dict[str, int] = {d.isoformat(): 0 for d in day_list}
    for t in tasks:
        dt = _parse_task_date_utc(t.updated_at)
        if dt is None:
            continue
        key = dt.date().isoformat()
        if key in counts:
            counts[key] += 1
    vals = [counts[d.isoformat()] for d in day_list]
    max_v = max(vals + [1])
    w = 420
    h = 110
    gap = 3
    bar_w = max(4, int((w - (days - 1) * gap) / days))
    bars: List[str] = []
    for i, v in enumerate(vals):
        bh = int((float(v) / float(max_v)) * (h - 22)) if max_v > 0 else 0
        x = i * (bar_w + gap)
        y = h - 16 - bh
        bars.append(
            f"<rect data-cockpit-activity-bar='d{i}' data-cockpit-activity-value='{int(v)}' "
            f"x='{x}' y='{y}' width='{bar_w}' height='{max(1, bh)}' "
            "fill='url(#tpGrad)' rx='2' ry='2'></rect>"
        )
    first_lbl = day_list[0].strftime("%m-%d")
    last_lbl = day_list[-1].strftime("%m-%d")
    svg = (
        f"<svg viewBox='0 0 {w} {h}' role='img' aria-label='Work order activity last {days} days' "
        "style='width:100%;height:auto;display:block;'>"
        "<defs><linearGradient id='tpGrad' x1='0' y1='0' x2='1' y2='0'>"
        "<stop offset='0%' stop-color='var(--accent,#d94f1e)'/><stop offset='100%' stop-color='var(--accent2,#e8622c)'/>"
        "</linearGradient></defs>"
        f"{''.join(bars)}"
        f"<text x='0' y='{h-2}' fill='var(--dim)' font-size='10'>{_esc(first_lbl)}</text>"
        f"<text x='{w-34}' y='{h-2}' fill='var(--dim)' font-size='10'>{_esc(last_lbl)}</text>"
        "</svg>"
    )
    # wl-89: no lead paragraph — the enclosing pulse panel head names the range.
    return (
        svg
        + f"<div id='cockpit-activity-meta' data-cockpit-activity-days='{days}' hidden></div>"
    )


def _render_service_health_body(scope: str = "") -> str:
    """Service health pane for the Overview (#485) — uptime, store size, queue
    depth. Store rows come from the discovered registry and the queue counts
    from the in-scope trackers (wl-85) — nothing named in product code.
    Returns panel-body HTML; the Pulse grid wraps it (wl-89).
    """
    # Uptime
    now = datetime.now(timezone.utc)
    delta = now - _SERVER_START
    total_s = int(delta.total_seconds())
    h, rem = divmod(total_s, 3600)
    m, s = divmod(rem, 60)
    uptime_str = f"{h}h {m}m {s}s" if h else f"{m}m {s}s"
    started_str = _SERVER_START.strftime("%H:%M UTC")

    # DB paths + sizes
    def _db_row(label: str, path: object) -> str:
        import pathlib
        p = pathlib.Path(str(path))
        if p.exists():
            size_kb = p.stat().st_size / 1024
            size_str = f"{size_kb:.1f} KB" if size_kb < 1024 else f"{size_kb / 1024:.2f} MB"
            status_color = "var(--green)"
            status_dot = "●"
        else:
            size_str = "not found"
            status_color = "var(--red, #ef4444)"
            status_dot = "●"
        return (
            f"<div style='display:flex; justify-content:space-between; align-items:center; "
            f"font-size:11px; padding:3px 0; border-bottom:1px solid var(--border);'>"
            f"<span style='color:var(--dim);'>{_esc(label)}</span>"
            f"<span style='font-family:monospace; font-size:10px;'>"
            f"<span style='color:{status_color};'>{status_dot}</span> {_esc(size_str)}</span>"
            f"</div>"
        )

    specs = [
        spec for spec in discover_products()
        if not scope or spec.slug == scope
    ]
    db_rows = "".join(_db_row(spec.db_path.name, spec.db_path) for spec in specs)
    if not db_rows:
        db_rows = "<span class='dim' style='font-size:11px;'>no stores discovered</span>"

    # Queue depth across the in-scope stores
    try:
        all_td: List[Task] = []
        for _spec, tracker in _scoped_product_trackers(scope):
            all_td.extend(tracker.list_tasks())
        ip_count = len([t for t in all_td if t.status == TaskStatus.IN_PROGRESS])
        ir_count = len([t for t in all_td if t.status == TaskStatus.IN_REVIEW])
        bl_count = len([t for t in all_td if t.status == TaskStatus.BACKLOG])
        done_count = len([t for t in all_td if t.status == TaskStatus.DONE])
        total_count = len(all_td)
        queue_html = (
            f"<div style='display:grid; grid-template-columns:1fr 1fr; gap:4px; font-size:11px; margin-top:4px;'>"
            f"<span style='color:var(--dim);'>in_progress</span><span style='font-variant-numeric:tabular-nums; text-align:right;'>{ip_count}</span>"
            f"<span style='color:var(--dim);'>in_review</span><span style='font-variant-numeric:tabular-nums; text-align:right;'>{ir_count}</span>"
            f"<span style='color:var(--dim);'>backlog</span><span style='font-variant-numeric:tabular-nums; text-align:right;'>{bl_count}</span>"
            f"<span style='color:var(--dim);'>done</span><span style='font-variant-numeric:tabular-nums; text-align:right;'>{done_count}</span>"
            f"<span style='color:var(--dim); font-weight:700;'>total</span><span style='font-variant-numeric:tabular-nums; text-align:right; font-weight:700;'>{total_count}</span>"
            f"</div>"
        )
    except Exception:
        queue_html = "<span class='dim' style='font-size:11px;'>store unavailable</span>"

    body = (
        f"<div style='font-size:11px; margin-bottom:10px;'>"
        f"<div style='display:flex; justify-content:space-between; margin-bottom:4px;'>"
        f"<span class='dim'>Uptime</span><span style='font-family:monospace;'>{_esc(uptime_str)}</span>"
        f"</div>"
        f"<div style='display:flex; justify-content:space-between;'>"
        f"<span class='dim'>Started</span><span style='font-family:monospace;'>{_esc(started_str)}</span>"
        f"</div>"
        f"</div>"
        f"<div style='font-size:11px; font-weight:700; color:var(--dim); text-transform:uppercase; letter-spacing:0.4px; margin-bottom:4px;'>Store</div>"
        f"{db_rows}"
        f"<div style='font-size:11px; font-weight:700; color:var(--dim); text-transform:uppercase; letter-spacing:0.4px; margin:10px 0 4px;'>Queue depth</div>"
        f"{queue_html}"
    )
    return body


def _render_breakdown_panels(
    scope: str, all_tasks: List[Task]
) -> Tuple[str, str, str]:
    """wl-89: status/priority count bars + 14-day activity chart for the
    Pulse grid — the former cockpit cards, re-homed as themed panel bodies.
    Returns (breakdown_body, breakdown_meta, activity_body). The live JS
    (data-cockpit-* hooks, /api/admin/overview/summary) targets this markup.
    """
    tasks = [t for t in all_tasks if t.status != TaskStatus.DONE]
    total = len(tasks)
    pool_path = f"/admin/tickets/{scope or 'all'}"
    board = f"{pool_path}?view=board"
    table = f"{pool_path}?view=table"

    status_fill = {
        TaskStatus.BACKLOG: "background:linear-gradient(90deg,#9c8468,#b58a5a);",
        TaskStatus.IN_PROGRESS: "background:linear-gradient(90deg,#f59e0b,#f97316);",
        TaskStatus.IN_REVIEW: "background:linear-gradient(90deg,#52667a,#7a8fa3);",
        TaskStatus.CANCELED: "background:linear-gradient(90deg,#6b7280,#9ca3af);",
    }
    status_rows: List[Tuple[str, int, str, str, str]] = []
    for s in (
        TaskStatus.BACKLOG,
        TaskStatus.IN_PROGRESS,
        TaskStatus.IN_REVIEW,
        TaskStatus.CANCELED,
    ):
        c = len([t for t in tasks if t.status == s])
        status_rows.append(
            (
                str(_STATUS_LABELS.get(s, s)),
                c,
                f"{table}&status={_esc(s)}",
                f"status:{s}",
                status_fill.get(s, "background:linear-gradient(90deg,#d94f1e,#e8622c);"),
            )
        )

    pri_fill = {
        1: "background:linear-gradient(90deg,#ef4444,#fb7185);",
        2: "background:linear-gradient(90deg,#f59e0b,#f97316);",
        3: "background:linear-gradient(90deg,#d94f1e,#e8622c);",
        4: "background:linear-gradient(90deg,#64748b,#94a3b8);",
    }
    pri_rows: List[Tuple[str, int, str, str, str]] = []
    for p in (1, 2, 3, 4):
        c = len([t for t in tasks if int(t.priority or 3) == p])
        pri_rows.append(
            (
                str(_PRIORITY_LABELS.get(p, f"P{p}")),
                c,
                f"{table}&priority={p}",
                f"priority:{p}",
                pri_fill.get(p, "background:linear-gradient(90deg,#d94f1e,#e8622c);"),
            )
        )

    breakdown_body = (
        "<div class='pulse-breakdown'>"
        "<div><div class='pulse-breakdown-title'>By status</div>"
        + _render_count_bars(status_rows, empty_text="No work order statuses yet.")
        + "</div>"
        "<div><div class='pulse-breakdown-title'>By priority</div>"
        + _render_count_bars(pri_rows, empty_text="No priority data yet.")
        + "</div>"
        "</div>"
    )
    breakdown_meta = (
        f"{total} open · <a href='{board}'>board</a> · <a href='{table}'>table</a>"
    )
    activity_body = _render_activity_chart(tasks, days=14)
    return breakdown_body, breakdown_meta, activity_body


def _cockpit_live_js() -> str:
    return r"""
<script>
  function tpCockpitFillStyleForKey(key) {
    var k = String(key || '');
    var map = {
      'status:backlog': 'linear-gradient(90deg,#9c8468,#b58a5a)',
      'status:in_progress': 'linear-gradient(90deg,#f59e0b,#f97316)',
      'status:in_review': 'linear-gradient(90deg,#52667a,#7a8fa3)',
      'status:canceled': 'linear-gradient(90deg,#6b7280,#9ca3af)',
      'priority:1': 'linear-gradient(90deg,#ef4444,#fb7185)',
      'priority:2': 'linear-gradient(90deg,#f59e0b,#f97316)',
      'priority:3': 'linear-gradient(90deg,#d94f1e,#e8622c)',
      'priority:4': 'linear-gradient(90deg,#64748b,#94a3b8)'
    };
    return map[k] || 'linear-gradient(90deg,#d94f1e,#e8622c)';
  }

  function tpCockpitSetRow(key, value, total) {
    var countEl = document.querySelector("[data-cockpit-count='" + key + "']");
    var pctEl = document.querySelector("[data-cockpit-pct='" + key + "']");
    var fillEl = document.querySelector("[data-cockpit-fill='" + key + "']");
    if (!countEl || !pctEl || !fillEl) return;
    var v = Number(value || 0);
    var t = Number(total || 0);
    var pct = t > 0 ? (v / t) * 100.0 : 0.0;
    var fillPct = pct > 0 ? pct : (v > 0 ? 2.0 : 0.0);
    countEl.textContent = String(v);
    pctEl.textContent = pct.toFixed(1) + '%';
    fillEl.style.width = fillPct.toFixed(2) + '%';
    fillEl.style.background = tpCockpitFillStyleForKey(key);
    fillEl.style.opacity = v > 0 ? '1' : '0';
    fillEl.style.transition = 'width 240ms ease, opacity 180ms ease';
  }

  function tpCockpitSetActivity(series) {
    if (!Array.isArray(series) || !series.length) return;
    var maxV = 1;
    for (var i = 0; i < series.length; i++) {
      maxV = Math.max(maxV, Number(series[i] || 0));
    }
    for (var j = 0; j < series.length; j++) {
      var v = Number(series[j] || 0);
      var bar = document.querySelector("[data-cockpit-activity-bar='d" + j + "']");
      if (!bar) continue;
      var h = Math.max(1, Math.round((v / maxV) * 88));
      var y = 94 - h;
      bar.setAttribute('height', String(h));
      bar.setAttribute('y', String(y));
      bar.setAttribute('data-cockpit-activity-value', String(v));
    }
  }

  async function tpCockpitRefresh() {
    try {
      var scope = document.body.getAttribute('data-ops-scope') || '';
      var resp = await fetch('/api/admin/overview/summary?scope=' +
          encodeURIComponent(scope) + '&_=' + Date.now(), {
        cache: 'no-store',
        headers: { 'Accept': 'application/json' }
      });
      var j = await resp.json();
      if (!j || !j.ok) return;
      tpCockpitSetRow('status:backlog', j.status_counts.backlog || 0, j.total || 0);
      tpCockpitSetRow('status:in_progress', j.status_counts.in_progress || 0, j.total || 0);
      tpCockpitSetRow('status:in_review', j.status_counts.in_review || 0, j.total || 0);
      tpCockpitSetRow('status:canceled', j.status_counts.canceled || 0, j.total || 0);
      tpCockpitSetRow('priority:1', j.priority_counts['1'] || 0, j.total || 0);
      tpCockpitSetRow('priority:2', j.priority_counts['2'] || 0, j.total || 0);
      tpCockpitSetRow('priority:3', j.priority_counts['3'] || 0, j.total || 0);
      tpCockpitSetRow('priority:4', j.priority_counts['4'] || 0, j.total || 0);
      tpCockpitSetActivity(j.activity_14d || []);
    } catch (e) { /* no-op */ }
  }

  function tpCockpitInit() {
    tpCockpitRefresh();
    setInterval(function() {
      if (document.visibilityState === 'hidden') return;
      tpCockpitRefresh();
    }, 4000);
    document.addEventListener('visibilitychange', function() {
      if (document.visibilityState === 'visible') tpCockpitRefresh();
    });
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', tpCockpitInit);
  } else {
    tpCockpitInit();
  }
</script>
"""


def _render_product_tradeos_hub() -> str:
    th = os.environ.get("TRADEOS_HOST", "127.0.0.1")
    wl = os.environ.get("TRADEOS_PORT", "8788")
    to_url = f"http://{th}:{wl}/"
    to_url_esc = _esc(to_url)
    wq = f"{_OPS_TASK_LIST_PATH}?view=board"

    lead = (
        "<p class='ts-ops-lead dim'><strong>tradeOS</strong> is the primary project here. "
        "This page will aggregate work order summaries, work-in-flight, ADR links, and other "
        "cross-cutting signals as we wire them up.</p>"
    )
    open_app = _task_card(
        "Open the app",
        "<p class='dim'>Five-tab web UI, engines, brokers — edge, risk, execution, feedback, "
        "and context around a self-hosted book.</p>"
        f"<p class='dim'>Default URL <code>{_esc(th)}:{_esc(wl)}</code> "
        "(override with <code>TRADEOS_HOST</code> / <code>TRADEOS_PORT</code>).</p>"
        f"<p><a class='btn go' href='{to_url_esc}' rel='noopener'>Open tradeOS</a></p>",
    )
    placeholders = (
        _task_card(
            "Tickets & work (next)",
            "<p class='dim'>Planned: counts and links into the shared SQLite tracker — same data as "
            f"the <a href='{wq}'>Board</a> — scoped to work that touches this project.</p>",
        )
        + _task_card(
            "ADRs & docs (next)",
            "<p class='dim'>Planned: pointers to decision records and operator docs that apply to "
            "tradeOS, without duplicating the full doc tree.</p>",
        )
    )
    return (
        "<div class='ts-prod-page'>"
        + lead
        + _render_tradeos_widgets()
        + open_app
        + placeholders
        + "</div>"
    )


def _render_product_ops_page() -> str:
    task_port = _esc(os.environ.get("TASK_PORT", "8799"))
    body = (
        "<p class='dim'>The work order store (<strong>Board</strong> / <strong>Table</strong>) runs on a "
        "<strong>separate port</strong> from the host product so the host can restart "
        "without losing the board (ADR-019).</p>"
        f"<p class='dim'>You are on port <code>{task_port}</code> — landing: "
        "<code>/admin/overview</code>. Use <strong>Board</strong> or <strong>Table</strong> in the "
        "header for the tracker; <strong>Projects</strong> for the per-project hubs.</p>"
    )
    return f"<div class='ts-prod-page'>{_task_card('Ticketing (this console)', body)}</div>"


# ── Pulse — live operator dashboard ─────────────────────────────────────



def _pulse_priority_color(p: int) -> str:
    return {1: "#ef4444", 2: "#f59e0b", 3: "#38bdf8", 4: "#64748b"}.get(int(p or 3), "#38bdf8")


def _pulse_status_color(s: str) -> str:
    return {
        TaskStatus.IN_PROGRESS: "#f97316",
        TaskStatus.IN_REVIEW: "#7a8fa3",
        TaskStatus.BACKLOG: "#38bdf8",
        TaskStatus.DONE: "#4caf7d",
        TaskStatus.CANCELED: "#9ca3af",
    }.get(s, "#64748b")


# wl-29: cycled Dispatch tokens (theme-aware — no raw hex) so each product
# store gets a stable dot color across the ticker and Next up rows (wl-96:
# the fleet/Projects panel that introduced these is retired).
_FLEET_DOT_VARS: Tuple[str, ...] = (
    "--neon", "--blue", "--mag", "--orange", "--purple", "--green", "--yellow",
)


def _fleet_dot_vars_by_slug() -> Dict[str, str]:
    return {
        spec.slug: _FLEET_DOT_VARS[i % len(_FLEET_DOT_VARS)]
        for i, spec in enumerate(discover_products())
    }


def _render_active_agents_panel(
    inflight_tasks: List[Task], *, now: datetime,
    previews: Dict[str, Dict[str, str]],
) -> str:
    """wl-29: who's on what, derived from the latest comment per in-flight ticket.

    The latest comment is the Owner: marker itself when a ticket has had no
    activity since claim, so its age doubles as idle time — no separate
    Start: timestamp parser needed.
    """
    if not inflight_tasks:
        return "<div class='pulse-empty'>⚙ No claims in flight.</div>"
    rows = ""
    for t in inflight_tasks:
        preview = previews.get(t.id) or {}
        owner = _detect_owner(preview) if preview else None
        icon, label = owner if owner else ("●", "unknown")
        ts = preview.get("created_at") or t.updated_at or t.created_at
        age = _pulse_relative_time(ts, now=now)
        rows += (
            f"<a href='/admin/desk?open={_esc(t.id)}' class='agent-row'>"
            f"<span class='agent-icon'>{icon}</span>"
            f"<span class='agent-label'>{_esc(label)}</span>"
            f"<span class='agent-arrow'>&rarr;</span>"
            f"<span class='agent-id'>#{_esc(t.id)}</span>"
            f"<span class='agent-age'>{age} ago</span>"
            "</a>"
        )
    return f"<div class='agent-rows'>{rows}</div>"


def _author_tally(
    scope: str = "", pending: Optional[Dict[str, int]] = None
) -> List[Dict[str, Any]]:
    """wl-93: tickets worked per comment author across the in-scope stores.

    Derived entirely from signed comments (wl-84: no hardcoded roster) via
    read-only SQL: worked = distinct tickets the author commented on,
    closed = distinct tickets where they posted a §5 closeout
    (body starts 'Completed:'), last = most recent comment timestamp.
    wl-95: ``pending`` maps author -> in-flight tickets they currently own
    (latest Owner: marker); owners missing from the comment tally still get
    a row so pending work is never hidden.
    """
    merged: Dict[str, Dict[str, Any]] = {}
    for _spec, tracker in _scoped_product_trackers(scope):
        db_path = _tracker_db_path(tracker)
        if db_path is None or not Path(db_path).exists():
            continue
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                rows = conn.execute(
                    """
                    SELECT author,
                           COUNT(DISTINCT task_id) AS worked,
                           COUNT(DISTINCT CASE WHEN body LIKE 'Completed:%'
                                               THEN task_id END) AS closed,
                           MAX(created_at) AS last_at
                    FROM task_comments
                    WHERE author != ''
                    GROUP BY author
                    """
                ).fetchall()
            finally:
                conn.close()
        except Exception:
            continue
        for author, worked, closed, last_at in rows:
            agg = merged.setdefault(
                author,
                {"author": author, "worked": 0, "closed": 0, "pending": 0,
                 "last_at": ""},
            )
            agg["worked"] += int(worked or 0)
            agg["closed"] += int(closed or 0)
            if (last_at or "") > agg["last_at"]:
                agg["last_at"] = last_at or ""
    for owner, n in (pending or {}).items():
        agg = merged.setdefault(
            owner,
            {"author": owner, "worked": 0, "closed": 0, "pending": 0,
             "last_at": ""},
        )
        agg["pending"] = int(n)
    out = list(merged.values())
    out.sort(key=lambda a: (-a["worked"], a["author"]))
    return out


def _render_author_tally_panel(
    tallies: List[Dict[str, Any]], *, now: datetime
) -> str:
    """wl-93: per-author scoreboard rows — worked · closed · pending · last."""
    if not tallies:
        return "<div class='pulse-empty'>No signed comments yet.</div>"
    rows = (
        "<div class='pulse-authors-hd'>"
        "<span></span><span>worked</span><span>closed</span>"
        "<span>pending</span><span>last</span>"
        "</div>"
    )
    # Top 10 by worked, but never drop an author who owns pending work.
    shown = tallies[:10] + [a for a in tallies[10:] if a.get("pending")]
    for a in shown:
        age = _pulse_relative_time(a["last_at"], now=now)
        n_pending = int(a.get("pending") or 0)
        pending_cell = (
            f"<span class='pulse-author-n pulse-author-n--pending'>{n_pending}</span>"
            if n_pending else
            "<span class='pulse-author-n pulse-author-n--zero'>0</span>"
        )
        rows += (
            "<div class='pulse-author-row'>"
            f"<span class='pulse-author-name'>{_esc(a['author'])}</span>"
            f"<span class='pulse-author-n'>{a['worked']}</span>"
            f"<span class='pulse-author-n pulse-author-n--closed'>{a['closed']}</span>"
            f"{pending_cell}"
            f"<span class='pulse-author-age'>{age}</span>"
            "</div>"
        )
    return f"<div class='pulse-side-rows'>{rows}</div>"


_LANE_LABEL_PREFIX = "lane:"


def _lane_lens_rows(
    all_tasks: List[Task], *, prefix: str = _LANE_LABEL_PREFIX
) -> List[Dict[str, Any]]:
    """wl-100: queue depth per lane:* label — backlog/gated/in-flight counts.

    Forward-looking counterpart to the Authors panel: a straight facet over
    labels already on each task (no comment parsing). Backlog tasks with no
    lane:* label collect into a synthetic 'unlabeled' row so triage-starved
    lanes are visible. The prefix is a convention, not a hardcoded roster —
    any label matching it becomes its own row.
    """
    buckets: Dict[str, Dict[str, int]] = {}

    def _bucket(name: str) -> Dict[str, int]:
        return buckets.setdefault(name, {"backlog": 0, "gated": 0, "inflight": 0})

    for t in all_tasks:
        lanes = [lbl[len(prefix):] for lbl in (t.labels or []) if lbl.startswith(prefix)]
        if t.status == TaskStatus.BACKLOG:
            key = "gated" if task_is_gated(t) else "backlog"
            for lane in (lanes or ["unlabeled"]):
                _bucket(lane)[key] += 1
        elif t.status in (TaskStatus.IN_PROGRESS, TaskStatus.IN_REVIEW):
            for lane in lanes:
                _bucket(lane)["inflight"] += 1

    rows = [
        {"lane": lane, **counts}
        for lane, counts in buckets.items()
        if counts["backlog"] or counts["gated"] or counts["inflight"]
    ]
    rows.sort(
        key=lambda r: (
            r["lane"] != "unlabeled",
            -(r["backlog"] + r["gated"] + r["inflight"]),
            r["lane"],
        )
    )
    return rows


def _render_lane_lens_panel(rows: List[Dict[str, Any]]) -> str:
    """wl-100: per-lane queue depth — backlog · gated · in-flight rows."""
    if not rows:
        return "<div class='pulse-empty'>No lane:* labels in scope.</div>"
    head = (
        "<div class='pulse-lane-hd'>"
        "<span></span><span>backlog</span><span>gated</span><span>in-flight</span>"
        "</div>"
    )
    body = ""
    for r in rows:
        gated_cell = (
            f"<span class='pulse-lane-n pulse-lane-n--gated'>{r['gated']}</span>"
            if r["gated"] else
            "<span class='pulse-lane-n pulse-lane-n--zero'>0</span>"
        )
        inflight_cell = (
            f"<span class='pulse-lane-n pulse-lane-n--inflight'>{r['inflight']}</span>"
            if r["inflight"] else
            "<span class='pulse-lane-n pulse-lane-n--zero'>0</span>"
        )
        body += (
            "<div class='pulse-lane-row'>"
            f"<span class='pulse-lane-name'>{_esc(r['lane'])}</span>"
            f"<span class='pulse-lane-n'>{r['backlog']}</span>"
            f"{gated_cell}"
            f"{inflight_cell}"
            "</div>"
        )
    return f"<div class='pulse-side-rows'>{head}{body}</div>"


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


# wl-106: allocation view — filed-vs-closed per lane and per author over a
# selectable window, plus a totals row reconciling with wl_counts.
_ALLOCATION_WINDOWS = (7, 14, 30)




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


def _render_allocation_panel(
    lane_rows: List[Dict[str, Any]],
    author_rows: List[Dict[str, Any]],
    totals: Dict[str, Any],
    *,
    scope: str,
    window_days: int,
) -> str:
    """wl-106: stacked filed-vs-closed per lane and per author, plus an
    all-time totals row reconciling with wl_counts."""
    scope_path = scope or "all"
    selector = "".join(
        f"<a href='/admin/overview/{_esc(scope_path)}?days={d}' "
        f"class='pulse-alloc-seg{' pulse-alloc-seg--on' if d == window_days else ''}'>{d}d</a>"
        for d in _ALLOCATION_WINDOWS
    )

    def _table(rows: List[Dict[str, Any]], key: str, empty_msg: str, *, flag_imbalance: bool = False) -> str:
        if not rows:
            return f"<div class='pulse-empty'>{empty_msg}</div>"
        head = (
            "<div class='pulse-alloc-hd'>"
            "<span></span><span>filed</span><span>closed</span>"
            "</div>"
        )
        body = "".join(
            "<div class='pulse-alloc-row'>"
            f"<span class='pulse-alloc-name'>{_esc(r[key])}"
            + (
                " <span class='pulse-alloc-flag' title='intake &gt; drain this window'>&#9650;</span>"
                if flag_imbalance and r["filed"] > r["closed"] else ""
            )
            + "</span>"
            f"<span class='pulse-alloc-n pulse-alloc-n--filed'>{r['filed']}</span>"
            f"<span class='pulse-alloc-n pulse-alloc-n--closed'>{r['closed']}</span>"
            "</div>"
            for r in rows
        )
        return f"<div class='pulse-side-rows'>{head}{body}</div>"

    lane_html = _table(
        lane_rows, "lane", "No lane:* labels filed or closed in this window.",
        flag_imbalance=True,
    )
    author_html = _table(author_rows, "author", "No signed comments in this window.")

    totals_counts = totals["counts"]
    totals_row = " · ".join(
        f"{_STATUS_LABELS.get(s, s)} {totals_counts.get(s, 0)}"
        for s in TaskStatus.ALL if totals_counts.get(s, 0)
    ) or "empty store"

    return (
        f"<div class='pulse-alloc-selector' data-testid='allocation-window'>{selector}</div>"
        "<div class='pulse-alloc-section'>"
        "<div class='pulse-alloc-label'>By lane</div>" + lane_html + "</div>"
        "<div class='pulse-alloc-section'>"
        "<div class='pulse-alloc-label'>By author</div>" + author_html + "</div>"
        "<div class='pulse-alloc-totals'>"
        f"<span class='pulse-alloc-totals-label'>All-time totals &Sigma; {totals['total']} "
        f"(reconciles with wl_counts)</span>"
        f"<span class='pulse-alloc-totals-row'>{_esc(totals_row)}</span>"
        "</div>"
    )


# wl-107: cycle-time (closed-in-window) and age (currently-open) distributions
# per lane:* label and per priority — median/p90 in hours, exposing where work
# stalls. Same created/updated proxy as the Allocation view (no closed_at
# column).
def _cycle_age_lane_rows(
    all_tasks: List[Task], since: datetime, now: datetime, *, prefix: str = _LANE_LABEL_PREFIX
) -> List[Dict[str, Any]]:
    cycle_buckets: Dict[str, List[float]] = {}
    age_buckets: Dict[str, List[float]] = {}
    for t in all_tasks:
        lanes = [lbl[len(prefix):] for lbl in (t.labels or []) if lbl.startswith(prefix)] or ["unlabeled"]
        if t.status == TaskStatus.DONE:
            closed = _parse_iso_ts(t.updated_at)
            created = _parse_iso_ts(t.created_at)
            if closed is not None and created is not None and closed >= since and closed >= created:
                hrs = (closed - created).total_seconds() / 3600
                for lane in lanes:
                    cycle_buckets.setdefault(lane, []).append(hrs)
        elif t.status in (TaskStatus.BACKLOG, TaskStatus.IN_PROGRESS, TaskStatus.IN_REVIEW):
            created = _parse_iso_ts(t.created_at)
            if created is not None:
                hrs = max(0.0, (now - created).total_seconds() / 3600)
                for lane in lanes:
                    age_buckets.setdefault(lane, []).append(hrs)
    return _cycle_age_rows_from_buckets(cycle_buckets, age_buckets, sort_key=lambda lane: lane != "unlabeled")


def _cycle_age_priority_rows(
    all_tasks: List[Task], since: datetime, now: datetime
) -> List[Dict[str, Any]]:
    cycle_buckets: Dict[str, List[float]] = {}
    age_buckets: Dict[str, List[float]] = {}
    for t in all_tasks:
        pri = f"P{int(t.priority or 3)}"
        if t.status == TaskStatus.DONE:
            closed = _parse_iso_ts(t.updated_at)
            created = _parse_iso_ts(t.created_at)
            if closed is not None and created is not None and closed >= since and closed >= created:
                hrs = (closed - created).total_seconds() / 3600
                cycle_buckets.setdefault(pri, []).append(hrs)
        elif t.status in (TaskStatus.BACKLOG, TaskStatus.IN_PROGRESS, TaskStatus.IN_REVIEW):
            created = _parse_iso_ts(t.created_at)
            if created is not None:
                hrs = max(0.0, (now - created).total_seconds() / 3600)
                age_buckets.setdefault(pri, []).append(hrs)
    return _cycle_age_rows_from_buckets(cycle_buckets, age_buckets, sort_key=lambda pri: pri)


def _cycle_age_rows_from_buckets(
    cycle_buckets: Dict[str, List[float]],
    age_buckets: Dict[str, List[float]],
    *,
    sort_key,
) -> List[Dict[str, Any]]:
    names = sorted(set(cycle_buckets) | set(age_buckets), key=sort_key)
    rows = []
    for name in names:
        c = sorted(cycle_buckets.get(name, []))
        a = sorted(age_buckets.get(name, []))
        rows.append({
            "name": name,
            "cycle_median": _percentile(c, 0.5),
            "cycle_p90": _percentile(c, 0.9),
            "age_median": _percentile(a, 0.5),
            "age_p90": _percentile(a, 0.9),
            "cycle_n": len(c),
            "age_n": len(a),
        })
    return rows


def _fmt_hours(hrs: Optional[float]) -> str:
    if hrs is None:
        return "—"
    return _fmt_minutes(int(hrs * 60))


def _render_cycle_age_panel(
    lane_rows: List[Dict[str, Any]], priority_rows: List[Dict[str, Any]], *, window_days: int
) -> str:
    """wl-107: median/p90 cycle time (closed in window) and age (open now)
    per lane and per priority, in compact duration form."""

    def _table(rows: List[Dict[str, Any]], empty_msg: str) -> str:
        if not rows:
            return f"<div class='pulse-empty'>{empty_msg}</div>"
        head = (
            "<div class='pulse-cycle-hd'>"
            "<span></span><span>cyc.med</span><span>cyc.p90</span>"
            "<span>age.med</span><span>age.p90</span>"
            "</div>"
        )
        body = "".join(
            "<div class='pulse-cycle-row'>"
            f"<span class='pulse-cycle-name'>{_esc(r['name'])}</span>"
            f"<span class='pulse-cycle-n'>{_fmt_hours(r['cycle_median'])}</span>"
            f"<span class='pulse-cycle-n'>{_fmt_hours(r['cycle_p90'])}</span>"
            f"<span class='pulse-cycle-n pulse-cycle-n--age'>{_fmt_hours(r['age_median'])}</span>"
            f"<span class='pulse-cycle-n pulse-cycle-n--age'>{_fmt_hours(r['age_p90'])}</span>"
            "</div>"
            for r in rows
        )
        return f"<div class='pulse-side-rows'>{head}{body}</div>"

    lane_html = _table(lane_rows, "No lane:* labels closed or open in this window.")
    priority_html = _table(priority_rows, "No tasks closed or open in this window.")
    return (
        f"<div class='pulse-alloc-selector'>window {window_days}d — cyc. = closed-in-window, "
        "age = currently open</div>"
        "<div class='pulse-alloc-section'>"
        "<div class='pulse-alloc-label'>By lane</div>" + lane_html + "</div>"
        "<div class='pulse-alloc-section'>"
        "<div class='pulse-alloc-label'>By priority</div>" + priority_html + "</div>"
    )


# wl-107: focus cut — founder-session prep list. Clusters (by lane:* label)
# ranked by open P1/P2 count x staleness x blocked-status. Data only, no
# prescriptions — the founder decides what to do with the ranking.
def _focus_cut_rows(
    all_tasks: List[Task], blocked_entries: List[Any], *, now: datetime, prefix: str = _LANE_LABEL_PREFIX
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


def _render_focus_panel(rows: List[Dict[str, Any]]) -> str:
    if not rows:
        return "<div class='pulse-empty'>No lane has open P1/P2 or blocked work.</div>"
    head = (
        "<div class='pulse-focus-hd'>"
        "<span></span><span>P1/P2</span><span>stale</span><span>blocked</span>"
        "</div>"
    )
    body = ""
    for r in rows[:8]:
        blocked_cell = (
            f"<span class='pulse-focus-n pulse-focus-n--blocked'>{r['blocked']}</span>"
            if r["blocked"] else
            "<span class='pulse-focus-n pulse-focus-n--zero'>0</span>"
        )
        body += (
            "<div class='pulse-focus-row'>"
            f"<span class='pulse-focus-name'>{_esc(r['lane'])}</span>"
            f"<span class='pulse-focus-n'>{r['open_p1p2']}</span>"
            f"<span class='pulse-focus-n'>{_fmt_hours(r['staleness_hours'])}</span>"
            f"{blocked_cell}"
            "</div>"
        )
    return f"<div class='pulse-side-rows'>{head}{body}</div>"




_AGING_BACKLOG = timedelta(days=7)


def _fmt_minutes(mins: int) -> str:
    """Compact duration like '45m', '3h20m', '2d4h'."""
    if mins < 60:
        return f"{mins}m"
    hrs, m = divmod(mins, 60)
    if hrs < 24:
        return f"{hrs}h{m}m" if m else f"{hrs}h"
    days, h = divmod(hrs, 24)
    return f"{days}d{h}h" if h else f"{days}d"


def _render_next_up_panel(
    ready_tasks: List[Task], *, now: datetime, dot_vars: Dict[str, str]
) -> str:
    """wl-86: what an agent would pick next — the merged ready queue across
    the in-scope stores, priority order. Same eligibility as wl_ready
    (blockers done, not gated)."""
    if not ready_tasks:
        return "<div class='pulse-empty'>Queue clear — nothing ready.</div>"
    rows = ""
    for t in ready_tasks[:6]:
        prod_slug, _ = split_task_id(t.id)
        dot_var = dot_vars.get(prod_slug, "--dim")
        pri_color = _pulse_priority_color(t.priority)
        title = _esc(t.title)[:70] + ("…" if len(t.title) > 70 else "")
        age = _pulse_relative_time(t.created_at, now=now)
        rows += (
            f"<a href='/admin/desk?open={_esc(t.id)}' class='pulse-side-row'>"
            f"<span class='pulse-side-pri' style='color:{pri_color};'>P{int(t.priority or 3)}</span>"
            f"<span class='pulse-side-id'><span class='pulse-ticker-dot' "
            f"style='background:var({dot_var});'></span>#{_esc(t.id)}</span>"
            f"<span class='pulse-side-title'>{title}</span>"
            f"<span class='pulse-side-age'>{age}</span>"
            f"</a>"
        )
    return f"<div class='pulse-side-rows'>{rows}</div>"


def _render_attention_panel(
    inflight_tasks: List[Task],
    blocked_entries: List[Any],
    backlog: List[Task],
    *,
    now: datetime,
    parse_ts,
) -> Tuple[str, int]:
    """wl-86: everything that needs a human — stalled in-flight (§4), blocked
    backlog with unresolved deps, and P1/P2 backlog going stale. Returns
    (html, item count) so the panel head can show the total."""
    items: List[Tuple[str, str, Task, str]] = []  # (tag, color, task, note)
    seen: set = set()
    for t in inflight_tasks:
        ts = parse_ts(t.updated_at)
        if ts is not None and (now - ts) >= _stale_inflight():
            items.append((
                "stale", "#f59e0b", t,
                f"no update {_pulse_relative_time(t.updated_at, now=now)}",
            ))
            seen.add(t.id)
    for bt in blocked_entries:
        ids = ", ".join(f"#{b.ticket_id}" for b in bt.blockers[:2])
        if len(bt.blockers) > 2:
            ids += f" +{len(bt.blockers) - 2}"
        items.append(("blocked", "#ef4444", bt.task, f"waiting on {ids}"))
        seen.add(bt.task.id)
    for t in backlog:
        if t.id in seen or int(t.priority or 3) > 2:
            continue
        ts = parse_ts(t.updated_at) or parse_ts(t.created_at)
        if ts is not None and (now - ts) >= _AGING_BACKLOG:
            items.append((
                "aging", "#38bdf8", t,
                f"idle {_pulse_relative_time(t.updated_at or t.created_at, now=now)}",
            ))
    if not items:
        return "<div class='pulse-empty'>✓ Nothing needs attention.</div>", 0
    rows = ""
    for tag, color, t, note in items[:8]:
        title = _esc(t.title)[:60] + ("…" if len(t.title) > 60 else "")
        rows += (
            f"<a href='/admin/desk?open={_esc(t.id)}' class='pulse-side-row'>"
            f"<span class='pulse-attn-tag' style='color:{color};border-color:{color};'>{tag}</span>"
            f"<span class='pulse-side-id'>#{_esc(t.id)}</span>"
            f"<span class='pulse-side-title'>{title}</span>"
            f"<span class='pulse-side-note'>{_esc(note)}</span>"
            f"</a>"
        )
    return f"<div class='pulse-side-rows'>{rows}</div>", len(items)


# wl-135: founder-attention feed — everything waiting on the founder,
# aggregated across every registered product store, always (not scoped to
# the page's current store, unlike the panels above). Distinct from
# _render_attention_panel (wl-86, scope-local generic staleness): this is
# specifically the founder's five gates — review, decision label, human
# gate, stalled in-flight, date-gated embargo.
_ATTENTION_KIND_META = {
    "in_review": ("in review", "#38bdf8"),
    "founder_decision": ("decision", "#a855f7"),
    "human_gate": ("gated", "#ef4444"),
    "stalled": ("stalled", "#f59e0b"),
    "embargo": ("embargo", "#64748b"),
}



def _render_founder_attention_rows(items: List[Dict[str, Any]], *, limit: Optional[int] = None) -> str:
    """Compact side-panel rows (Overview). Full decision board is
    `_render_attention_page_body`."""
    if not items:
        return "<div class='pulse-empty'>&#10003; Nothing waiting on you.</div>"
    rows = ""
    for it in (items[:limit] if limit else items):
        tag, color = _ATTENTION_KIND_META.get(it["kind"], (it["kind"], "#64748b"))
        title = _esc(it["title"])[:70] + ("…" if len(it["title"]) > 70 else "")
        age = _fmt_minutes(it["age_minutes"]) if it["age_minutes"] is not None else "—"
        rows += (
            f"<a href='{_esc(it['url'])}' class='pulse-side-row' title='{_esc(it['note'])}'>"
            f"<span class='pulse-attn-tag' style='color:{color};border-color:{color};'>{_esc(tag)}</span>"
            f"<span class='pulse-side-id'>#{_esc(it['id'])}</span>"
            f"<span class='pulse-side-title'>{title}</span>"
            f"<span class='pulse-side-age'>{age}</span>"
            f"</a>"
        )
    return f"<div class='pulse-side-rows'>{rows}</div>"


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


def _render_flow_panel(
    all_tasks: List[Task],
    *,
    now: datetime,
    today_start: datetime,
    parse_ts,
    done_today: int,
) -> str:
    """wl-86: intake vs burn — is the backlog growing or shrinking today —
    plus median created→done cycle time over the last 7 days."""
    floor = datetime.min.replace(tzinfo=timezone.utc)
    created_today = len([
        t for t in all_tasks if (parse_ts(t.created_at) or floor) >= today_start
    ])
    net = created_today - done_today
    week_ago = now - timedelta(days=7)
    done_week = 0
    cycle_mins: List[int] = []
    for t in all_tasks:
        if t.status != TaskStatus.DONE:
            continue
        closed = parse_ts(t.updated_at)
        if closed is None or closed < week_ago:
            continue
        done_week += 1
        created = parse_ts(t.created_at)
        if created is not None and closed >= created:
            cycle_mins.append(int((closed - created).total_seconds() // 60))
    if cycle_mins:
        cycle_mins.sort()
        mid = len(cycle_mins) // 2
        med = (cycle_mins[mid] if len(cycle_mins) % 2
               else (cycle_mins[mid - 1] + cycle_mins[mid]) // 2)
        cycle_str = _fmt_minutes(med)
    else:
        cycle_str = "—"
    net_color = "#4caf7d" if net <= 0 else "#f59e0b"
    net_str = f"{net:+d}" if net else "±0"
    return (
        "<div class='pulse-flow'>"
        f"<div class='pulse-flow-stat'><b>{created_today}</b><i>filed today</i></div>"
        f"<div class='pulse-flow-stat'><b>{done_today}</b><i>done today</i></div>"
        f"<div class='pulse-flow-stat'><b style='color:{net_color};'>{net_str}</b><i>net intake</i></div>"
        "</div>"
        "<div class='pulse-flow-rows'>"
        f"<div class='pulse-flow-row'><span>Median cycle · 7d</span><b>{cycle_str}</b></div>"
        f"<div class='pulse-flow-row'><span>Closed · 7d</span><b>{done_week}</b></div>"
        "</div>"
    )


# wl-94: movable/resizable Overview panels — drag handle + width/height
# toggles shared by every panel-head; layout state lives client-side only
# (localStorage), so no server round-trip on rearrange.
_PANEL_DRAG_HANDLE = (
    "<span class='pulse-panel-drag' title='Drag to reorder'>&#10241;</span>"
)
_PANEL_CONTROLS = (
    "<span class='pulse-panel-controls'>"
    "<button type='button' class='pulse-panel-btn pulse-panel-width' "
    "title='Toggle width (side/main/full)'>&#8596;</button>"
    "<button type='button' class='pulse-panel-btn pulse-panel-height' "
    "title='Toggle height (normal/compact)'>&#8597;</button>"
    "</span>"
)


def _pulse_layout_js(scope: str) -> str:
    """wl-94: client-only panel layout (order/column/height) for the Overview
    grid. Panels are server-rendered; this only moves the CSS `order` /
    `grid-column` of existing `[data-panel-id]` nodes and persists the
    result to localStorage — no server round-trip, survives the 30s
    auto-reload (wl-theme pattern: scoped key, plain get/setItem).
    """
    script = r"""
<script>
(function() {
  var KEY = 'wl-overview-layout:__SCOPE__';
  var COLMAP = { main: '1', side: '2', full: '1 / -1' };
  var WIDTH_CYCLE = ['side', 'main', 'full'];
  var grid = document.querySelector('.pulse-grid');
  if (!grid) return;
  var panels = Array.prototype.slice.call(grid.querySelectorAll('[data-panel-id]'));

  function loadLayout() {
    try { return JSON.parse(localStorage.getItem(KEY) || '{}'); } catch (e) { return {}; }
  }
  function saveLayout(layout) {
    try { localStorage.setItem(KEY, JSON.stringify(layout)); } catch (e) {}
  }
  function domOrder() {
    return panels.map(function(p) { return p.getAttribute('data-panel-id'); });
  }
  function applyLayout(layout) {
    var order = (layout.order || []).filter(function(id) {
      return panels.some(function(p) { return p.getAttribute('data-panel-id') === id; });
    });
    domOrder().forEach(function(id) { if (order.indexOf(id) === -1) order.push(id); });
    panels.forEach(function(p) {
      var id = p.getAttribute('data-panel-id');
      p.style.order = String(order.indexOf(id));
      var col = (layout.column && layout.column[id]) || p.getAttribute('data-default-col');
      p.style.gridColumn = COLMAP[col] || COLMAP[p.getAttribute('data-default-col')];
      p.setAttribute('data-col', col);
      var h = (layout.height && layout.height[id]) || 'normal';
      p.classList.toggle('pulse-panel--compact', h === 'compact');
      p.setAttribute('data-height', h);
    });
  }
  function currentOrder() {
    return panels.slice().sort(function(a, b) {
      return parseInt(a.style.order || '0', 10) - parseInt(b.style.order || '0', 10);
    }).map(function(p) { return p.getAttribute('data-panel-id'); });
  }
  function mutateLayout(fn) {
    var layout = loadLayout();
    layout.order = layout.order && layout.order.length ? layout.order : currentOrder();
    layout.column = layout.column || {};
    layout.height = layout.height || {};
    fn(layout);
    saveLayout(layout);
    applyLayout(layout);
  }

  applyLayout(loadLayout());

  var dragId = null;
  panels.forEach(function(p) {
    var id = p.getAttribute('data-panel-id');
    var handle = p.querySelector('.pulse-panel-drag');
    if (handle) {
      p.setAttribute('draggable', 'true');
      p.addEventListener('dragstart', function(e) {
        dragId = id;
        if (e.dataTransfer) e.dataTransfer.effectAllowed = 'move';
        p.classList.add('pulse-panel--dragging');
      });
      p.addEventListener('dragend', function() {
        p.classList.remove('pulse-panel--dragging');
      });
    }
    p.addEventListener('dragover', function(e) {
      if (!dragId) return;
      e.preventDefault();
      if (e.dataTransfer) e.dataTransfer.dropEffect = 'move';
      p.classList.add('pulse-panel--drop-target');
    });
    p.addEventListener('dragleave', function() {
      p.classList.remove('pulse-panel--drop-target');
    });
    p.addEventListener('drop', function(e) {
      e.preventDefault();
      p.classList.remove('pulse-panel--drop-target');
      var targetId = id;
      if (!dragId || dragId === targetId) { dragId = null; return; }
      mutateLayout(function(layout) {
        var order = currentOrder();
        var from = order.indexOf(dragId);
        var to = order.indexOf(targetId);
        if (from !== -1 && to !== -1) {
          order.splice(from, 1);
          order.splice(order.indexOf(targetId), 0, dragId);
        }
        layout.order = order;
        layout.column[dragId] = p.getAttribute('data-col') || p.getAttribute('data-default-col');
      });
      dragId = null;
    });

    var widthBtn = p.querySelector('.pulse-panel-width');
    if (widthBtn) widthBtn.addEventListener('click', function() {
      mutateLayout(function(layout) {
        var cur = p.getAttribute('data-col') || p.getAttribute('data-default-col');
        var next = WIDTH_CYCLE[(WIDTH_CYCLE.indexOf(cur) + 1) % WIDTH_CYCLE.length];
        layout.column[id] = next;
      });
    });

    var heightBtn = p.querySelector('.pulse-panel-height');
    if (heightBtn) heightBtn.addEventListener('click', function() {
      mutateLayout(function(layout) {
        var cur = p.getAttribute('data-height') || 'normal';
        layout.height[id] = cur === 'compact' ? 'normal' : 'compact';
      });
    });
  });

  var resetBtn = document.getElementById('pulse-reset-layout');
  if (resetBtn) resetBtn.addEventListener('click', function() {
    try { localStorage.removeItem(KEY); } catch (e) {}
    applyLayout({});
  });
})();
</script>
"""
    return script.replace("__SCOPE__", (scope or "all").replace("'", ""))


def _render_pulse_page(scope: str = "", window_days: int = 14) -> str:
    """Live metrics strip — 'what's happening in the in-scope stores right now'.

    Auto-refreshes every 6s. Dark, monospace, neon-accented. No user input
    besides the Allocation panel's window selector (wl-106; ?days= query
    param on this same route).
    """
    all_tasks = _merged_scope_tasks_for_filters(scope)

    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_ago = now - timedelta(hours=24)

    in_progress = [t for t in all_tasks if t.status == TaskStatus.IN_PROGRESS]
    in_review = [t for t in all_tasks if t.status == TaskStatus.IN_REVIEW]
    backlog = [t for t in all_tasks if t.status == TaskStatus.BACKLOG]

    done_today = [t for t in all_tasks if t.status == TaskStatus.DONE
                  and (_parse_task_date_utc(t.updated_at) or now) >= today_start]

    # Throughput sparkline — done tasks per hour over last 24h (24 buckets)
    hourly_buckets = [0] * 24
    for t in all_tasks:
        if t.status != TaskStatus.DONE:
            continue
        ts = _parse_task_date_utc(t.updated_at)
        if ts is None or ts < day_ago:
            continue
        hours_ago = int((now - ts).total_seconds() // 3600)
        if 0 <= hours_ago < 24:
            hourly_buckets[23 - hours_ago] += 1
    spark_max = max(hourly_buckets) or 1
    spark_bars = "".join(
        f"<span class='pulse-spark-bar' style='height:{max(4, int(40 * n / spark_max))}px;' "
        f"title='{n} done · {24 - i}h ago'></span>"
        for i, n in enumerate(hourly_buckets)
    )

    # Activity ticker — last 20 tasks by updated_at, any status
    sorted_recent = sorted(
        all_tasks,
        key=lambda t: _parse_task_date_utc(t.updated_at) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )[:20]

    # In-flight cards
    inflight_tasks = sorted(
        in_progress + in_review,
        key=lambda t: (0 if t.status == TaskStatus.IN_PROGRESS else 1,
                       _parse_task_date_utc(t.updated_at) or datetime.min.replace(tzinfo=timezone.utc)),
    )

    def _age(t: Task) -> str:
        return _pulse_relative_time(t.updated_at or t.created_at, now=now)

    def _priority_label(p: int) -> str:
        return {1: "P1", 2: "P2", 3: "P3", 4: "P4"}.get(int(p or 3), "P3")

    dot_vars = _fleet_dot_vars_by_slug()
    inflight_previews = _load_preview_comments_multi(
        product_trackers(), inflight_tasks
    )
    agents_html = _render_active_agents_panel(
        inflight_tasks, now=now, previews=inflight_previews
    )

    # wl-86: ready + blocked across the in-scope stores (one WorkQueue per
    # store — dependency checks only resolve within a store).
    ready_tasks: List[Task] = []
    blocked_entries: List[Any] = []
    for _spec, tracker in _scoped_product_trackers(scope):
        try:
            q = WorkQueue(tracker)
            ready_tasks.extend(q.ready())
            blocked_entries.extend(q.blocked())
        except Exception:
            continue
    ready_tasks.sort(key=lambda t: (int(t.priority or 3), t.created_at or ""))

    next_up_html = _render_next_up_panel(ready_tasks, now=now, dot_vars=dot_vars)
    attention_html, attention_count = _render_attention_panel(
        inflight_tasks, blocked_entries, backlog, now=now, parse_ts=_parse_ts,
    )
    # wl-135: founder-attention feed is always all-store — a store-scoped
    # Pulse page still shows the full "waiting on you" picture, matching the
    # header chip and /admin/attention.
    founder_attention_all = _collect_founder_attention_items(now=now)
    founder_attention_items, _hid, _sn = _partition_attention_items(
        founder_attention_all, now=now)
    founder_attention_html = _render_founder_attention_rows(founder_attention_items, limit=8)
    # wl-89: former cockpit analytics, re-homed as themed panels.
    breakdown_html, breakdown_meta, activity14_html = _render_breakdown_panels(
        scope, all_tasks,
    )
    health_html = _render_service_health_body(scope)
    # wl-93: per-author scoreboard from signed comments.
    # wl-95: pending = in-flight tickets whose latest Owner: marker is theirs.
    pending_by_owner: Dict[str, int] = {}
    for t in inflight_tasks:
        owner = (inflight_previews.get(t.id) or {}).get("owner") or ""
        if owner:
            pending_by_owner[owner] = pending_by_owner.get(owner, 0) + 1
    author_tallies = _author_tally(scope, pending=pending_by_owner)
    authors_html = _render_author_tally_panel(author_tallies, now=now)
    # wl-100: forward-looking queue depth per lane:* label.
    lane_rows = _lane_lens_rows(all_tasks)
    lanelens_html = _render_lane_lens_panel(lane_rows)
    # wl-106: filed-vs-closed per lane/author over a selectable window, plus
    # an all-time totals row reconciling with wl_counts.
    alloc_since = now - timedelta(days=window_days)
    alloc_lane_rows = _allocation_lane_rows(all_tasks, alloc_since)
    alloc_author_rows = _allocation_author_rows(scope, alloc_since)
    alloc_totals = _status_totals(all_tasks)
    allocation_html = _render_allocation_panel(
        alloc_lane_rows, alloc_author_rows, alloc_totals,
        scope=scope, window_days=window_days,
    )
    # wl-107: cycle-time (closed-in-window) and age (currently-open) medians/
    # p90s per lane and per priority — where work stalls.
    cycle_lane_rows = _cycle_age_lane_rows(all_tasks, alloc_since, now)
    cycle_priority_rows = _cycle_age_priority_rows(all_tasks, alloc_since, now)
    cycle_age_html = _render_cycle_age_panel(
        cycle_lane_rows, cycle_priority_rows, window_days=window_days,
    )
    # wl-107: founder-session prep — lanes ranked by open P1/P2 x staleness x
    # blocked-status (blocked_entries already computed above for Attention).
    focus_rows = _focus_cut_rows(all_tasks, blocked_entries, now=now)
    focus_html = _render_focus_panel(focus_rows)

    # Metrics strip
    avg_inflight_age = ""
    if inflight_tasks:
        ages = []
        for t in inflight_tasks:
            ts = _parse_ts(t.updated_at)
            if ts:
                ages.append(int((now - ts).total_seconds() // 60))
        if ages:
            avg_min = sum(ages) // len(ages)
            avg_inflight_age = f"{avg_min}m" if avg_min < 60 else f"{avg_min // 60}h{avg_min % 60}m"

    thr_24h = sum(hourly_buckets)

    metrics_html = (
        "<div class='pulse-metrics'>"
        f"<div class='pulse-metric'><div class='pulse-metric-val' style='color:#f97316;'>"
        f"{len(in_progress)}<span class='pulse-dot pulse-dot--live'></span></div>"
        f"<div class='pulse-metric-lbl'>In progress</div></div>"
        f"<div class='pulse-metric'><div class='pulse-metric-val' style='color:#7a8fa3;'>{len(in_review)}</div>"
        f"<div class='pulse-metric-lbl'>In review</div></div>"
        f"<div class='pulse-metric'><div class='pulse-metric-val' style='color:#4caf7d;'>{len(done_today)}</div>"
        f"<div class='pulse-metric-lbl'>Done today</div></div>"
        f"<div class='pulse-metric'><div class='pulse-metric-val'>{thr_24h}</div>"
        f"<div class='pulse-metric-lbl'>Done · 24h</div></div>"
        f"<div class='pulse-metric'><div class='pulse-metric-val' style='color:#38bdf8;'>{len(ready_tasks)}</div>"
        f"<div class='pulse-metric-lbl'>Ready</div></div>"
        f"<div class='pulse-metric'><div class='pulse-metric-val'>{len(backlog)}</div>"
        f"<div class='pulse-metric-lbl'>Backlog</div></div>"
        f"<div class='pulse-metric'><div class='pulse-metric-val'>{avg_inflight_age or '—'}</div>"
        f"<div class='pulse-metric-lbl'>Avg in-flight age</div></div>"
        "</div>"
    )

    flow_html = _render_flow_panel(
        all_tasks, now=now, today_start=today_start,
        parse_ts=_parse_ts, done_today=len(done_today),
    )

    # In-flight cards
    _live_dot = "<span class='pulse-dot pulse-dot--live'></span>"
    if inflight_tasks:
        cards_html = ""
        for t in inflight_tasks:
            status_color = _pulse_status_color(t.status)
            pri_color = _pulse_priority_color(t.priority)
            status_lbl = _STATUS_LABELS.get(t.status, t.status)
            pulse_cls = " pulse-card--active" if t.status == TaskStatus.IN_PROGRESS else ""
            status_prefix = _live_dot if t.status == TaskStatus.IN_PROGRESS else ""
            title = _esc(t.title)[:90] + ("…" if len(t.title) > 90 else "")
            cards_html += (
                f"<a href='/admin/desk?open={_esc(t.id)}' class='pulse-card{pulse_cls}'>"
                f"<div class='pulse-card-head'>"
                f"<span class='pulse-id'>#{_esc(t.id)}</span>"
                f"<span class='pulse-pri' style='color:{pri_color};'>{_priority_label(t.priority)}</span>"
                f"<span class='pulse-status' style='color:{status_color};'>"
                f"{status_prefix}{_esc(status_lbl)}</span>"
                f"<span class='pulse-age'>{_age(t)}</span>"
                f"</div>"
                f"<div class='pulse-card-title'>{title}</div>"
                f"</a>"
            )
    else:
        cards_html = "<div class='pulse-empty'>⚙ No tickets in flight. Queue is idle.</div>"

    # Activity ticker
    ticker_items = ""
    for t in sorted_recent:
        status_color = _pulse_status_color(t.status)
        pri_color = _pulse_priority_color(t.priority)
        title = _esc(t.title)[:70] + ("…" if len(t.title) > 70 else "")
        prod_slug, _ = split_task_id(t.id)
        dot_var = dot_vars.get(prod_slug, "--dim")
        ticker_items += (
            f"<a href='/admin/desk?open={_esc(t.id)}' class='pulse-ticker-row'>"
            f"<span class='pulse-ticker-age'>{_age(t)}</span>"
            f"<span class='pulse-ticker-status' style='color:{status_color};'>{_esc(_STATUS_LABELS.get(t.status, t.status))}</span>"
            f"<span class='pulse-ticker-id'><span class='pulse-ticker-dot' style='background:var({dot_var});'></span>#{_esc(t.id)}</span>"
            f"<span class='pulse-ticker-pri' style='color:{pri_color};'>{_priority_label(t.priority)}</span>"
            f"<span class='pulse-ticker-title'>{title}</span>"
            f"</a>"
        )

    # Throughput sparkline card
    spark_html = (
        "<div class='pulse-spark'>"
        f"<div class='pulse-spark-head'><span>Done / hour · last 24h</span>"
        f"<span class='pulse-spark-total'>Σ {thr_24h}</span></div>"
        f"<div class='pulse-spark-bars'>{spark_bars}</div>"
        f"<div class='pulse-spark-axis'><span>24h</span><span>12h</span><span>now</span></div>"
        "</div>"
    )

    last_updated = now.strftime("%H:%M:%S UTC")

    body = f"""
    <div class='pulse-page'>
      <div class='pulse-header'>
        <div class='pulse-title'>
          <span class='pulse-dot pulse-dot--live pulse-dot--lg'></span>
          <span class='pulse-title-main'>OVERVIEW</span>
          <span class='pulse-title-sub'>· live operator view</span>
        </div>
        <div class='pulse-stamp'>
          <span class='dim'>updated</span>
          <span class='pulse-stamp-val' id='pulse-updated'>{last_updated}</span>
          <span class='dim'>· auto-refresh 30s</span>
          <button type='button' id='pulse-reset-layout' class='pulse-panel-btn' title='Reset panel layout to default'>reset layout</button>
        </div>
      </div>

      {metrics_html}

      <div class='pulse-grid'>
          <div class='pulse-panel' data-panel-id='inflight' data-default-col='main'>
            <div class='pulse-panel-head'>
              {_PANEL_DRAG_HANDLE}
              <span class='pulse-panel-title'>In flight</span>
              <span class='pulse-panel-meta'>{len(in_progress)} active · {len(in_review)} reserved</span>
              {_PANEL_CONTROLS}
            </div>
            <div class='pulse-cards'>{cards_html}</div>
          </div>

          <div class='pulse-panel' data-panel-id='activity' data-default-col='main'>
            <div class='pulse-panel-head'>
              {_PANEL_DRAG_HANDLE}
              <span class='pulse-panel-title'>Activity · last 20 updates</span>
              {_PANEL_CONTROLS}
            </div>
            <div class='pulse-ticker'>{ticker_items}</div>
          </div>

          <div class='pulse-panel' data-panel-id='breakdown' data-default-col='main'>
            <div class='pulse-panel-head'>
              {_PANEL_DRAG_HANDLE}
              <span class='pulse-panel-title'>Breakdown</span>
              <span class='pulse-panel-meta'>{breakdown_meta}</span>
              {_PANEL_CONTROLS}
            </div>
            {breakdown_html}
          </div>

          <div class='pulse-panel' data-panel-id='recent14' data-default-col='main'>
            <div class='pulse-panel-head'>
              {_PANEL_DRAG_HANDLE}
              <span class='pulse-panel-title'>Recent activity · 14 days</span>
              {_PANEL_CONTROLS}
            </div>
            {activity14_html}
          </div>

          <div class='pulse-panel' data-panel-id='throughput' data-default-col='side'>
            <div class='pulse-panel-head'>
              {_PANEL_DRAG_HANDLE}
              <span class='pulse-panel-title'>Throughput</span>
              {_PANEL_CONTROLS}
            </div>
            {spark_html}
          </div>

          <div class='pulse-panel' data-panel-id='nextup' data-default-col='side'>
            <div class='pulse-panel-head'>
              {_PANEL_DRAG_HANDLE}
              <span class='pulse-panel-title'>Next up</span>
              <span class='pulse-panel-meta'>{len(ready_tasks)} ready</span>
              {_PANEL_CONTROLS}
            </div>
            {next_up_html}
          </div>

          <div class='pulse-panel' data-panel-id='lanelens' data-default-col='side'>
            <div class='pulse-panel-head'>
              {_PANEL_DRAG_HANDLE}
              <span class='pulse-panel-title'>Lane lens</span>
              <span class='pulse-panel-meta'>{len(lane_rows)} lane{'' if len(lane_rows) == 1 else 's'}</span>
              {_PANEL_CONTROLS}
            </div>
            {lanelens_html}
          </div>

          <div class='pulse-panel' data-panel-id='allocation' data-default-col='main'>
            <div class='pulse-panel-head'>
              {_PANEL_DRAG_HANDLE}
              <span class='pulse-panel-title'>Allocation · {window_days}d</span>
              <span class='pulse-panel-meta'>{len(alloc_lane_rows)} lane{'' if len(alloc_lane_rows) == 1 else 's'} · {len(alloc_author_rows)} author{'' if len(alloc_author_rows) == 1 else 's'}</span>
              {_PANEL_CONTROLS}
            </div>
            {allocation_html}
          </div>

          <div class='pulse-panel' data-panel-id='cycleage' data-default-col='main'>
            <div class='pulse-panel-head'>
              {_PANEL_DRAG_HANDLE}
              <span class='pulse-panel-title'>Cycle &amp; age · {window_days}d</span>
              <span class='pulse-panel-meta'>{len(cycle_lane_rows)} lane{'' if len(cycle_lane_rows) == 1 else 's'}</span>
              {_PANEL_CONTROLS}
            </div>
            {cycle_age_html}
          </div>

          <div class='pulse-panel' data-panel-id='focus' data-default-col='side'>
            <div class='pulse-panel-head'>
              {_PANEL_DRAG_HANDLE}
              <span class='pulse-panel-title'>Focus cut</span>
              <span class='pulse-panel-meta'>{len(focus_rows)} lane{'' if len(focus_rows) == 1 else 's'}</span>
              {_PANEL_CONTROLS}
            </div>
            {focus_html}
          </div>

          <div class='pulse-panel' data-panel-id='founder_attention' data-default-col='side'>
            <div class='pulse-panel-head'>
              {_PANEL_DRAG_HANDLE}
              <span class='pulse-panel-title'>needs You</span>
              <span class='pulse-panel-meta'><a href='/admin/attention' class='dim'>{len(founder_attention_items) or 'clear'} &rarr;</a></span>
              {_PANEL_CONTROLS}
            </div>
            {founder_attention_html}
          </div>

          <div class='pulse-panel' data-panel-id='attention' data-default-col='side'>
            <div class='pulse-panel-head'>
              {_PANEL_DRAG_HANDLE}
              <span class='pulse-panel-title'>Attention</span>
              <span class='pulse-panel-meta'>{attention_count or 'clear'}</span>
              {_PANEL_CONTROLS}
            </div>
            {attention_html}
          </div>

          <div class='pulse-panel' data-panel-id='flow' data-default-col='side'>
            <div class='pulse-panel-head'>
              {_PANEL_DRAG_HANDLE}
              <span class='pulse-panel-title'>Flow · 7 days</span>
              {_PANEL_CONTROLS}
            </div>
            {flow_html}
          </div>

          <div class='pulse-panel' data-panel-id='agents' data-default-col='side'>
            <div class='pulse-panel-head'>
              {_PANEL_DRAG_HANDLE}
              <span class='pulse-panel-title'>In-flight claims</span>
              {_PANEL_CONTROLS}
            </div>
            {agents_html}
          </div>

          <div class='pulse-panel' data-panel-id='authors' data-default-col='side'>
            <div class='pulse-panel-head'>
              {_PANEL_DRAG_HANDLE}
              <span class='pulse-panel-title'>Authors · tickets worked</span>
              <span class='pulse-panel-meta'>{len(author_tallies)} signed</span>
              {_PANEL_CONTROLS}
            </div>
            {authors_html}
          </div>

          <div class='pulse-panel' data-panel-id='health' data-default-col='side'>
            <div class='pulse-panel-head'>
              {_PANEL_DRAG_HANDLE}
              <span class='pulse-panel-title'>Service health</span>
              {_PANEL_CONTROLS}
            </div>
            {health_html}
          </div>
      </div>
    </div>

    <style>
      .pulse-page {{ padding: 16px 20px; color: var(--fg, #eee); font-family: ui-monospace, 'SF Mono', Menlo, monospace; }}
      .pulse-header {{ display:flex; align-items:center; justify-content:space-between;
        border-bottom:1px solid var(--border); padding-bottom:12px; margin-bottom:16px; }}
      .pulse-title {{ display:flex; align-items:center; gap:10px; }}
      .pulse-title-main {{ font-size:22px; font-weight:700; letter-spacing:0.12em; color:var(--neon,#e8622c); }}
      .pulse-title-sub {{ font-size:12px; color:var(--dim); letter-spacing:0.05em; text-transform:uppercase; }}
      .pulse-stamp {{ font-size:11px; display:flex; gap:6px; align-items:center; }}
      .pulse-stamp-val {{ font-weight:600; color:var(--fg); }}
      .pulse-dot {{ display:inline-block; width:6px; height:6px; border-radius:50%; background:#4caf7d;
        box-shadow:0 0 4px #4caf7d; margin-right:4px; }}
      .pulse-dot--live {{ background:#4caf7d; animation: pulse-blink 1.4s ease-in-out infinite; }}
      .pulse-dot--lg {{ width:10px; height:10px; }}
      @keyframes pulse-blink {{ 0%, 100% {{ opacity:1; box-shadow:0 0 6px #4caf7d; }} 50% {{ opacity:0.35; box-shadow:0 0 2px #4caf7d; }} }}

      .pulse-metrics {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(120px, 1fr));
        gap:8px; margin-bottom:20px; }}
      .pulse-metric {{ padding:12px 14px; background:var(--bg2, #211d16); border:1px solid var(--border);
        border-radius:6px; text-align:left; }}
      .pulse-metric-val {{ font-size:24px; font-weight:700; line-height:1; display:flex; align-items:center; gap:6px; }}
      .pulse-metric-lbl {{ font-size:10px; text-transform:uppercase; letter-spacing:0.1em; color:var(--dim); margin-top:6px; }}

      /* wl-86/94: panels are direct grid children (not fixed-column wrappers)
         so wl-94's layout JS can move any panel between main/side/full via
         an inline grid-column + order — every cell filled, no half-empty
         auto-fit rows. Collapses to one column on narrow viewports. */
      .pulse-grid {{ display:grid; grid-template-columns:minmax(0, 3fr) minmax(0, 2fr);
        gap:12px; align-items:start; }}
      @media (max-width: 960px) {{ .pulse-grid {{ grid-template-columns:1fr; }}
        .pulse-grid > .pulse-panel {{ grid-column:1 !important; }} }}
      .pulse-panel {{ background:var(--bg2, #211d16); border:1px solid var(--border); border-radius:6px;
        padding:12px 14px; grid-column:1; }}
      .pulse-panel-head {{ display:flex; align-items:baseline; gap:8px;
        padding-bottom:8px; margin-bottom:10px; border-bottom:1px dashed var(--border); }}
      .pulse-panel-title {{ font-size:11px; text-transform:uppercase; letter-spacing:0.12em; color:var(--dim); font-weight:600; }}
      .pulse-panel-meta {{ font-size:11px; color:var(--dim); }}

      /* wl-94: drag handle + width/height toggles, shared across all panels */
      .pulse-panel-drag {{ cursor:grab; color:var(--dim); font-size:13px; line-height:1; flex:none; }}
      .pulse-panel[draggable='true']:active .pulse-panel-drag {{ cursor:grabbing; }}
      .pulse-panel--dragging {{ opacity:0.4; }}
      .pulse-panel--drop-target {{ outline:1px dashed var(--neon); outline-offset:2px; }}
      .pulse-panel-controls {{ margin-left:auto; display:flex; gap:4px; align-items:center; flex:none; }}
      .pulse-panel-btn {{ background:transparent; border:1px solid var(--border); border-radius:4px;
        color:var(--dim); font-size:11px; line-height:1; padding:2px 5px; cursor:pointer; }}
      .pulse-panel-btn:hover {{ color:var(--fg); border-color:var(--neon); }}
      .pulse-panel--compact {{ max-height:220px; overflow-y:auto; }}

      .pulse-cards {{ display:flex; flex-direction:column; gap:6px; }}
      .pulse-card {{ display:block; padding:8px 10px; background:rgba(250,250,249,0.03); border:1px solid var(--border);
        border-left:3px solid #64748b; border-radius:4px; text-decoration:none; color:var(--fg);
        transition:background .12s, border-color .12s; }}
      .pulse-card:hover {{ background:rgba(232,98,44,0.06); border-color:var(--neon); }}
      .pulse-card--active {{ border-left-color:#f97316; background:rgba(249,115,22,0.05); }}
      .pulse-card-head {{ display:flex; gap:10px; align-items:baseline; font-size:11px; }}
      .pulse-id {{ color:var(--dim); font-weight:600; }}
      .pulse-pri {{ font-weight:700; font-size:10px; letter-spacing:0.05em; }}
      .pulse-status {{ font-weight:600; text-transform:uppercase; letter-spacing:0.05em; font-size:10px; }}
      .pulse-age {{ margin-left:auto; color:var(--dim); font-variant-numeric:tabular-nums; }}
      .pulse-card-title {{ font-size:13px; color:var(--fg); margin-top:4px; line-height:1.35; }}

      .pulse-ticker {{ display:flex; flex-direction:column; gap:3px; max-height:340px; overflow-y:auto; }}
      .pulse-ticker-row {{ display:grid; grid-template-columns:44px 90px 60px 40px 1fr; gap:8px;
        padding:4px 6px; font-size:11px; text-decoration:none; color:var(--fg); align-items:baseline;
        border-left:2px solid transparent; }}
      .pulse-ticker-row:hover {{ background:rgba(232,98,44,0.06); border-left-color:var(--neon); }}
      .pulse-ticker-age {{ color:var(--dim); font-variant-numeric:tabular-nums; }}
      .pulse-ticker-status {{ text-transform:uppercase; letter-spacing:0.05em; font-weight:600; font-size:10px; }}
      .pulse-ticker-id {{ color:var(--dim); font-weight:600; display:flex; align-items:center; gap:5px; }}
      .pulse-ticker-dot {{ display:inline-block; width:6px; height:6px; border-radius:50%; flex:none; }}
      .pulse-ticker-pri {{ font-weight:700; font-size:10px; }}
      .pulse-ticker-title {{ color:var(--fg); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}

      /* wl-29: active agents — worker -> ticket -> age, from latest comment */
      .agent-rows {{ display:flex; flex-direction:column; gap:5px; max-height:260px; overflow-y:auto; }}
      .agent-row {{ display:flex; align-items:baseline; gap:6px; padding:3px 4px; font-size:11px;
        text-decoration:none; color:var(--fg); border-left:2px solid transparent; }}
      .agent-row:hover {{ background:rgba(232,98,44,0.06); border-left-color:var(--neon); }}
      .agent-icon {{ flex:none; }}
      .agent-label {{ font-weight:600; }}
      .agent-arrow {{ color:var(--dim); }}
      .agent-id {{ color:var(--dim); font-weight:600; }}
      .agent-age {{ margin-left:auto; color:var(--dim); font-variant-numeric:tabular-nums; }}

      .pulse-spark {{ display:flex; flex-direction:column; gap:8px; }}
      .pulse-spark-head {{ display:flex; justify-content:space-between; font-size:11px; color:var(--dim); }}
      .pulse-spark-total {{ color:var(--fg); font-weight:700; }}
      .pulse-spark-bars {{ display:flex; align-items:flex-end; gap:2px; height:50px;
        border-bottom:1px solid var(--border); padding-bottom:1px; }}
      .pulse-spark-bar {{ flex:1; background:linear-gradient(180deg, var(--neon,#e8622c), rgba(232,98,44,0.4));
        border-radius:1px 1px 0 0; min-height:2px; transition:height .3s; }}
      .pulse-spark-axis {{ display:flex; justify-content:space-between; font-size:9px; color:var(--dim);
        text-transform:uppercase; letter-spacing:0.08em; }}

      /* wl-86: side-column rows (Next up, Attention) */
      .pulse-side-rows {{ display:flex; flex-direction:column; gap:3px; }}
      .pulse-side-row {{ display:grid; grid-template-columns:auto auto minmax(0, 1fr) auto; gap:8px;
        padding:4px 6px; font-size:11px; text-decoration:none; color:var(--fg); align-items:baseline;
        border-left:2px solid transparent; }}
      .pulse-side-row:hover {{ background:rgba(232,98,44,0.06); border-left-color:var(--neon); }}
      .pulse-side-pri {{ font-weight:700; font-size:10px; letter-spacing:0.05em; }}
      .pulse-side-id {{ color:var(--dim); font-weight:600; display:flex; align-items:center; gap:5px;
        white-space:nowrap; }}
      .pulse-side-title {{ overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
      .pulse-side-age, .pulse-side-note {{ color:var(--dim); font-variant-numeric:tabular-nums;
        white-space:nowrap; text-align:right; }}
      .pulse-attn-tag {{ font-size:9px; font-weight:700; text-transform:uppercase;
        letter-spacing:0.08em; border:1px solid; border-radius:3px; padding:1px 4px; line-height:1.2; }}

      /* wl-86: flow panel — intake vs burn + cycle time */
      .pulse-flow {{ display:grid; grid-template-columns:repeat(3, 1fr); gap:8px; margin-bottom:10px; }}
      .pulse-flow-stat b {{ display:block; font-size:20px; font-weight:700; line-height:1; }}
      .pulse-flow-stat i {{ font-style:normal; font-size:9px; text-transform:uppercase;
        letter-spacing:0.08em; color:var(--dim); display:block; margin-top:4px; }}
      .pulse-flow-rows {{ display:flex; flex-direction:column; gap:4px;
        border-top:1px dashed var(--border); padding-top:8px; }}
      .pulse-flow-row {{ display:flex; justify-content:space-between; font-size:11px; }}
      .pulse-flow-row span {{ color:var(--dim); }}

      /* wl-93: author scoreboard — worked · closed · pending (wl-95) · last */
      .pulse-authors-hd, .pulse-author-row {{ display:grid;
        grid-template-columns:minmax(0, 1fr) 52px 52px 52px 44px; gap:8px;
        align-items:baseline; padding:3px 6px; font-size:11px; }}
      .pulse-authors-hd {{ font-size:9px; text-transform:uppercase; letter-spacing:0.08em;
        color:var(--dim); padding-bottom:5px; border-bottom:1px dashed var(--border); }}
      .pulse-authors-hd span, .pulse-author-n, .pulse-author-age {{ text-align:right; }}
      .pulse-authors-hd span:first-child {{ text-align:left; }}
      .pulse-author-name {{ font-weight:600; overflow:hidden; text-overflow:ellipsis;
        white-space:nowrap; }}
      .pulse-author-n {{ font-variant-numeric:tabular-nums; font-weight:700; }}
      .pulse-author-n--closed {{ color:#4caf7d; }}
      .pulse-author-n--pending {{ color:#f97316; }}
      .pulse-author-n--zero {{ color:var(--dim); font-weight:400; }}
      .pulse-author-age {{ color:var(--dim); font-variant-numeric:tabular-nums; }}

      /* wl-100: lane lens — queue depth per lane:* label, backlog · gated · in-flight */
      .pulse-lane-hd, .pulse-lane-row {{ display:grid;
        grid-template-columns:minmax(0, 1fr) 56px 56px 64px; gap:8px;
        align-items:baseline; padding:3px 6px; font-size:11px; }}
      .pulse-lane-hd {{ font-size:9px; text-transform:uppercase; letter-spacing:0.08em;
        color:var(--dim); padding-bottom:5px; border-bottom:1px dashed var(--border); }}
      .pulse-lane-hd span, .pulse-lane-n {{ text-align:right; }}
      .pulse-lane-hd span:first-child {{ text-align:left; }}
      .pulse-lane-name {{ font-weight:600; overflow:hidden; text-overflow:ellipsis;
        white-space:nowrap; }}
      .pulse-lane-n {{ font-variant-numeric:tabular-nums; font-weight:700; }}
      .pulse-lane-n--gated {{ color:#f59e0b; }}
      .pulse-lane-n--inflight {{ color:#f97316; }}
      .pulse-lane-n--zero {{ color:var(--dim); font-weight:400; }}

      /* wl-106: allocation view — filed-vs-closed per lane/author + totals */
      .pulse-alloc-selector {{ display:flex; gap:6px; margin-bottom:10px; }}
      .pulse-alloc-seg {{ font-size:10px; font-weight:700; letter-spacing:0.05em;
        color:var(--dim); text-decoration:none; padding:2px 8px; border:1px solid var(--border);
        border-radius:3px; }}
      .pulse-alloc-seg:hover {{ color:var(--fg); }}
      .pulse-alloc-seg--on {{ color:var(--fg); border-color:var(--neon); background:rgba(232,98,44,0.08); }}
      .pulse-alloc-section {{ margin-bottom:12px; }}
      .pulse-alloc-label {{ font-size:10px; text-transform:uppercase; letter-spacing:0.1em;
        color:var(--dim); font-weight:600; margin-bottom:6px; }}
      .pulse-alloc-hd, .pulse-alloc-row {{ display:grid;
        grid-template-columns:minmax(0, 1fr) 52px 52px; gap:8px;
        align-items:baseline; padding:3px 6px; font-size:11px; }}
      .pulse-alloc-hd {{ font-size:9px; text-transform:uppercase; letter-spacing:0.08em;
        color:var(--dim); padding-bottom:5px; border-bottom:1px dashed var(--border); }}
      .pulse-alloc-hd span, .pulse-alloc-n {{ text-align:right; }}
      .pulse-alloc-hd span:first-child {{ text-align:left; }}
      .pulse-alloc-name {{ font-weight:600; overflow:hidden; text-overflow:ellipsis;
        white-space:nowrap; }}
      .pulse-alloc-n {{ font-variant-numeric:tabular-nums; font-weight:700; }}
      .pulse-alloc-n--closed {{ color:#4caf7d; }}
      .pulse-alloc-totals {{ display:flex; flex-direction:column; gap:3px;
        border-top:1px dashed var(--border); padding-top:8px; font-size:11px; }}
      .pulse-alloc-totals-label {{ color:var(--dim); font-size:9px; text-transform:uppercase;
        letter-spacing:0.08em; }}
      .pulse-alloc-totals-row {{ font-variant-numeric:tabular-nums; }}
      .pulse-alloc-flag {{ color:#f59e0b; font-size:9px; }}

      /* wl-107: cycle-time (closed-in-window) + age (open-now) medians/p90s */
      .pulse-cycle-hd, .pulse-cycle-row {{ display:grid;
        grid-template-columns:minmax(0, 1fr) 48px 48px 48px 48px; gap:6px;
        align-items:baseline; padding:3px 6px; font-size:11px; }}
      .pulse-cycle-hd {{ font-size:9px; text-transform:uppercase; letter-spacing:0.08em;
        color:var(--dim); padding-bottom:5px; border-bottom:1px dashed var(--border); }}
      .pulse-cycle-hd span, .pulse-cycle-n {{ text-align:right; }}
      .pulse-cycle-hd span:first-child {{ text-align:left; }}
      .pulse-cycle-name {{ font-weight:600; overflow:hidden; text-overflow:ellipsis;
        white-space:nowrap; }}
      .pulse-cycle-n {{ font-variant-numeric:tabular-nums; font-weight:700; }}
      .pulse-cycle-n--age {{ color:#38bdf8; }}

      /* wl-107: focus cut — lanes ranked by open P1/P2 x staleness x blocked */
      .pulse-focus-hd, .pulse-focus-row {{ display:grid;
        grid-template-columns:minmax(0, 1fr) 44px 56px 56px; gap:8px;
        align-items:baseline; padding:3px 6px; font-size:11px; }}
      .pulse-focus-hd {{ font-size:9px; text-transform:uppercase; letter-spacing:0.08em;
        color:var(--dim); padding-bottom:5px; border-bottom:1px dashed var(--border); }}
      .pulse-focus-hd span, .pulse-focus-n {{ text-align:right; }}
      .pulse-focus-hd span:first-child {{ text-align:left; }}
      .pulse-focus-name {{ font-weight:600; overflow:hidden; text-overflow:ellipsis;
        white-space:nowrap; }}
      .pulse-focus-n {{ font-variant-numeric:tabular-nums; font-weight:700; }}
      .pulse-focus-n--blocked {{ color:#ef4444; }}
      .pulse-focus-n--zero {{ color:var(--dim); font-weight:400; }}

      /* wl-89: breakdown panel — status + priority bars side by side */
      .pulse-breakdown {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; }}
      @media (max-width: 720px) {{ .pulse-breakdown {{ grid-template-columns:1fr; }} }}
      .pulse-breakdown-title {{ font-size:10px; text-transform:uppercase; letter-spacing:0.1em;
        color:var(--dim); font-weight:600; margin-bottom:8px; }}
      .pulse-panel-meta a {{ color:var(--dim); }}

      .pulse-empty {{ color:var(--dim); padding:20px; text-align:center; font-size:12px;
        letter-spacing:0.05em; }}
    </style>

    <script>
      // Auto-refresh the whole page. Preserves scroll position on reload.
      // 30s: pulse now renders inside the merged Cockpit landing, so the
      // reload also repaints the breakdown charts below it.
      (function() {{
        var REFRESH_MS = 30000;
        setTimeout(function() {{ window.location.reload(); }}, REFRESH_MS);
      }})();
    </script>
    """ + _pulse_layout_js(scope)
    # wl-89: 4s live refresh for the breakdown bars + activity chart
    # (data-cockpit-* hooks) rides along with the page.
    return body + _cockpit_live_js()


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
from worklane.api.scene import router as _scene_router            # noqa: E402
from worklane.api.tasks import router as _tasks_router            # noqa: E402  wl-225

logger = logging.getLogger(__name__)

router = APIRouter()
router.include_router(_events_router)
router.include_router(_gen_router)
router.include_router(_scene_router)
router.include_router(_tasks_router)

# Canonical default agent identity (.mcp.json WL_AGENT_ID fallback). A write
# signed with this identity that also claims ownership of a *different*
# agent (an Owner: marker in the comment body) means the caller's launcher
# never exported WL_AGENT_ID (wl-39) and is silently mis-signing writes.
DEFAULT_AGENT_ID = "founder"


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
    import sqlite3

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
def products_index_redirect() -> RedirectResponse:
    return RedirectResponse(f"{TICKETS_APP_ALL}?view=board", status_code=302)


@router.get("/admin/products/tradeos")
def products_tradeos_redirect() -> RedirectResponse:
    return RedirectResponse(f"{TICKETS_APP_ALL}?view=board", status_code=302)


@router.get("/admin/products/ops")
def products_ops_redirect() -> RedirectResponse:
    return RedirectResponse(f"{TICKETS_APP_ALL}?view=board", status_code=302)


@router.get("/admin/products/more")
def products_more_redirect() -> RedirectResponse:
    """Legacy URL: Products shell removed; send to Tickets home."""
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
        # wl-90: Board/Table live in the primary header nav — no in-page toggle.
        view_toggle_html="",
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
# paper voice immediately. One /api/report seam feeds three consumers:
# this page, oc-15's daily founder brief, and city hall (reporting
# doctrine, wl-139: engines compute facts, dashboards render).

_REPORT_WINDOW_DAYS = int(os.environ.get("WL_REPORT_WINDOW_DAYS") or os.environ.get("WL_REPORT_WINDOW_DAYS", "7"))
_REPORT_AGING_DAYS = int(os.environ.get("WL_REPORT_AGING_DAYS") or os.environ.get("WL_REPORT_AGING_DAYS", "7"))
_REPORT_PRUNE_QUIET_HOURS = int(os.environ.get("WL_REPORT_PRUNE_QUIET_HOURS") or os.environ.get("WL_REPORT_PRUNE_QUIET_HOURS", "72"))


def _report_verdict(filed: int, signed: int, backlog: int, over_aging: int) -> str:
    """One deterministic word per ledger. Order matters: rot beats flow."""
    if backlog and over_aging >= max(5, (backlog + 4) // 5):
        return "aging"
    if filed >= 5 and filed >= 2 * signed:
        return "growing"
    if signed >= 0.8 * filed:
        return "keeping up"
    return "steady"


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
    waiting_on_you = len(_partition_attention_items(_collect_founder_attention_items(now=now), now=now)[0])
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
