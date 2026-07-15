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
import urllib.error
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
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

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
# WL_BRAND=city|standalone selects the mode. This internal checkout IS the
# city instance, so the default here is "city"; the WorkLane public export
# must default to "standalone" (wl-134 — flip the default in the export).
_BRAND_MODE = os.environ.get("WL_BRAND", "city")
# Second naming amendment (founder, 2026-07-14, theme adoption): with the
# living-scene themes the ROOM NAME leads in-city — "Ticket Desk" is the
# room, "Tickets" the one-word function. Standalone stays engine-branded.
_BRAND_NAME = (
    "ProtocolCity — Ticket Desk · Tickets" if _BRAND_MODE == "city" else "WorkLane — Tickets"
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
    "<span class='brand-room'>TICKET DESK</span> · TICKETS" if _BRAND_MODE == "city"
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
            "Every ticket across all project stores",
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
                f"{spec.display} tickets ({spec.db_path.name})",
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
            "Every ticket across all project stores",
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
              title="Everything waiting on the founder, across every store: in review, founder-decision, human-gated, stalled, embargoed."></span>
        <span id="ts-last-updated" class="ts-last-updated dim"></span>
"""
        )
    # Product tabs live inline in the primary header row (wl-36) — the
    # subnav band is gone; one row of chrome instead of two.
    _subnav_html = ""
    _port = os.environ.get("TASK_PORT", "8799")
    # wl-128: reciprocal of WorkForce's Workplaces join — link out to the
    # machine's worker/shift/law board. Config-driven only: unset means no
    # orchestrator is installed on this machine, which is lawful
    # (RUNNER_SPEC §9), so no link renders. When a host does run one
    # co-located, point this at it (typically http://127.0.0.1:8797).
    _workforce_url = os.environ.get("WL_WORKFORCE_URL", "").strip()
    # wl-90: Board and Table are sibling primary views. From a tickets page
    # the links keep the current scope path and filters; elsewhere they lead
    # to the scope's default view.
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
  /* Theme init (before paint) — Dispatch paper is the default (wl-37); dark
     and system remain fully supported via the existing toggle/persistence. */
  (function() {{
    /* wl-84: key renamed from 'tradeos-theme' — migrate old prefs once. */
    var legacy = localStorage.getItem('tradeos-theme');
    if (legacy && !localStorage.getItem('wl-theme')) {{
      localStorage.setItem('wl-theme', legacy);
      localStorage.removeItem('tradeos-theme');
    }}
    var stored = localStorage.getItem('wl-theme') || 'light';
    function resolve(p) {{
      if (p === 'system') return window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
      return p;
    }}
    document.documentElement.setAttribute('data-theme', resolve(stored));
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
<body data-ops-shell="{_esc(shell)}" data-ops-scope="{_esc(page_scope)}">
  <header class="task-server-header task-server-header--stack">
    <div class="task-server-header-primary ops-main-nav" data-ops-region="main-nav">
      <a href="/admin/desk" class="{_brand_cls}" title="The desk — the room you walk into">{_BRAND_HEADER_HTML}</a>{f'<span class="task-server-hint dim">{_BRAND_SUBTITLE}</span>' if _BRAND_SUBTITLE else ''}
      <nav class="ts-primary-shell ts-segmented" aria-label="Primary">
        <a href="/admin/overview/{_esc(page_scope or 'all')}" class="{_seg(shell == 'overview' and nav_active == 'overview')}"
           title="Landing — the store visually interpreted: live metrics + breakdown charts"{' aria-current="page"' if (shell == 'overview' and nav_active == 'overview') else ''}>Overview</a>
        <a href="{_board_href}" class="{_seg(_board_on)}"
           title="Ticket board — cards by status column"{_board_cur}>Board</a>
        <a href="{_table_href}" class="{_seg(_table_on)}"
           title="Ticket table — dense timetable rows"{_table_cur}>Table</a>
      </nav>
{_product_scope}
      <div class="task-server-header-end">
{_ticket_header_widgets}
        <span class="task-server-hint dim">port {_esc(_port)}</span>{f'''
        <a href="{_esc(_workforce_url)}" target="_blank" rel="noopener"
           title="WorkForce — the machine's worker/shift/law board (separate port)"
           style="text-decoration:none; color:var(--dim); font-size:12px; padding:4px 6px;">WorkForce</a>''' if _workforce_url else ''}
        <a href="/admin/docs" title="Docs — PROCESS/ARCHITECTURE/README + per-agent instruction files rendered in-app"
           style="text-decoration:none; color:{'var(--text)' if nav_active == 'docs' else 'var(--dim)'}; font-size:16px; padding:4px 6px;">&#128220;</a>
        <a href="/admin/settings" title="Settings — projects, prefixes, numbering, service"
           style="text-decoration:none; color:{'var(--text)' if nav_active == 'settings' else 'var(--dim)'}; font-size:16px; padding:4px 6px;">&#9881;</a>
        <span class="ts-dev-badge">WL</span>
        <button id="theme-toggle" onclick="cycleTheme()" title="Toggle theme"
                style="background:none;border:0;color:var(--dim);cursor:pointer;font-size:16px;padding:4px 8px;">&#9789;</button>
      </div>
    </div>
{_subnav_html}
  </header>
  <div class="page page-full ts-ops-page">
    {body}
  </div>
  <script>
  var _themeIcons = {{ dark: '\\u263D', light: '\\u2600', system: '\\u25D1' }};
  function cycleTheme() {{
    var order = ['dark', 'light', 'system'];
    var current = localStorage.getItem('wl-theme') || 'light';
    var next = order[(order.indexOf(current) + 1) % order.length];
    localStorage.setItem('wl-theme', next);
    var resolved = (next === 'system')
      ? (window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark')
      : next;
    document.documentElement.setAttribute('data-theme', resolved);
    var btn = document.getElementById('theme-toggle');
    if (btn) btn.textContent = _themeIcons[next] || '\\u263D';
  }}
  (function() {{
    var pref = localStorage.getItem('wl-theme') || 'light';
    var btn = document.getElementById('theme-toggle');
    if (btn) btn.textContent = _themeIcons[pref] || '\\u263D';
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
    background: rgba(245,158,11,.15); color: #f59e0b;
    border: 1px solid rgba(245,158,11,.3);
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
) -> Tuple[List[Task], Dict[str, Dict[str, str]]]:
    """Merge tasks across all product stores; the live-feed product's half
    may come from the main app HTTP API when ``TRADEOS_TICKETS_SOURCE`` says
    so (see products.live_feed_product_slug)."""
    empty_prev: Dict[str, Dict[str, str]] = {}
    p = (product or "").strip().lower()
    if not _tradeos_tickets_use_http_feed():
        tasks = list_tasks_for_wq_multi(
            products,
            status=status,
            label=label,
            priority=priority,
            product=p,
            limit=limit,
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
                limit=500,
            )
        )
    merged.sort(key=lambda x: x.updated_at or "", reverse=True)
    out = merged[:limit]
    if not with_preview:
        prev = {}
    return out, prev


def _scoped_product_trackers(scope: str = "") -> List[Tuple[ProductSpec, Any]]:
    """Registered (spec, tracker) pairs, narrowed to one project store when a
    scope slug is given ("" = every store) — wl-85: every page declares a
    scope and everything on it honors that scope.
    """
    pairs = product_trackers()
    if not scope:
        return pairs
    return [(s, t) for s, t in pairs if s.slug == scope]


def _merged_ready_count(scope: str = "") -> int:
    """Ready-to-dispatch count aggregated across the in-scope product
    trackers (wl-40; "" = all) — each product's WorkQueue resolves its own
    blockers independently (blocker ids don't cross product boundaries), so
    this sums per-tracker ready() counts rather than building one
    cross-product queue. Local SQLite trackers only, same as
    list_tasks_for_scope_multi's non-HTTP-feed branch below — the tradeOS
    live-HTTP-feed source is wl-48 slice c's separate concern.
    """
    return sum(
        len(WorkQueue(tracker).ready())
        for _spec, tracker in _scoped_product_trackers(scope)
    )


def _merged_in_flight_tasks(scope: str = "") -> List[Task]:
    """in_progress + in_review tasks aggregated across the in-scope product
    trackers (wl-40; "" = all), sorted newest-updated first. See
    _merged_ready_count for the local-SQLite-only scope note.
    """
    out: List[Task] = []
    for spec, tracker in _scoped_product_trackers(scope):
        out.extend(
            # Composite ids, same convention as list_tasks_for_scope_multi
            # (wl-144): bare store-local ids fall back to the DEFAULT store
            # in split_task_id, mis-attributing every non-default store's
            # in-flight work downstream (attention feed, in-flight API).
            replace(t, id=f"{spec.prefix}-{t.id}")
            for t in WorkQueue(tracker).all_tasks
            if t.status in (TaskStatus.IN_PROGRESS, TaskStatus.IN_REVIEW)
        )
    out.sort(key=lambda t: t.updated_at or "", reverse=True)
    return out


def _merged_scope_tasks_for_filters(product: str) -> List[Task]:
    """All tasks for label chips / buckets (respects HTTP vs local live-feed source)."""
    p = parse_wq_product(product)
    products = product_trackers()
    if not _tradeos_tickets_use_http_feed():
        return list_tasks_for_scope_multi(products, p, limit=None)
    feed_slug = live_feed_product_slug()
    merged: List[Task] = []
    if p in ("", feed_slug):
        ta, _ = _fetch_tradeos_tasks_via_http(
            status=None,
            label=None,
            priority=None,
            limit=5000,
            with_preview=False,
        )
        merged.extend(ta)
    if p != feed_slug:
        non_feed = [(s, t) for s, t in products if s.slug != feed_slug]
        merged.extend(list_tasks_for_scope_multi(non_feed, p, limit=None))
    merged.sort(key=lambda x: x.updated_at or "", reverse=True)
    return merged


def _resolve_product_tracker(task_id: str) -> Tuple[str, str, Any]:
    """Composite task id → (product slug, raw store id, tracker).

    ``o-`` ids still resolve to the retired ops store so legacy links
    keep working; everything else routes through the product registry.
    """
    slug, raw = split_task_id(task_id)
    if slug == "ops":
        return slug, raw, get_ops_ticket_tracker()
    return slug, raw, product_tracker(slug)


def _tracker_db_path(tracker: Any) -> Optional[Path]:
    """Hot SQLite path for a product tracker, if it is file-backed."""
    p = getattr(tracker, "_db_path", None)
    if p is None:
        return None
    return Path(p)


def _archive_tracker_for_hot_db(hot_db: Path) -> Optional[Any]:
    """Open the sibling archive store for ``hot_db`` (None if missing)."""
    from worklane.trackers.sqlite import SQLiteTracker

    archive_path = archival.archive_db_path_for(hot_db)
    if not archive_path.exists():
        return None
    return SQLiteTracker(db_path=archive_path, product_default="")


def _get_task_hot_or_archive(
    tracker: Any, raw_id: str
) -> Tuple[Optional[Task], List[TaskComment], bool]:
    """Hot-store first; fall through to sibling archive DB (read-only).

    Returns ``(task, comments, archived)``. ``archived=True`` means the
    row lives only in cold storage — mutations must refuse.
    """
    task = tracker.get_task(raw_id)
    if task is not None:
        comments: List[TaskComment] = (
            tracker.list_comments(raw_id)
            if hasattr(tracker, "list_comments")
            else []
        )
        return task, comments, False

    hot = _tracker_db_path(tracker)
    if hot is None:
        return None, [], False
    archive_tr = _archive_tracker_for_hot_db(hot)
    if archive_tr is None:
        return None, [], False
    task = archive_tr.get_task(raw_id)
    if task is None:
        return None, [], False
    comments = (
        archive_tr.list_comments(raw_id)
        if hasattr(archive_tr, "list_comments")
        else []
    )
    return task, comments, True


def _fetch_tradeos_ops_snapshot() -> Dict[str, Optional[Dict[str, Any]]]:
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
        link = f"/admin/tasks/{tid}"
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
        "<thead><tr><th>Ticket</th><th>Status</th><th>Pri</th><th>Updated</th></tr></thead>"
        "<tbody>"
        + (
            "".join(rows)
            if rows
            # wl-91: quick-add is gone (wl-26) — don't advertise it.
            else "<tr><td colspan='4' class='dim'>No open tickets — "
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


def _parse_task_date_utc(raw: Optional[str]) -> Optional[datetime]:
    s = (raw or "").strip()
    if not s:
        return None
    try:
        # Common case: ISO timestamp, optionally with Z suffix.
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        pass
    # Fallbacks for SQLite-ish formats.
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s[:19], fmt)
            return dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue
    return None


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
        f"<svg viewBox='0 0 {w} {h}' role='img' aria-label='Ticket activity last {days} days' "
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
        + _render_count_bars(status_rows, empty_text="No ticket statuses yet.")
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
        "This page will aggregate ticket summaries, work-in-flight, ADR links, and other "
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
        "<p class='dim'>The ticket store (<strong>Board</strong> / <strong>Table</strong>) runs on a "
        "<strong>separate port</strong> from the host product so the host can restart "
        "without losing the board (ADR-019).</p>"
        f"<p class='dim'>You are on port <code>{task_port}</code> — landing: "
        "<code>/admin/overview</code>. Use <strong>Board</strong> or <strong>Table</strong> in the "
        "header for the tracker; <strong>Projects</strong> for the per-project hubs.</p>"
    )
    return f"<div class='ts-prod-page'>{_task_card('Ticketing (this console)', body)}</div>"


# ── Pulse — live operator dashboard ─────────────────────────────────────

def _pulse_relative_time(iso_ts: str, *, now: Optional[datetime] = None) -> str:
    """Return a compact relative time string like '3m', '2h', '1d'."""
    if not iso_ts:
        return "—"
    try:
        s = iso_ts.replace("Z", "+00:00")
        ts = datetime.fromisoformat(s)
    except Exception:
        return "—"
    if now is None:
        now = datetime.now(ts.tzinfo)
    delta = now - ts
    secs = int(delta.total_seconds())
    if secs < 0:
        return "now"
    if secs < 60:
        return f"{secs}s"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m"
    hrs = mins // 60
    if hrs < 24:
        return f"{hrs}h"
    days = hrs // 24
    return f"{days}d"


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
            f"<a href='/admin/tasks/{_esc(t.id)}' class='agent-row'>"
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


def _parse_iso_ts(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


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


def _allocation_lane_rows(
    all_tasks: List[Task], since: datetime, *, prefix: str = _LANE_LABEL_PREFIX
) -> List[Dict[str, Any]]:
    """Filed-vs-closed per lane:* label within the window.

    Filed = created_at in window (any status); closed = status==done and
    updated_at in window — same created/updated proxy _render_flow_panel
    uses (no closed_at column). Unlabeled tasks collect into a synthetic
    'unlabeled' row, same convention as _lane_lens_rows.
    """
    buckets: Dict[str, Dict[str, int]] = {}

    def _bucket(name: str) -> Dict[str, int]:
        return buckets.setdefault(name, {"filed": 0, "closed": 0})

    for t in all_tasks:
        lanes = [lbl[len(prefix):] for lbl in (t.labels or []) if lbl.startswith(prefix)] or ["unlabeled"]
        created = _parse_iso_ts(t.created_at)
        if created is not None and created >= since:
            for lane in lanes:
                _bucket(lane)["filed"] += 1
        if t.status == TaskStatus.DONE:
            closed_at = _parse_iso_ts(t.updated_at)
            if closed_at is not None and closed_at >= since:
                for lane in lanes:
                    _bucket(lane)["closed"] += 1

    rows = [
        {"lane": lane, **counts}
        for lane, counts in buckets.items()
        if counts["filed"] or counts["closed"]
    ]
    rows.sort(key=lambda r: (r["lane"] != "unlabeled", -(r["filed"] + r["closed"]), r["lane"]))
    return rows


def _allocation_author_rows(scope: str, since: datetime) -> List[Dict[str, Any]]:
    """Filed-vs-closed per comment author within the window.

    Same signed-comment derivation as _author_tally: filed = 'Intake: filed
    by%' comment (PROTOCOL.md §5 intake marker), closed = 'Completed:%'
    closeout comment — windowed on the comment's own created_at.
    """
    merged: Dict[str, Dict[str, Any]] = {}
    since_s = since.isoformat()
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
                           COUNT(DISTINCT CASE WHEN body LIKE 'Intake: filed by%'
                                               AND created_at >= ? THEN task_id END) AS filed,
                           COUNT(DISTINCT CASE WHEN body LIKE 'Completed:%'
                                               AND created_at >= ? THEN task_id END) AS closed
                    FROM task_comments
                    WHERE author != ''
                    GROUP BY author
                    """,
                    (since_s, since_s),
                ).fetchall()
            finally:
                conn.close()
        except Exception:
            continue
        for author, filed, closed in rows:
            agg = merged.setdefault(author, {"author": author, "filed": 0, "closed": 0})
            agg["filed"] += int(filed or 0)
            agg["closed"] += int(closed or 0)
    out = [a for a in merged.values() if a["filed"] or a["closed"]]
    out.sort(key=lambda a: (-(a["filed"] + a["closed"]), a["author"]))
    return out


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


# wl-86: PROTOCOL.md §4 — in-flight tickets with no update in over 90 minutes
# count as stalled; the Attention panel surfaces them alongside blocked and
# aging backlog work. wl-138: reuses board._claim_stale_minutes() (env
# TICKETING_CLAIM_STALE_MINUTES, wl-104) as the one named knob instead of a
# second hardcoded constant — same threshold value, still a distinct check
# (no update at all vs. board.py's no-comment-since-claim).
def _stale_inflight() -> timedelta:
    return timedelta(minutes=_claim_stale_minutes())


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
            f"<a href='/admin/tasks/{_esc(t.id)}' class='pulse-side-row'>"
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
            f"<a href='/admin/tasks/{_esc(t.id)}' class='pulse-side-row'>"
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
_FOUNDER_DECISION_LABELS = {"needs:founder-decision", "founder-decision"}


def _attention_item(
    t: Task, product: str, kind: str, note: str,
    since: Optional[datetime], now: datetime,
) -> Dict[str, Any]:
    return {
        "id": t.id,
        "product": product,
        "title": t.title,
        "priority": int(t.priority or 3),
        "kind": kind,
        "note": note,
        "waiting_since": since.isoformat() if since else None,
        "age_minutes": int((now - since).total_seconds() // 60) if since else None,
        "gate_until": None,
        "url": f"/admin/tasks/{t.id}",
    }


def _collect_founder_attention_items(*, now: datetime) -> List[Dict[str, Any]]:
    """Everything blocked on the founder, all stores (wl-135): in_review
    (review IS the founder gate), needs:founder-decision/founder-decision
    labels, gate_type=human, stalled in-flight (§4, >90m — reuses
    _stale_inflight()), and gate_type=timer embargoes with a machine-readable
    gate_until. Sorted oldest-first — age is how long the founder has been
    the blocker. Each open task counts once, first match wins in the order
    above (an in_review ticket that's also labeled founder-decision shows
    once, as in_review).
    """
    items: List[Dict[str, Any]] = []
    counted: set = set()

    for t in _merged_in_flight_tasks(""):
        prod_slug, _ = split_task_id(t.id)
        if t.status == TaskStatus.IN_REVIEW:
            since = _parse_task_date_utc(t.updated_at) or _parse_task_date_utc(t.created_at)
            items.append(_attention_item(t, prod_slug, "in_review", "awaiting review", since, now))
            counted.add(t.id)
        else:
            since = _parse_task_date_utc(t.updated_at)
            if since is not None and (now - since) >= _stale_inflight():
                items.append(_attention_item(
                    t, prod_slug, "stalled",
                    f"no update {_pulse_relative_time(t.updated_at, now=now)}", since, now,
                ))
                counted.add(t.id)

    for t in _merged_scope_tasks_for_filters(""):
        if t.id in counted or t.status in (TaskStatus.DONE, TaskStatus.CANCELED):
            continue
        prod_slug, _ = split_task_id(t.id)
        since = _parse_task_date_utc(t.updated_at) or _parse_task_date_utc(t.created_at)
        labels = set(t.labels or [])
        if labels & _FOUNDER_DECISION_LABELS:
            items.append(_attention_item(t, prod_slug, "founder_decision", "founder decision needed", since, now))
            counted.add(t.id)
        elif t.gate_type == "human":
            items.append(_attention_item(t, prod_slug, "human_gate", t.gate_note or "human gate", since, now))
            counted.add(t.id)
        elif t.gate_type == "timer" and t.gate_until:
            note = f"gated until {t.gate_until[:10]}" if t.gate_until else (t.gate_note or "embargoed")
            item = _attention_item(t, prod_slug, "embargo", note, since, now)
            item["gate_until"] = t.gate_until
            items.append(item)
            counted.add(t.id)

    items.sort(key=lambda it: it["waiting_since"] or "")
    return items


def _render_founder_attention_rows(items: List[Dict[str, Any]], *, limit: Optional[int] = None) -> str:
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


def _render_attention_page_body(items: List[Dict[str, Any]]) -> str:
    """wl-135: full-page render behind /admin/attention — same rows as the
    Overview panel, unlimited, plus a per-kind breakdown line."""
    if not items:
        return _task_card(
            "Waiting on founder",
            "<div class='pulse-empty'>&#10003; Nothing waiting on you across any store.</div>",
        )
    by_kind: Dict[str, int] = {}
    for it in items:
        by_kind[it["kind"]] = by_kind.get(it["kind"], 0) + 1
    summary = " &middot; ".join(
        f"{by_kind[k]} {_ATTENTION_KIND_META.get(k, (k, ''))[0]}"
        for k in by_kind
    )
    rows_html = _render_founder_attention_rows(items)
    return _task_card(
        f"Waiting on founder — {len(items)}",
        f"<div class='dim' style='margin-bottom:10px; font-size:12px;'>{summary}</div>{rows_html}",
    )


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

    def _parse_ts(s: str) -> Optional[datetime]:
        if not s:
            return None
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None

    done_today = [t for t in all_tasks if t.status == TaskStatus.DONE
                  and (_parse_ts(t.updated_at) or now) >= today_start]

    # Throughput sparkline — done tasks per hour over last 24h (24 buckets)
    hourly_buckets = [0] * 24
    for t in all_tasks:
        if t.status != TaskStatus.DONE:
            continue
        ts = _parse_ts(t.updated_at)
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
        key=lambda t: _parse_ts(t.updated_at) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )[:20]

    # In-flight cards
    inflight_tasks = sorted(
        in_progress + in_review,
        key=lambda t: (0 if t.status == TaskStatus.IN_PROGRESS else 1,
                       _parse_ts(t.updated_at) or datetime.min.replace(tzinfo=timezone.utc)),
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
    founder_attention_items = _collect_founder_attention_items(now=now)
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
                f"<a href='/admin/tasks/{_esc(t.id)}' class='pulse-card{pulse_cls}'>"
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
            f"<a href='/admin/tasks/{_esc(t.id)}' class='pulse-ticker-row'>"
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
              <span class='pulse-panel-title'>Waiting on founder</span>
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
    status: str, label: str, priority: str, product: str = ""
) -> str:
    """Board polling must mirror the same filters as the page."""
    tsurf = _ticket_create_surface_from_scope(product or "")
    payload = json.dumps(
        {
            "status": status or "",
            "label": label or "",
            "priority": priority or "",
            "product": product or "",
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
        "<th class='tt-c-ticket' data-tt-key='ticket'>Ticket</th>"
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
    href = f"/admin/tasks/{_esc(t.id)}"
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

logger = logging.getLogger(__name__)

router = APIRouter()

# Canonical default agent identity (.mcp.json WL_AGENT_ID fallback). A write
# signed with this identity that also claims ownership of a *different*
# agent (an Owner: marker in the comment body) means the caller's launcher
# never exported WL_AGENT_ID (wl-39) and is silently mis-signing writes.
DEFAULT_AGENT_ID = "founder-terminal"


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
        "<tr><td>Close-out contract (§5) — <code>Completed:</code> requires <code>Verification:</code> + <code>Links:</code></td><td>enforced</td></tr>"
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
        f"<tr><th>Runtime dir</th><td><code>{_esc(str(wl_data_dir().parent))}</code> (WORKLANE_RUNTIME_DIR)</td></tr>"
        f"<tr><th>Data dir</th><td><code>{_esc(str(wl_data_dir()))}</code></td></tr>"
        "<tr><th>DB overrides</th><td><code>WORKLANE_DB</code> (tradeos store), <code>OPS_TICKETS_DB</code> (legacy)</td></tr>"
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
        + _task_card("Done-ticket archival", archival_html)
        + _task_card("Identity & enforcement", identity_html)
        + _task_card("Service", service_html)
        + "</div>"
        + _task_server_extra_css()
        + settings_js
    )
    return _task_page("Settings", body, nav_active="settings")


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
) -> Any:
    """Tickets app — surface is a first-class path (``all`` \| ``tradeos``)."""
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
) -> str:
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
        merged_scope_tasks=merged_scope,
        # wl-90: Board/Table live in the primary header nav — no in-page toggle.
        view_toggle_html="",
    )
    extra_js = _task_server_extra_js()
    extra_css = _task_server_extra_css()

    poll_inject = (
        _wq_poll_script(status, label, priority, prod or "")
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
            + "<p>No ticket with id <code>"
            f"{_esc(task_id)}</code>. "
            f"<a href='{TICKETS_APP_ALL}'>Back to list</a></p>"
            + _client_js()
            + _task_server_extra_js()
        )
        return _task_page(
            "Ticket not found",
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
        f"<pre style='white-space:pre-wrap; font-family:var(--font-mono); "
        f"font-size:var(--fs-sm); margin:0;'>{_esc(desc)}</pre>"
        if desc else "<p class='dim'>No description.</p>"
    )

    archive_banner = ""
    if archived:
        archive_banner = (
            "<div class='ts-archive-banner' role='status'>"
            "Archived (cold storage) — read-only. This ticket was compacted out "
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
        f"<a href='{TICKETS_APP_ALL}'>&larr; All tickets</a></p>"
        + archive_banner
        + f"<h1 style='margin:0 0 4px 0;'>{_esc(task.title)}</h1>"
        f"{ext_html}"
        + _task_card("Metadata", meta)
        + _task_card("Description", desc_html)
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

def _city_neighborhood_slugs() -> Optional[set]:
    """Neighborhood folder names (lowercased) at the city root, or None when
    no city is detectable (wl-155). WL stays host-neutral: WL_CITY_ROOT env
    wins; otherwise walk up from this repo to the topmost dir carrying an
    AGENTS.md (the city-root convention). A standalone checkout that is its
    own topmost AGENTS.md dir counts as no city — the check silently skips.
    """
    root = (os.environ.get("WL_CITY_ROOT") or "").strip()
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
    try:
        return {
            name.lower()
            for name in os.listdir(root)
            if os.path.isdir(os.path.join(root, name))
            and os.path.isfile(os.path.join(root, name, "AGENTS.md"))
        }
    except OSError:
        return None


@router.post("/api/admin/products")
async def api_create_product(request: Request) -> JSONResponse:
    """Bootstrap a new product store (wl-12): creates ``<slug>.db`` and
    returns its surface. Deliberate by design — no implicit creation from a
    typo'd ``surface=`` on ``/api/admin/tasks``; this is the only door in.
    """
    import re

    from worklane.trackers.sqlite import SQLiteTracker

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
    # wl-155: soft founding-path guardrail — the city joins store to
    # neighborhood by slug == dirname.lower(); warn (never refuse) when no
    # such folder exists. Skips silently outside a city (host-neutral).
    warning = None
    hoods = _city_neighborhood_slugs()
    if hoods is not None and slug not in hoods:
        warning = (
            f"store created, but no neighborhood folder named {slug!r} exists "
            "at the city root — city hall won't show a building until one "
            "does (the slug must equal the folder name, lowercased)"
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
    import re

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

    def _sign_intake(tracker: Any, raw_id: Any) -> None:
        """Record the filer on the ticket (tasks have no creator column —
        the signed comment trail is the §5.2 record)."""
        try:
            tracker.add_comment(str(raw_id), f"Intake: filed by {author}", author=author)
        except Exception:
            pass

    # project (wl-64) is the canonical field name; ticket_surface/surface
    # remain silent back-compat aliases. Reject rather than silently pick
    # when both are given with different values (PROTOCOL.md §5.2 rule).
    project_val = payload.get("project")
    legacy_surface_val = payload.get("ticket_surface") or payload.get("surface")
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
        )
        _sign_intake(tracker, task.id)
        out = task.to_dict()
        out["id"] = f"{prefix}-{task.id}"
        return JSONResponse({"ok": True, "task": out})

    spec = get_product(surface)
    if spec is None:
        return JSONResponse(
            {
                "ok": False,
                "error": f"unknown ticket surface {surface!r} — no {surface}.db project store",
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
        )
        _sign_intake(tracker, task.id)
        out = task.to_dict()
        out["id"] = f"{spec.prefix}-{task.id}"
        return JSONResponse({"ok": True, "task": out})

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
        return JSONResponse({"ok": True, "task": out})

    tracker = get_default_tracker()
    task = tracker.create_task(
        title=title,
        description=description,
        status=status_val,
        priority=priority,
        labels=labels,
        ext_id=ext_id,
    )
    _sign_intake(tracker, task.id)
    out = task.to_dict()
    out["id"] = f"{TASK_ID_PREFIX_TRADEOS}-{task.id}"
    return JSONResponse({"ok": True, "task": out})


def _tracker_db_path(tracker: Any) -> Path:
    """Resolve the SQLite path for a tracker (tests + product stores)."""
    path = getattr(tracker, "_db_path", None)
    if path is None:
        raise HTTPException(status_code=500, detail="tracker has no local db path")
    return Path(path)


@router.get("/api/admin/tasks/ready")
def api_tasks_ready(
    product: str = "",
    label: str = "",
    explain: int = 0,
    limit: int = 200,
) -> JSONResponse:
    """Dispatch-ready backlog tickets (wl-20 structured relations).

    Uses ``blocks`` edges in ``task_relations``. When ``explain=1``, each
    ticket includes ``ready`` / ``blocked_by`` detail (ready list is only
    the ready ones; full backlog explain is under ``explain`` when set).
    Prose ``Depends on #N`` remains the intake shim — it is not replaced
    here; materialize via the dry-run backfill script.
    """
    from worklane import relations as relmod

    prod = (product or "").strip().lower()
    if not prod or prod == "all":
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
    from worklane import relations as relmod

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
    from worklane import relations as relmod

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

    surf, raw_from, tracker = _resolve_product_tracker(task_id)
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
def api_delete_task_relation(task_id: str, relation_id: str) -> JSONResponse:
    """Delete a relation by id; task_id must be an endpoint of the edge."""
    from worklane import relations as relmod

    surf, raw_id, tracker = _resolve_product_tracker(task_id)
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


@router.patch("/api/admin/tasks/{task_id}")
async def api_update_task(task_id: str, request: Request) -> JSONResponse:
    try:
        payload: Dict[str, Any] = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)

    surf, raw_id, tracker = _resolve_product_tracker(task_id)

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
        updated = tracker.update_status(raw_id, new_status)
        if updated is None:
            return JSONResponse({"ok": False, "error": "task not found"}, status_code=404)
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
    if gate_type is not None and gate_type not in ("", "human", "timer"):
        return JSONResponse(
            {"ok": False, "error": "gate_type must be '' (clear), 'human', or 'timer'"},
            status_code=400,
        )
    if gate_type == "timer" and not gate_until:
        return JSONResponse(
            {"ok": False, "error": "gate_until is required when gate_type is 'timer'"},
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
        )
        if updated is None:
            return JSONResponse({"ok": False, "error": "task not found"}, status_code=404)
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

    surf, raw_id, tracker = _resolve_product_tracker(task_id)

    updated = tracker.update_labels(raw_id, add=add_labels, remove=remove_labels)
    if updated is None:
        return JSONResponse({"ok": False, "error": "task not found"}, status_code=404)
    out = updated.to_dict()
    out["id"] = task_id
    return JSONResponse({"ok": True, "task": out})


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
    if first_line.startswith("Blocked") and "Next step:" not in body:
        return (
            "Blocked comments must include a 'Next step:' line "
            "(PROTOCOL.md §5)"
        )
    return None


def _misattributed_owner(author: str, body: str) -> Optional[str]:
    """wl-50 guard: default-identity write claiming a different Owner:.

    Returns the mismatched agent id when a comment signed with the default
    identity (``founder-terminal``) carries an ``Owner: <agent>`` marker for
    a *different* agent — the signature the wl-39 misconfiguration produces
    (launcher never exported ``WL_AGENT_ID``, so an autonomous agent's
    writes fall back to the default and silently mis-sign). Returns None for
    normal interactive founder-terminal use.
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
            "claims Owner: %r — launcher likely never exported WL_AGENT_ID "
            "(wl-39/wl-50)",
            author, task_id, marked,
        )

    surf, raw_id, tracker = _resolve_product_tracker(task_id)
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
    limit: int = 200,
    with_preview: int = 0,
) -> JSONResponse:
    products = product_trackers()
    prio_int = parse_wq_priority(priority)
    prod = parse_wq_product(product)
    tasks, tradeos_prev = _list_tasks_for_wq_multi_resolved(
        products,
        status=status or None,
        label=label or None,
        priority=prio_int,
        product=prod,
        limit=limit,
        with_preview=bool(with_preview),
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

    scope_tasks = list_tasks_for_scope_multi(products, prod, limit=None)
    scope_counts = _wq_status_counts(scope_tasks)
    scope_total = sum(scope_counts.get(s, 0) for s in TaskStatus.ALL)
    # wl-47: board column headers show the filtered-scope truth, not the
    # capped fetch; chips keep the unfiltered scope_counts.
    column_counts = _wq_column_counts(
        scope_tasks,
        status=status or None,
        label=label or None,
        priority=prio_int,
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


@router.get("/api/ops/tickets-health")
def api_ops_tickets_health() -> JSONResponse:
    """Verify dual ticket DB paths and row counts (standalone WorkLane server)."""
    import sqlite3

    from worklane.trackers.sqlite import DEFAULT_DB_PATH

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
            "<th>Ticket</th><th>Priority</th><th>Labels</th><th>Updated</th>"
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
            f"Batch {idx} · 1 ticket" if single else
            f"Batch {idx} · {len(batch.tickets)} tickets sharing files"
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
            "<thead><tr><th>Ticket</th><th>Priority</th><th>Labels</th><th>Updated</th></tr></thead>"
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
        f"{len(ready)} ready ticket(s) in {len(batches)} batch(es). "
        "Tickets that touch the same files are grouped for one terminal."
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
        "<p class='dim'>Walks every in-progress ticket, scans <code>git log</code> "
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


@router.get("/api/dev/tasks")
def api_dev_tasks(status: str = "", label: str = "", limit: int = 200):
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


def _activity_ts_sort_key(raw: object) -> float:
    """Parse mixed ISO/SQLite timestamps so merged feed sorts newest-first reliably."""
    if raw is None:
        return 0.0
    s = str(raw).strip()
    if not s:
        return 0.0
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


@router.get("/api/dev/activity")
def api_dev_activity(limit: int = 30, project: str = ""):
    """Recent comments + status changes across all tasks, newest first.

    Returns a unified feed mixing comments and status transitions so the
    board's activity widget shows everything happening in one timeline.

    ``project`` scopes the feed to a specific product's tracker (e.g. a
    machine-wide worker roster on a host reads each lane's rounds from the
    project it signs into — tradeOS t-1327). Omitted or unknown → the
    server default tracker (``product_tracker`` falls back for an unknown
    slug, so a stale ?project= degrades to today's behavior, never errors).
    """
    tracker = product_tracker(project) if project else get_default_tracker()
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
        entries.append({
            "id": r["id"],
            "task_id": str(r["task_id"]),
            "body": r["body"],
            "author": r["author"],
            "created_at": r["created_at"],
            "task_title": r["task_title"],
            "entry_type": "comment",
            "new_status": "",
        })
    for r in status_rows:
        entries.append({
            "id": f"sc-{r['task_id']}",
            "task_id": str(r["task_id"]),
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


@router.get("/api/dev/board-summary")
def api_dev_board_summary(scope: str = ""):
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
def api_dev_board_summary_all_scopes():
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
def api_dev_allocation(window_days: int = 7, scope: str = "all"):
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


@router.get("/api/dev/attention")
def api_dev_attention():
    """wl-135: the founder-attention feed — everything blocked on the
    founder, always all stores (like board-summary's scope=all; there is no
    ``scope`` param here, this view has no per-store variant). Same shape
    convention as /api/dev/board-summary so other dashboards (Workers,
    Projects) and reporting sessions can consume it directly.
    """
    now = datetime.now(timezone.utc)
    items = _collect_founder_attention_items(now=now)
    resp = JSONResponse({
        "ok": True,
        "count": len(items),
        "items": items,
        "stale_minutes": _claim_stale_minutes(),
        "updated_at": now.isoformat(),
    })
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


@router.get("/admin/attention", response_class=HTMLResponse)
def admin_attention() -> Any:
    """wl-135: the full "waiting on founder" list — the standalone page
    behind the header chip and the Overview panel's "view all". Always all
    stores; this view has no scope concept (PROTOCOL.md §5 review gate,
    needs:founder-decision/founder-decision labels, gate_type=human,
    stalled in-flight, date-gated embargoes).
    """
    now = datetime.now(timezone.utc)
    items = _collect_founder_attention_items(now=now)
    body = _render_attention_page_body(items) + _task_server_extra_css()
    return _task_page("Waiting on founder", body, nav_active="attention")


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


@router.get("/api/dev/queue/ready")
def api_dev_queue_ready():
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
def api_dev_queue_dispatch(task_ids: str = ""):
    """Transition batch tasks to in_progress and redirect with dispatch prompt."""
    if not task_ids:
        return JSONResponse({"error": "task_ids required"}, status_code=400)

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
def api_dev_queue_in_flight():
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
def api_dev_queue_shutdown(apply: int = 0):
    """Run the close-out protocol against every in-progress ticket."""
    tracker = get_default_tracker()
    report = run_shutdown(tracker, apply=bool(apply))
    return JSONResponse({
        "tracker": tracker.name,
        **report.to_dict(),
    })


def _task_server_extra_js() -> str:
    """Additional JS for task-server-only features: clickable label
    filters, elapsed time, context-strip smart feed, quick-add panel,
    badge summaries, last-updated indicator, and card transition animations.

    Loaded on Overview, Board/Table, and Dev Queue — not on Products shell
    (see :func:`_products_page_response`).
    """
    return r"""
<script>
  /* wl-9: clickable on-card label/priority filters lived only on the JS
     card renderer (poll path). Cards are server-rendered now; filter via
     the command-bar chips instead. */

  /* ── Elapsed time helper ─────────────────────────────────────────── */
  function afFormatElapsed(isoStr) {
    if (!isoStr) return '';
    var then = Date.parse(isoStr);
    if (isNaN(then)) return '';
    var secs = Math.max(0, Math.floor((Date.now() - then) / 1000));
    var h = Math.floor(secs / 3600);
    var m = Math.floor((secs % 3600) / 60);
    if (h > 24) {
      var d = Math.floor(h / 24);
      return d + 'd ' + (h % 24) + 'h';
    }
    if (h > 0) return h + 'h ' + m + 'm';
    return m + 'm';
  }

  /* ── Last-updated indicator ──────────────────────────────────────── */
  var _tsLastPollAt = 0;
  function tsUpdateLastUpdated() {
    var el = document.getElementById('ts-last-updated');
    if (!el || !_tsLastPollAt) return;
    var secs = Math.floor((Date.now() - _tsLastPollAt) / 1000);
    el.textContent = secs < 5 ? 'Updated just now' : 'Updated ' + secs + 's ago';
  }
  setInterval(tsUpdateLastUpdated, 5000);

  /* ── Track previous task statuses for transition animation ───────── */
  var _tsPrevStatuses = {};

  /* After every board rebuild, inject elapsed time badges, animate
     cards that changed columns, and refresh the activity feed. */
  var _origAdminBoardRebuild = (typeof adminBoardRebuild === 'function') ? adminBoardRebuild : null;
  adminBoardRebuild = function(tasks, columnCounts) {
    /* Snapshot current statuses before rebuild */
    var newStatuses = {};
    for (var i = 0; i < tasks.length; i++) {
      newStatuses[tasks[i].id] = tasks[i].status;
    }

    /* Forward every arg — the wrapped fn takes (tasks, columnCounts) and
       dropping the counts clobbers the wl-47 header totals. */
    if (_origAdminBoardRebuild) _origAdminBoardRebuild(tasks, columnCounts);

    /* Animate cards that moved columns */
    for (var tid in newStatuses) {
      if (_tsPrevStatuses[tid] && _tsPrevStatuses[tid] !== newStatuses[tid]) {
        var card = document.querySelector(".tb-card[data-task-id='" + tid + "']");
        if (card) {
          card.classList.add('ts-card-entering');
          card.addEventListener('animationend', function() {
            this.classList.remove('ts-card-entering');
          }, {once: true});
        }
      }
    }
    _tsPrevStatuses = newStatuses;

    /* Update last-poll timestamp */
    _tsLastPollAt = Date.now();
    tsUpdateLastUpdated();

    /* Inject elapsed time into in-progress card footers */
    var ipCards = document.querySelectorAll(".tb-card[data-status='in_progress']");
    for (var i = 0; i < ipCards.length; i++) {
      var card = ipCards[i];
      var agoSpan = card.querySelector('.tb-card-ago[data-iso]');
      if (!agoSpan) continue;
      var iso = agoSpan.getAttribute('data-iso');
      var existing = card.querySelector('.tb-elapsed');
      var elapsed = afFormatElapsed(iso);
      if (!elapsed) continue;
      if (existing) {
        existing.textContent = '\u23F1 ' + elapsed;
      } else {
        var foot = card.querySelector('.tb-card-meta');
        if (foot) {
          var badge = document.createElement('span');
          badge.className = 'tb-elapsed';
          badge.textContent = '\u23F1 ' + elapsed;
          foot.appendChild(badge);
        }
      }
    }
    tsFetchBoardSummary();
    tsFetchAttentionSummary();
    tsFetchScopeNavCounts();
  };

  /* Tick elapsed timers every 30s */
  setInterval(function() {
    var ipCards = document.querySelectorAll(".tb-card[data-status='in_progress']");
    for (var i = 0; i < ipCards.length; i++) {
      var card = ipCards[i];
      var agoSpan = card.querySelector('.tb-card-ago[data-iso]');
      if (!agoSpan) continue;
      var iso = agoSpan.getAttribute('data-iso');
      var el = card.querySelector('.tb-elapsed');
      if (el) el.textContent = '\u23F1 ' + afFormatElapsed(iso);
    }
  }, 30000);

  /* ── Header pills: ready / in flight / stalled (wl-28) ─────────────
     wl-85: pills honor the page's declared scope (body[data-ops-scope])
     and their click-throughs land on the same scope's Board. */
  async function tsFetchBoardSummary() {
    try {
      var scope = document.body.getAttribute('data-ops-scope') || '';
      var poolPath = '/admin/tickets/' + (scope || 'all');
      var resp = await fetch('/api/dev/board-summary?scope=' + encodeURIComponent(scope), {
        headers: { 'Accept': 'application/json' }
      });
      var j = await resp.json();
      var readyEl = document.getElementById('ts-ready-badge');
      var inflightEl = document.getElementById('ts-inflight-badge');
      var stalledEl = document.getElementById('ts-stalled-badge');
      if (readyEl) {
        if (j.ready_count > 0) {
          readyEl.textContent = j.ready_count + ' ready';
          readyEl.hidden = false;
          readyEl.onclick = function() { window.location.href = poolPath + '?view=table&status=backlog'; };
        } else {
          readyEl.hidden = true;
        }
      }
      if (inflightEl) {
        if (j.in_flight_count > 0) {
          inflightEl.textContent = j.in_flight_count + ' in flight';
          inflightEl.hidden = false;
          inflightEl.onclick = function() { window.location.href = poolPath + '?view=board'; };
        } else {
          inflightEl.hidden = true;
        }
      }
      if (stalledEl) {
        if ((j.stalled_count || 0) > 0) {
          stalledEl.textContent = j.stalled_count + ' stalled';
          stalledEl.hidden = false;
          stalledEl.onclick = function() {
            window.location.href = poolPath + '?view=board';
          };
        } else {
          stalledEl.hidden = true;
        }
      }
      _tsLastPollAt = Date.now();
      tsUpdateLastUpdated();
    } catch (e) { /* silent */ }
  }

  /* wl-120: per-scope ready/stalled badges in the scope switcher pills
     ("All" + each discovered store, plus overflow "More" rows) — one batch
     request populates every data-scope-badge element on the page. */
  async function tsFetchScopeNavCounts() {
    var badges = document.querySelectorAll('[data-scope-badge]');
    if (!badges.length) return;
    try {
      var resp = await fetch('/api/dev/board-summary/all-scopes', {
        headers: { 'Accept': 'application/json' }
      });
      var j = await resp.json();
      var scopes = j.scopes || {};
      for (var i = 0; i < badges.length; i++) {
        var el = badges[i];
        var slug = el.getAttribute('data-scope-badge');
        var s = scopes[slug];
        if (!s) { el.hidden = true; continue; }
        var parts = [];
        if (s.ready_count > 0) {
          parts.push('<span class="ts-seg-count ts-seg-count--ready" title="' +
            s.ready_count + ' ready">' + s.ready_count + '</span>');
        }
        if (s.stalled_count > 0) {
          parts.push('<span class="ts-seg-count ts-seg-count--stalled" title="' +
            s.stalled_count + ' stalled">' + s.stalled_count + '</span>');
        }
        if (parts.length) {
          el.innerHTML = parts.join('');
          el.hidden = false;
        } else {
          el.hidden = true;
        }
      }
    } catch (e) { /* silent */ }
  }

  /* wl-135: founder-attention chip — always all-store, unlike the
     scope-aware pills above (no ?scope= — same "all stores" convention as
     board-summary's scope=all). */
  async function tsFetchAttentionSummary() {
    try {
      var resp = await fetch('/api/dev/attention', { headers: { 'Accept': 'application/json' } });
      var j = await resp.json();
      var el = document.getElementById('ts-attention-badge');
      if (!el) return;
      if ((j.count || 0) > 0) {
        el.textContent = 'waiting on founder: ' + j.count;
        el.hidden = false;
        el.onclick = function() { window.location.href = '/admin/attention'; };
      } else {
        el.hidden = true;
      }
    } catch (e) { /* silent */ }
  }

  /* ── Init: load everything on page ready ─────────────────────────── */
  function tsInit() {
    _tsLastPollAt = Date.now();
    tsUpdateLastUpdated();
    tsFetchBoardSummary();
    tsFetchAttentionSummary();
    tsFetchScopeNavCounts();
    /* Snapshot initial statuses from server-rendered cards */
    var cards = document.querySelectorAll('.tb-card[data-task-id]');
    for (var i = 0; i < cards.length; i++) {
      _tsPrevStatuses[cards[i].getAttribute('data-task-id')] =
        cards[i].getAttribute('data-status');
    }
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', tsInit);
  } else {
    tsInit();
  }
</script>
"""


def _task_server_extra_css() -> str:
    """Additional CSS for task-server-only features."""
    return """
<style>
  /* ── Clickable label links ───────────────────────────────────────── */
  .af-label-link { text-decoration:none; cursor:pointer; }
  .af-label-link:hover .badge { filter:brightness(1.3); }
  .af-label-link:hover .label-chip { color: var(--accent); text-decoration-color: var(--accent); }

  /* ── Elapsed timer badge ─────────────────────────────────────────── */
  .tb-elapsed {
    font-family:var(--font-mono, monospace);
    font-size:10px; color:var(--neon);
    font-weight:600; letter-spacing:.03em;
    background:color-mix(in srgb, var(--neon) 10%, transparent);
    border-radius:3px; padding:1px 5px;
    margin-left:auto;
  }

  /* ── Layout overrides: tighter cards, prominent counts ───────────── */
  .tb-card-head { margin-bottom:2px; }
  .tb-col-head h3 { font-size:13px !important; }
  .tb-card-meta .badge { font-size:var(--text-badge, 11px); padding:1px 5px; }

  /* More prominent column count badges */
  .tb-col-count {
    font-size:11px !important; font-weight:700 !important;
    color:var(--neon) !important;
    background:color-mix(in srgb, var(--neon) 10%, transparent);
    border:1px solid color-mix(in srgb, var(--neon) 28%, transparent) !important;
    padding:0 8px !important;
    min-width:22px; text-align:center;
  }

  /* Horizontal scroll when columns overflow */
  .tb-board { overflow-x:auto; }

  /* WorkLane landing + work queue shell */
  .page.page-full .tos-card { margin-bottom: 12px; }
  /* Workspace = scope & filters + detail (one dependent module); see docs/operations/ui-chrome.md */
  .ops-workspace {
    min-width: 0;
  }
  .ops-wq-dispatch-hygiene {
    margin-top: var(--ops-module-gap, 12px);
  }
  .ops-wq-dispatch-summary {
    cursor: pointer;
    list-style: none;
    font-size: var(--fs-sm, 13px);
    padding: 8px 0;
  }
  .ops-wq-dispatch-summary::-webkit-details-marker { display: none; }
  .ops-wq-dispatch-details-body {
    padding-top: 4px;
  }
  .ops-dev-browser-tools .tb-toolbar {
    display: flex;
    align-items: center;
    justify-content: flex-end;
    flex-wrap: wrap;
    gap: 8px 12px;
  }
  .ops-dq-wq-chart {
    font-size: 11px;
    text-decoration: none;
    white-space: nowrap;
    padding: 4px 8px;
    border-radius: 8px;
    border: 1px solid color-mix(in srgb, var(--border) 70%, transparent);
  }
  .ops-dq-wq-chart:hover {
    color: var(--neon);
    border-color: color-mix(in srgb, var(--neon) 35%, var(--border));
  }
  .ops-dev-hygiene-h {
    font-size: clamp(0.8125rem, 1.45vw, 0.9375rem);
    font-weight: 600;
    margin: 0 0 10px 0;
    letter-spacing: 0.02em;
    color: var(--fg);
  }
  .ts-ops-lead { margin: 0 0 8px; font-size: var(--fs-sm, 13px); line-height: 1.5; max-width: 52rem; }
  .ts-plugin-tag {
    display: inline-block; font-size: 9px; font-weight: 800; letter-spacing: 0.12em;
    text-transform: uppercase; color: var(--dim); border: 1px solid var(--border);
    border-radius: 4px; padding: 1px 6px; margin-right: 8px; vertical-align: middle;
  }
  .devq-stat-row { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 6px; align-items: stretch; }
  .devq-stat-chip {
    display: inline-flex; flex-direction: column; align-items: center; justify-content: center;
    text-decoration: none; color: inherit; min-width: 64px; padding: 6px 10px;
    border-radius: 6px; background: var(--bg); border: 1px solid var(--border);
    transition: border-color 0.15s, color 0.15s;
  }
  .devq-stat-chip:hover { border-color: var(--neon); color: var(--neon); }
  .devq-stat-num { font-variant-numeric: tabular-nums; font-weight: 700; line-height: 1.2; }
  .devq-stat-lbl { font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em; color: var(--dim); margin-top: 2px; }
  .ts-ticket-strip-lead { margin: 0 0 4px; font-size: var(--fs-sm, 13px); line-height: 1.5; max-width: 52rem; }
  .ts-ticket-mod-foot { margin: 8px 0 0; font-size: var(--fs-sm, 13px); }
  .ts-ticket-recent td:first-child { max-width: min(420px, 70vw); overflow: hidden; text-overflow: ellipsis; }
  .ts-tw-meta { margin: 0 0 10px; font-size: var(--fs-sm, 13px); line-height: 1.45; }
  .ts-tw-offline { margin: 0; font-size: var(--fs-sm, 14px); line-height: 1.55; max-width: 48rem; }
  .ts-tw-stat-grid {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
    gap: 10px; margin-bottom: 12px;
  }
  .ts-tw-stat {
    background: var(--bg, #17140f); border: 1px solid var(--border);
    border-radius: 6px; padding: 8px 10px;
  }
  .ts-tw-stat-label {
    font-size: 11px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase;
    color: var(--dim); margin-bottom: 4px;
  }
  .ts-tw-stat-value { font-size: var(--fs-lg, 18px); font-weight: 600; color: var(--fg); }
  .ts-tw-stat-sub { font-size: var(--fs-sm, 12px); font-weight: 400; margin-left: 4px; }
  .ts-tw-pill {
    display: inline-block; font-size: var(--fs-sm, 12px); font-weight: 700;
    letter-spacing: .04em; padding: 3px 10px; border-radius: 999px; border: 1px solid var(--border);
  }
  .ts-tw-pill--green { color: var(--green, #4caf7d); border-color: rgba(76, 175, 125, 0.45); background: rgba(76, 175, 125, 0.08); }
  .ts-tw-pill--yellow { color: var(--yellow, #d9a441); border-color: rgba(217, 164, 65, 0.45); background: rgba(217, 164, 65, 0.08); }
  .ts-tw-pill--red { color: var(--red, #ff3b3b); border-color: rgba(255, 59, 59, 0.45); background: rgba(255, 59, 59, 0.08); }
  .ts-tw-pill--muted { color: var(--dim); }
  .ts-tw-split {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
    gap: 12px; align-items: start;
  }
  .ts-tw-panel-title { font-size: var(--fs-md, 15px); font-weight: 600; margin: 0 0 8px 0; }
  .ts-tw-table td { font-size: var(--fs-sm, 12px); }

  /* ── Command bar (wl-36): counts + jump + filters + view, one row ── */
  .wq-cmdbar-row {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 6px 8px;
    min-height: 28px;
    padding: 0 0 2px;
  }
  .wq-cmdbar-spacer { flex: 1 1 auto; }
  .wq-jump-form {
    display: flex;
    align-items: center;
    gap: 6px;
    margin: 0;
    flex-wrap: nowrap;
  }
  .wq-jump-form { position: relative; }
  .wq-jump-form label { font-size: var(--fs-sm, 12px); }
  .wq-jump-input {
    max-width: 108px;
    height: 28px;
    box-sizing: border-box;
    font-variant-numeric: tabular-nums;
  }
  /* display:flex would otherwise beat the hidden attribute's UA
     display:none — keep the empty popover invisible (wl-82). */
  .wq-jump-ambiguous[hidden] { display: none; }
  .wq-jump-ambiguous {
    position: absolute;
    top: 32px;
    left: 0;
    z-index: 20;
    display: flex;
    flex-direction: column;
    min-width: 220px;
    max-width: 360px;
    background: var(--card, #fff);
    border: 1px solid var(--border);
    border-radius: 6px;
    box-shadow: 0 4px 16px rgba(0, 0, 0, 0.18);
    padding: 4px;
  }
  .wq-jump-ambiguous a {
    padding: 6px 8px;
    font-size: var(--fs-sm, 12px);
    border-radius: 4px;
    text-decoration: none;
    color: var(--fg);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .wq-jump-ambiguous a:hover { background: var(--raised, rgba(0, 0, 0, 0.06)); }
  .wq-adv-toggle {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    height: 28px;
    padding: 0 11px;
    font-size: var(--fs-sm, 12px);
    font-weight: 500;
    color: var(--muted);
    background: var(--bg2);
    border: 1px solid var(--border);
    border-radius: var(--r-md, 6px);
    cursor: pointer;
    transition: border-color .12s, color .12s;
  }
  .wq-adv-toggle:hover { border-color: var(--neon); color: var(--fg); }
  .wq-adv-toggle--open {
    color: var(--neon);
    border-color: color-mix(in srgb, var(--neon) 40%, transparent);
    background: color-mix(in srgb, var(--neon) 8%, transparent);
  }
  .wq-adv-caret { font-size: 9px; }
  .wq-adv-toggle--open .wq-adv-caret { transform: rotate(180deg); }
  .wq-advanced-panel {
    border: 1px solid color-mix(in srgb, var(--border) 85%, transparent);
    border-radius: 10px;
    background: color-mix(in srgb, var(--bg2) 94%, transparent);
    margin-bottom: 8px;
  }

  /* Status count chips — one-line: count then label */
  .wq-buckets {
    display: flex;
    flex-wrap: wrap;
    gap: 5px;
    align-items: center;
  }
  .wq-bucket {
    display: inline-flex;
    align-items: baseline;
    gap: 5px;
    padding: 3px 10px;
    border-radius: var(--r-md, 6px);
    text-decoration: none;
    border: 1px solid var(--border);
    background: var(--bg2);
    transition: border-color .12s, box-shadow .12s;
  }
  .wq-bucket:hover { border-color: var(--neon); color: inherit; }
  .wq-bucket--active {
    border-color: var(--neon);
    box-shadow: 0 0 0 1px color-mix(in srgb, var(--neon) 24%, transparent);
    background: color-mix(in srgb, var(--neon) 8%, transparent);
  }
  .wq-bucket-val {
    font-size: 12px;
    font-weight: 600;
    font-variant-numeric: tabular-nums;
    line-height: 1.2;
  }
  .wq-bucket-lbl {
    font-size: 9px;
    text-transform: uppercase;
    letter-spacing: .07em;
    color: var(--dim);
    line-height: 1.2;
  }
  .wq-bucket--all .wq-bucket-val { color: var(--fg); }
  .wq-bucket--backlog .wq-bucket-val { color: var(--muted); }
  .wq-bucket--progress .wq-bucket-val { color: var(--neon); }
  .wq-bucket--review .wq-bucket-val { color: var(--yellow); }
  .wq-bucket--done .wq-bucket-val { color: var(--green); }
  .wq-bucket--canceled { opacity: .92; }
  .wq-bucket--canceled .wq-bucket-val { color: var(--dim); }

  /* Work Queue tables — same density as Dev Queue batch tables */
  .ts-wq-shell .tos-table {
    font-size: clamp(12px, 1.35vw, 13px);
  }
  .ts-wq-shell .tos-table th {
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: .04em;
    color: var(--dim);
    font-weight: 600;
    padding: 8px 10px;
  }
  .ts-wq-shell .tos-table td {
    padding: 8px 10px;
    vertical-align: top;
  }

  /* View toggle lives in the command bar (wl-36) — 28px row height */
  .ts-wq-shell .tb-view-toggle {
    border-radius: var(--r-md, 6px);
    margin-bottom: 0;
  }
  .ts-wq-shell .tb-view-btn {
    padding: 4px 12px;
    font-size: var(--fs-sm, 12px);
    font-weight: 500;
    min-height: 28px;
    box-sizing: border-box;
    display: inline-flex;
    align-items: center;
  }

  .wq-advanced-panel { padding: 12px 12px 10px; }
  .wq-advanced-hint { margin: 10px 0 0; font-size: 11px; line-height: 1.4; }

  .wq-filter .wq-advanced-form.ts-filter-form {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    gap: 10px 14px;
    align-items: end;
  }
  .wq-filter .ts-filter-actions label { visibility: hidden; }
  .wq-filter-actions { min-height: 32px; }
  .wq-filter-chips {
    margin-top: 12px;
    padding-top: 10px;
    border-top: 1px solid color-mix(in srgb, var(--border) 80%, transparent);
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  .wq-chip-group { display: flex; flex-direction: column; gap: 5px; }
  .wq-chip-group-label {
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--dim);
  }
  .wq-chip-row {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    align-items: center;
  }
  .notif-filter-chip.active {
    border-color: var(--neon) !important;
    background: rgba(0, 245, 255, 0.12) !important;
    color: var(--neon) !important;
  }

  /* wl-10: facet chip groups — top-N visible, overflow + "other" collapsed
     behind a toggle; search box expands and filters everything live. */
  .wq-chip-search { margin-bottom: 2px; }
  .wq-chip-search-input { width: 100%; max-width: 320px; }
  .wq-chip-group[data-collapsed="1"] .wq-chip--overflow { display: none; }
  .wq-chip-group--other[data-collapsed="1"] .notif-filter-chip { display: none; }
  .wq-chip-more {
    display: inline-flex;
    align-items: center;
    height: 22px;
    padding: 0 8px;
    font-size: 11px;
    font-weight: 600;
    color: var(--dim);
    background: transparent;
    border: 1px dashed var(--border);
    border-radius: var(--r-md, 6px);
    cursor: pointer;
  }
  .wq-chip-more:hover { color: var(--neon); border-color: var(--neon); }
  .wq-filter-chips.wq-chips-searching .wq-chip-more { display: none; }
  .notif-filter-chip.wq-chip-search-hidden { display: none !important; }

  /* Settings: editable products (wl-17) */
  .ts-settings-input {
    width: 100%; min-width: 100px; padding: 4px 8px;
    border: 1px solid var(--border); border-radius: var(--r-md, 4px);
    background: var(--bg); color: var(--fg); font-size: var(--fs-sm);
    font-family: inherit;
  }
  .ts-settings-input--narrow { width: 80px; min-width: 60px; }
  .ts-settings-add-row {
    display: flex; gap: 8px; align-items: center; margin-top: 12px;
    flex-wrap: wrap;
  }
  .ts-settings-add-row .ts-settings-input { width: auto; flex: 1 1 160px; }

  /* wl-23: archived ticket detail is read-only */
  .ts-archive-banner {
    margin: 0 0 12px 0; padding: 10px 12px;
    border: 1px solid var(--border); border-radius: var(--r-md, 4px);
    background: rgba(255, 193, 7, 0.08); color: var(--fg);
    font-size: var(--fs-sm);
  }

  /* wl-38: Table view as a Dispatch timetable — dense row grid. */
  .ts-timetable { overflow-x: auto; }
  .ts-timetable-table {
    table-layout: fixed; width: 100%; border-collapse: collapse;
  }
  .ts-timetable-table th {
    text-align: left; text-transform: uppercase; letter-spacing: .16em;
    font-size: var(--fs-xs); font-weight: 600; color: var(--dim);
    padding: 6px 8px; border-bottom: 1px solid var(--border);
    white-space: nowrap;
  }
  .ts-timetable-table th[data-tt-key] { cursor: pointer; user-select: none; }
  .ts-timetable-table th[data-tt-key]:hover { color: var(--fg); }
  .ts-timetable-table th[data-tt-key][aria-sort] { color: var(--accent); }
  .ts-timetable-table th[data-tt-key][aria-sort]::after {
    display: inline-block; margin-left: 4px; letter-spacing: 0;
  }
  .ts-timetable-table th[data-tt-key][aria-sort="ascending"]::after { content: "\\2191"; }
  .ts-timetable-table th[data-tt-key][aria-sort="descending"]::after { content: "\\2193"; }
  .ts-timetable-table td {
    padding: 0 8px; height: 29px; border-bottom: 1px solid var(--border);
    font-size: var(--fs-sm); vertical-align: middle; overflow: hidden;
  }
  .ts-timetable-table tbody tr.tt-row { cursor: pointer; }
  .ts-timetable-table tbody tr.tt-row:nth-child(even) { background: var(--bg2); }
  .ts-timetable-table tbody tr.tt-row:hover { background: var(--hover-tint); }
  .ts-timetable-table .tt-c-age, .ts-timetable-table .tt-c-no {
    font-family: var(--font-mono); font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }
  .ts-timetable-table th.tt-c-age, .ts-timetable-table td.tt-c-age { width: 84px; }
  .ts-timetable-table th.tt-c-no, .ts-timetable-table td.tt-c-no { width: 96px; }
  .ts-timetable-table th.tt-c-status, .ts-timetable-table td.tt-c-status { width: 110px; }
  .ts-timetable-table th.tt-c-pri, .ts-timetable-table td.tt-c-pri { width: 90px; text-align: right; }
  .ts-timetable-table td.tt-c-pri { text-align: right; }
  .ts-timetable-table .tt-c-ticket, .ts-timetable-table .tt-c-labels {
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .ts-timetable-table .tt-c-ticket a {
    color: var(--fg); text-decoration: none; font-weight: 500;
  }
  .ts-timetable-table .tt-c-ticket a:hover { color: var(--accent); }
  .ts-timetable-table .tt-c-labels { font-family: var(--font-mono); color: var(--dim); }
  .ts-timetable-table .tt-c-labels .label-chip { color: inherit; text-decoration-color: var(--border); }
  /* Owner column (wl-104): same byline/claim-age/stale markup as Board cards. */
  .ts-timetable-table th.tt-c-owner, .ts-timetable-table td.tt-c-owner { width: 170px; }
  .ts-timetable-table .tt-c-owner {
    font-family: var(--font-mono); font-size: 10px; color: var(--muted);
    letter-spacing: .03em; white-space: nowrap; overflow: hidden;
    text-overflow: ellipsis; display: flex; align-items: center; gap: 4px;
  }
</style>
"""


# ── The living desk (wl-132 / wl-167 / wl-170): the kiosk interior ───────
# Ratified spec (founder verdicts on wl-132): grok's structure — nameplate,
# IN-TRAY of decision slips, hold bin of quiet claims, neighborhood-ledger
# blotter, stamp pad + FILED outbox rail — with claude's liveness (stamp
# thunk on fresh receipts, real per-item paper stack heights). CITY_DNA
# sec.5 (wl-170 / pc-52): page base is the city `page` token (#faf6ec) —
# one sheet across the rooms; paper/LAND surfaces keep plaza tones; live
# sky band unchanged. The desk keeps the ticket verbs on Board/Table/
# ticket pages; the scene renders around them, never replaces them.
# Engineering constraint proven live on the sibling rooms: setInterval,
# never requestAnimationFrame.

_SCENE_WINDOW_HOURS = 24

# ── Founder identity (wl-148) ─────────────────────────────────────────────
# The canonical §5.2 id signed at the controls plus a presentation-only
# alias. Aliases are paint, ids are identity: the SIGNED author never
# changes, the alias only changes how founder-signed entries render.
# Overlay file in the data dir, same pattern as the products overlay.

_FOUNDER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")


def _identity_config_path() -> Path:
    return wl_data_dir() / "identity.json"


def _identity_config() -> Dict[str, str]:
    cfg = {"founder_id": "founder-terminal", "founder_alias": ""}
    try:
        raw = json.loads(_identity_config_path().read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            for k in cfg:
                if isinstance(raw.get(k), str):
                    cfg[k] = raw[k]
    except (OSError, ValueError):
        pass
    return cfg


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

# Directory-board doors (pc-37 case #4): mode-aware — a standalone WorkLane
# install is ONE room and shows no doors to uninstalled rooms.
_CITYHALL_URL = os.environ.get("WL_CITYHALL_URL", "http://127.0.0.1:8796")
_WORKFORCE_URL = os.environ.get("WL_WORKFORCE_URL", "http://127.0.0.1:8797")


def _closeout_authors(slug: str) -> Dict[str, str]:
    """task_id -> author of the latest 'Completed:' close-out comment, for
    one store (wl-165 sprite chips). Same signed-comment derivation as the
    Allocation view; keyed by both the raw and prefixed id so the scene's
    composite ids always match."""
    out: Dict[str, str] = {}
    for spec, tracker in _scoped_product_trackers(slug):
        db_path = _tracker_db_path(tracker)
        if db_path is None or not Path(db_path).exists():
            continue
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                rows = conn.execute(
                    "SELECT task_id, author FROM task_comments "
                    "WHERE body LIKE 'Completed:%' AND author != '' "
                    "ORDER BY created_at",
                ).fetchall()
            finally:
                conn.close()
        except Exception:
            continue
        for tid, author in rows:
            tid = str(tid)
            out[tid] = str(author)          # later rows win: latest close-out
            out[f"{spec.prefix}-{tid}"] = str(author)
    return out


# wl-168: recent status transitions for the paper-line flyers. /api/dev/activity
# only carries new_status (no old→new pair), so the engine computes a small
# window from task_events and the scene animates it. Engines compute facts;
# scenes animate them.
_SCENE_TRANSITION_HOURS = 2
_SCENE_TRANSITION_LIMIT = 40


def _scene_recent_transitions(
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """status_change events with from_status inferred from the prior event
    that recorded a status for the same task (created or earlier change)."""
    now = now or datetime.now(timezone.utc)
    cutoff_ts = (now - timedelta(hours=_SCENE_TRANSITION_HOURS)).timestamp()
    out: List[Dict[str, Any]] = []
    for spec, tracker in _scoped_product_trackers(""):
        db_path = _tracker_db_path(tracker)
        if db_path is None or not Path(db_path).exists():
            continue
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    """
                    SELECT e.id AS event_id,
                           COALESCE(t.ext_id, CAST(t.id AS TEXT)) AS task_id,
                           e.status AS to_status,
                           e.created_at AS ts,
                           (SELECT e2.status FROM task_events e2
                             WHERE e2.task_id = e.task_id
                               AND e2.id < e.id
                               AND e2.status IS NOT NULL
                               AND e2.status != ''
                             ORDER BY e2.id DESC LIMIT 1) AS from_status,
                           (SELECT c.author FROM task_comments c
                             WHERE c.task_id = e.task_id
                               AND c.author != ''
                               AND c.created_at <= e.created_at
                             ORDER BY c.created_at DESC LIMIT 1) AS author
                      FROM task_events e
                      JOIN tasks t ON t.id = e.task_id
                     WHERE e.event_type = 'status_change'
                       AND e.status IS NOT NULL
                       AND e.status != ''
                     ORDER BY e.id DESC
                     LIMIT 60
                    """,
                ).fetchall()
            finally:
                conn.close()
        except Exception:
            continue
        for r in rows:
            to_st = str(r["to_status"] or "").strip()
            from_st = str(r["from_status"] or "").strip()
            if not to_st or from_st == to_st:
                continue
            ts = r["ts"] or ""
            if _activity_ts_sort_key(ts) < cutoff_ts:
                continue
            raw_id = str(r["task_id"])
            composite = (
                raw_id if raw_id.startswith(f"{spec.prefix}-")
                else f"{spec.prefix}-{raw_id}"
            )
            out.append({
                "id": f"{spec.slug}:{r['event_id']}",
                "task_id": composite,
                "from_status": from_st,
                "to_status": to_st,
                "author": str(r["author"] or ""),
                "ts": ts,
                "store": spec.slug,
            })
    out.sort(key=lambda x: _activity_ts_sort_key(x.get("ts")), reverse=True)
    return out[:_SCENE_TRANSITION_LIMIT]


@router.get("/api/scene")
def api_desk_scene() -> JSONResponse:
    """The desk scene's facts in one call (wl-132): per-store ledger counts,
    the founder-attention tray (wl-135 collector, unchanged), and the window
    of FILED receipts. Computed from THIS engine's own stores — the scene
    never reads the city lens (engines compute their own facts).

    wl-168: also a recent_transitions[] window (task id, from_status,
    to_status, author, ts) so the paper line can animate status movement —
    /api/dev/activity only carries new_status, not old→new pairs.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=_SCENE_WINDOW_HOURS)
    stores: List[Dict[str, Any]] = []
    filed: List[Dict[str, Any]] = []
    for spec in discover_products():
        tasks = _merged_scope_tasks_for_filters(spec.slug)
        closers = _closeout_authors(spec.slug)
        counts = {
            TaskStatus.BACKLOG: 0,
            TaskStatus.IN_PROGRESS: 0,
            TaskStatus.IN_REVIEW: 0,
        }
        done_total = 0
        for t in tasks:
            st = (t.status or "").strip()
            if st == TaskStatus.DONE:
                done_total += 1
                dt = _parse_task_date_utc(t.updated_at)
                if dt is not None and dt >= cutoff:
                    filed.append({
                        "id": t.id, "store": spec.slug, "title": t.title,
                        "closed_at": t.updated_at,
                        "author": closers.get(str(t.id), ""),
                    })
            elif st in counts:
                counts[st] += 1
        stores.append({
            "slug": spec.slug, "display": spec.display, "prefix": spec.prefix,
            "backlog": counts[TaskStatus.BACKLOG],
            "in_progress": counts[TaskStatus.IN_PROGRESS],
            "in_review": counts[TaskStatus.IN_REVIEW],
            "done_total": done_total,
            "ready": _merged_ready_count(spec.slug),
        })
    filed.sort(key=lambda f: _activity_ts_sort_key(f.get("closed_at")), reverse=True)
    payload = {
        "ok": True,
        "generated_at": now.isoformat(),
        "window_hours": _SCENE_WINDOW_HOURS,
        "stale_minutes": _claim_stale_minutes(),
        "stores": stores,
        "attention": _collect_founder_attention_items(now=now),
        "filed": filed[:60],
        "recent_transitions": _scene_recent_transitions(now=now),
    }
    resp = JSONResponse(payload)
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


_DESK_SCENE_CSS = """
/* Self-hosted IBM Plex (wl-37 pattern) — vendored under /static/fonts/, OFL. */
@font-face { font-family:"IBM Plex Sans"; font-style:normal; font-weight:400;
  font-display:swap; src:url("/static/fonts/ibm-plex-sans-400.woff2") format("woff2"); }
@font-face { font-family:"IBM Plex Sans"; font-style:normal; font-weight:600;
  font-display:swap; src:url("/static/fonts/ibm-plex-sans-600.woff2") format("woff2"); }
@font-face { font-family:"IBM Plex Sans"; font-style:normal; font-weight:700;
  font-display:swap; src:url("/static/fonts/ibm-plex-sans-700.woff2") format("woff2"); }
@font-face { font-family:"IBM Plex Mono"; font-style:normal; font-weight:400;
  font-display:swap; src:url("/static/fonts/ibm-plex-mono-400.woff2") format("woff2"); }
:root {
  /* CITY_DNA sec.5 (wl-170 / pc-52) — city page token; paper objects keep plaza */
  --desk:#faf6ec; --paper:#e2d9c2; --paper-top:#efe8d5; --line:#8c7a54;
  --rule:#9fb6d9; /* blue rules — paper-object accent, untouched */
  --ink:#1f2328; --dim:#7a6f5c; --blue:#1c4f9c; --stamp:#c0392b;
  --ok:#1e7a45; --warn:#a8681e; --pink:#fbe9ea; --pinkline:#e2b6ba;
  --gold:#e9c46a;
}
* { box-sizing:border-box; margin:0; }
html,body { height:100%; }
/* page is one sheet (wl-170); no plaza ramp on the body */
body { background:var(--desk); color:var(--ink);
  font:14px/1.5 "IBM Plex Sans",-apple-system,"Helvetica Neue",Helvetica,Arial,sans-serif;
  display:flex; flex-direction:column; }
/* night: the room darkens with the city (paintSky toggles body.night) */
body.night { background-image:linear-gradient(180deg,#9a9178 0,#a89f86 220px);
  background-color:#a89f86; }
a { color:var(--blue); text-decoration:none; } a:hover { text-decoration:underline; }
.dim { color:var(--dim); } .ok { color:var(--ok); } .warn { color:var(--warn); }
/* live sky band — same skyColors(hourF) as the plat (CITY_DNA sec.5) */
.sky { position:relative; height:48px; overflow:hidden; flex:none;
  border-bottom:1px solid var(--line);
  background:linear-gradient(180deg,#dcebf0,#eef0e2); }
.celestial { position:absolute; width:16px; height:16px; border-radius:50%;
  pointer-events:none; transition:left .8s linear, top .8s linear, right .8s linear; }
.celestial.sun { background:var(--gold); box-shadow:0 0 12px #e9c46a99; opacity:.92; }
.celestial.moon { background:#e8e4d2; opacity:.88; box-shadow:0 0 10px #e8e4d288; }
/* wl-165 city DNA: the plat's kiosk is this room's own sign, and papers
   wear their worker's citizen sprite (CITY_DNA.md — paper re-skin). */
.nameplate .kiosk { height:40px; width:auto; align-self:center; flex:none; }
.clip-item svg.citizen { height:15px; width:auto; vertical-align:-3px; margin-right:3px; }
/* wl-168: the paper line — desk counter strip between nameplate and columns;
   four stations left→right mirror the kanban flow, with live counts. */
.paper-line { position:relative; flex:none;
  background:linear-gradient(180deg,var(--paper-top),var(--paper));
  border-bottom:1px solid var(--line); box-shadow:0 2px 6px #0001;
  padding:10px 22px 12px; }
.pl-rail { display:flex; align-items:flex-end; justify-content:center; gap:4px;
  max-width:900px; margin:0 auto; position:relative; z-index:1; }
.pl-station { flex:1; min-width:0; max-width:200px; background:transparent;
  border:1px solid transparent; border-radius:4px; cursor:pointer;
  padding:8px 6px 6px; text-align:center; color:var(--ink); font:inherit; }
.pl-station:hover { border-color:var(--line); background:#fbf6ea66; }
.pl-station:focus { outline:none; border-color:var(--blue); }
.pl-obj { height:36px; display:flex; align-items:flex-end; justify-content:center; }
.pl-obj svg { height:34px; width:auto; display:block; }
.pl-label { font:600 10px "IBM Plex Sans",sans-serif; letter-spacing:.18em;
  text-transform:uppercase; color:var(--dim); margin-top:4px; }
.pl-count { font:700 16px "IBM Plex Mono",ui-monospace,monospace;
  color:var(--ink); line-height:1.2; margin-top:1px; }
.pl-arrow { color:var(--dim); font-size:14px; padding-bottom:28px; opacity:.55;
  user-select:none; flex:none; }
.pl-flyers { position:absolute; inset:0; pointer-events:none; overflow:visible; z-index:2; }
.pl-flyer { position:fixed; z-index:50; pointer-events:none;
  width:28px; height:34px; margin-left:-14px; margin-top:-17px; }
.pl-flyer .sheet { width:22px; height:28px; background:#fbf6ea; border:1px solid var(--line);
  box-shadow:1px 2px 4px #0002; border-radius:1px; position:relative; }
.pl-flyer .sheet::before { content:""; position:absolute; left:3px; right:3px; top:6px;
  height:1px; background:var(--rule); box-shadow:0 4px 0 var(--rule),0 8px 0 var(--rule); }
.pl-flyer svg.citizen { position:absolute; right:-6px; bottom:-2px; height:14px; width:auto; }
header.nameplate { background:linear-gradient(180deg,var(--paper-top),var(--paper));
  border-bottom:1px solid var(--line);
  border-top:6px solid var(--stamp); box-shadow:0 2px 8px #0002;
  padding:14px 22px 12px; display:flex; align-items:baseline; gap:14px; flex-wrap:wrap; }
h1 { font-size:19px; letter-spacing:.14em; font-weight:700; }
h1 .fn { color:var(--stamp); }
.epithet { color:var(--dim); font-size:13px; }
.badges { margin-left:auto; display:flex; gap:12px; align-items:baseline; font-size:12px;
  color:var(--dim); }
#liveChip { border:1.5px solid var(--ok); color:var(--ok); border-radius:3px;
  font-weight:700; font-size:10px; letter-spacing:.16em; padding:2px 8px;
  text-transform:uppercase; }
#liveChip.hold { border-color:var(--warn); color:var(--warn); }
#clock { font-variant-numeric:tabular-nums; }
main.surface { flex:1; overflow:auto; padding:20px 22px 26px; max-width:1420px;
  width:100%; margin:0 auto; display:grid; gap:22px;
  grid-template-columns:minmax(280px,.9fr) minmax(380px,1.35fr) minmax(280px,.9fr); }
@media (max-width:1080px){ main.surface { grid-template-columns:1fr 1fr; } }
@media (max-width:760px){ main.surface { grid-template-columns:1fr; } }
h2 { font-size:11px; letter-spacing:.24em; color:var(--dim); text-transform:uppercase;
  margin:0 0 10px; font-weight:600; }
.tray, .blotter, .pad, .clip { background:var(--paper-top); border:1px solid var(--line);
  box-shadow:0 2px 6px #0002; padding:14px 16px; margin-bottom:18px; }
/* paper objects: slips stay paper-white (stamp red / blue rules untouched) */
.form { background:#fbf6ea; border:1px solid var(--line); box-shadow:0 2px 5px #0002;
  padding:12px 14px 10px; margin-bottom:12px; position:relative; overflow:hidden; }
.form::before { content:""; position:absolute; left:0; top:0; bottom:0; width:4px;
  background:var(--blue); }
.form .no { font:600 12px "IBM Plex Mono",ui-monospace,Menlo,monospace; color:var(--blue); }
.form .t { margin:3px 0 4px; font-weight:600; font-size:13px; }
.form .meta { font-size:12px; color:var(--dim); }
.form.fresh { animation:inkin 2.4s ease-out; }
@keyframes inkin { from { background:#fff3c8; } }
.stamp { position:absolute; right:10px; top:8px; transform:rotate(6deg);
  border:2.5px solid var(--stamp); color:var(--stamp); border-radius:4px;
  font:800 10px/1 "IBM Plex Sans",sans-serif; letter-spacing:.18em;
  padding:4px 7px; text-transform:uppercase; opacity:.85;
  mask-image:radial-gradient(circle at 30% 40%, #000 92%, #0008 100%); }
.stamp.green { border-color:var(--ok); color:var(--ok); }
.stamp.amber { border-color:var(--warn); color:var(--warn); }
.slip { background:var(--pink); border-color:var(--pinkline); }
.slip::before { background:var(--stamp); }
.tag { border:1px solid var(--line); border-radius:3px; padding:0 6px; font-size:11px;
  color:var(--dim); background:var(--paper-top); }
.empty-note { color:var(--dim); font-size:12px; font-style:italic; padding:6px 0; }
/* wl-157/wl-162: the skim filter — narrows both trays to one store. A
   dropdown in the tray corner, not a chip row: flat rows wrap past ~4
   stores and shove the whole column below its neighbors (wl-117 redux). */
.tray-head { display:flex; align-items:baseline; justify-content:space-between; gap:8px; }
.tray-head h2 { flex:1; min-width:0; }
/* wl-164: the closed control dresses as header type — quiet uppercase
   label + caret, no box; the option list keeps the per-store counts. */
#trayFilter { appearance:none; -webkit-appearance:none; border:1px solid transparent;
  background:transparent url("data:image/svg+xml;charset=utf-8,%3Csvg xmlns='http://www.w3.org/2000/svg' width='8' height='5'%3E%3Cpath d='M0 0l4 5 4-5z' fill='%23848a80'/%3E%3C/svg%3E") no-repeat right 4px center;
  color:var(--dim); font:600 10.5px "IBM Plex Sans",sans-serif;
  text-transform:uppercase; letter-spacing:.14em; border-radius:3px;
  padding:2px 17px 2px 5px; cursor:pointer; max-width:50%; }
#trayFilter:hover { border-color:var(--line); color:var(--blue); }
#trayFilter:focus { border-color:var(--blue); color:var(--blue); outline:none; }
#trayFilter.on { color:var(--blue); }
/* the blotter: one ledger card per store, paper piles with real heights */
.hood { border:1px solid var(--line); background:var(--paper-top); box-shadow:0 2px 5px #0002;
  padding:12px 16px 10px; margin-bottom:12px; }
.hood-head { display:flex; justify-content:space-between; align-items:baseline; gap:8px; }
.hood-head .nm { font-weight:700; letter-spacing:.03em; }
.hood-head .nm a { color:var(--ink); } .hood-head .nm a:hover { color:var(--blue); }
.ready-chip { font-size:11px; color:var(--warn); border:1px solid var(--warn);
  border-radius:3px; padding:0 7px; white-space:nowrap; }
.ready-chip.zero { color:var(--dim); border-color:var(--line); }
.piles { display:flex; gap:18px; margin-top:10px; align-items:flex-end; }
.pile { flex:1; text-align:center; }
.pile .sheets { position:relative; height:56px; }
/* pile sheets are paper objects — keep the white carbon look */
.pile .sheet { position:absolute; left:12%; right:12%; height:4px; border-radius:1px;
  background:#fbf6ea; border:1px solid #c9bd9f; box-shadow:0 1px 1px #0001; }
.pile.doing .sheet { background:#fef7e0; border-color:#e3d9ae; }
.pile.review .sheet { background:#e8effa; border-color:#b9cdec; }
.pile .n { font:600 15px "IBM Plex Mono",ui-monospace,monospace; margin-top:2px; }
.pile .l { font-size:10px; letter-spacing:.18em; text-transform:uppercase; color:var(--dim); }
/* stamp pad + outbox */
.pad { text-align:center; position:relative; }
.rubber { display:inline-block; margin:10px auto 4px; border:3px solid var(--stamp);
  color:var(--stamp); border-radius:6px; font-weight:800; font-size:15px;
  letter-spacing:.24em; padding:10px 18px; text-transform:uppercase;
  transform:rotate(-4deg); transition:transform .08s;
  mask-image:radial-gradient(circle at 35% 45%, #000 90%, #0007 100%); }
.rubber.thunk { animation:thunk .5s ease-in; }
@keyframes thunk { 30% { transform:rotate(-4deg) translateY(10px) scale(.97); }
  45% { transform:rotate(-4deg) translateY(10px) scaleY(.9); }
  75% { transform:rotate(-4deg); } }
.inkring { position:absolute; left:50%; top:58px; width:30px; height:30px; margin-left:-15px;
  border:2.5px dashed var(--stamp); border-radius:50%; opacity:0; pointer-events:none; }
.inkring.show { animation:inkfade 2.2s ease-out; }
@keyframes inkfade { 10% { opacity:.7; } 100% { opacity:0; } }
.pad-stats { font-size:12px; color:var(--dim); margin-top:6px; }
.pad-stats .n { font:700 20px "IBM Plex Mono",ui-monospace,monospace; color:var(--ink);
  display:block; }
.clip-list { max-height:430px; overflow-y:auto; }
.clip-item { padding:6px 2px; border-bottom:1px dashed var(--line); font-size:12px; }
.clip-item:last-child { border-bottom:0; }
.clip-item .when { color:var(--dim); font-variant-numeric:tabular-nums;
  font:11px "IBM Plex Mono",ui-monospace,monospace; }
.clip-item.fresh { animation:inkin 2.4s ease-out; }
footer.bar { border-top:1px solid var(--line); background:var(--paper-top);
  padding:8px 22px; display:flex; justify-content:space-between; gap:14px;
  flex-wrap:wrap; font-size:12px; color:var(--dim); }
footer.bar a { margin-right:14px; }
/* the work-order drawer (wl-145): the ticket pulled out ON the desk,
   never a page exit — parchment chrome, paper body */
#scrim { position:fixed; inset:0; background:#4a3f2c40; opacity:0;
  pointer-events:none; transition:opacity .18s; z-index:40; }
#scrim.open { opacity:1; pointer-events:auto; }
#wo { position:fixed; top:0; right:0; bottom:0; width:min(580px,94vw);
  background:var(--paper-top); border-left:1px solid var(--line);
  box-shadow:-8px 0 28px #0003; transform:translateX(102%);
  transition:transform .22s ease-out; z-index:41; display:flex;
  flex-direction:column; }
#wo.open { transform:translateX(0); }
.wo-head { padding:16px 20px 12px; border-bottom:2px solid var(--rule);
  position:relative; background:linear-gradient(180deg,var(--paper-top),var(--paper)); }
.wo-head .no { font:600 13px "IBM Plex Mono",ui-monospace,monospace; color:var(--blue); }
.wo-head .t { font-weight:700; font-size:15px; margin:4px 90px 2px 0; }
.wo-head .stamp { top:14px; right:44px; font-size:11px; padding:5px 9px; }
.wo-close { position:absolute; top:10px; right:10px; border:1px solid var(--line);
  background:var(--paper-top); color:var(--dim); width:26px; height:26px; border-radius:3px;
  font-size:14px; cursor:pointer; line-height:1; }
.wo-close:hover { color:var(--ink); border-color:var(--dim); }
.wo-body { flex:1; overflow-y:auto; padding:14px 20px 20px; }
.wo-meta { width:100%; border-collapse:collapse; font-size:12px; margin-bottom:12px; }
.wo-meta td { padding:4px 0; border-bottom:1px dotted var(--line); vertical-align:top; }
.wo-meta td:first-child { color:var(--dim); width:86px; text-transform:uppercase;
  letter-spacing:.14em; font-size:10px; padding-top:6px; }
.wo-desc { font-size:13px; white-space:pre-wrap; background:#fbf6ea;
  border:1px solid var(--line); border-left:4px solid var(--rule);
  padding:10px 12px; margin-bottom:16px; overflow-wrap:break-word; }
.wo-entry { border:1px solid var(--line); border-left:3px solid var(--rule);
  background:#fbf6ea; padding:8px 12px; margin-bottom:10px; font-size:12.5px; }
.wo-entry .who { font:600 11px "IBM Plex Mono",ui-monospace,monospace;
  color:var(--blue); }
.wo-entry .body { white-space:pre-wrap; margin-top:4px; overflow-wrap:break-word; }
.wo-sign { border-top:1px solid var(--line); padding:10px 20px 14px;
  background:var(--paper); }
.wo-sign .sign-as { font-size:10px; letter-spacing:.18em; text-transform:uppercase;
  color:var(--dim); margin-bottom:6px; }
.wo-sign .sign-as .dim { text-transform:none; letter-spacing:0; }
.wo-sign textarea { width:100%; border:1px solid var(--line);
  background:#fbf6ea; color:var(--ink); font:12.5px "IBM Plex Sans",sans-serif;
  padding:6px 8px; margin-bottom:6px; box-sizing:border-box; }
.wo-sign textarea { min-height:58px; resize:vertical; }
.wo-sign button { border:1.5px solid var(--blue); color:var(--blue);
  background:var(--paper-top); font:600 11px "IBM Plex Sans",sans-serif;
  letter-spacing:.14em; text-transform:uppercase; padding:6px 14px;
  border-radius:3px; cursor:pointer; }
.wo-sign button:hover { background:#eef3fb; }
.wo-sign button:disabled { color:var(--dim); border-color:var(--line); cursor:default; }
.wo-sign .err-note { color:var(--stamp); font-size:11.5px; margin-top:4px; }
.wo-foot { font-size:11.5px; color:var(--dim); padding:0 20px 12px; }
"""

# setInterval only — requestAnimationFrame suspends in background panes (the
# constraint proven live on the city-hall and dispatch scenes).
_DESK_SCENE_JS = """
<script>
"use strict";
var SCENE=null, seenFiled={}, firstPoll=true;
function esc(s){return String(s==null?"":s).replace(/[&<>"']/g,function(c){
  return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];});}
function $(id){return document.getElementById(id);}
function ago(iso){if(!iso)return "";var t=Date.parse(iso);if(isNaN(t))return "";
  var s=Math.max(0,(Date.now()-t)/1000|0);
  if(s>=86400)return (s/86400|0)+"d ago"; if(s>=3600)return (s/3600|0)+"h ago";
  if(s>=60)return (s/60|0)+"m ago"; return s+"s ago";}
function sheets(n,cap){var out="",m=Math.min(n,cap||12);
  for(var i=0;i<m;i++){var rot=((i*37)%7)-3;
    out+='<div class="sheet" style="bottom:'+(i*4)+'px;transform:rotate('+(rot/3)+'deg)"></div>';}
  return out;}
function pile(cls,label,n){
  return '<div class="pile '+cls+'"><div class="sheets">'+sheets(n)+'</div>'+
    '<div class="n">'+esc(n)+'</div><div class="l">'+esc(label)+'</div></div>';}
/* wl-168: paper-line stations — status → station id for counts & flyers */
var PL_STATUS={backlog:"plFiled",in_progress:"plClaimed",in_review:"plSignoff",done:"plSigned"};
var PL_SEEN={}, PL_ACTIVE=0, PL_MAX=6, PL_MS=1500, firstPlPoll=true;
function plCounts(d){
  var c={filed:0,claimed:0,signoff:0,signed:0};
  (d.stores||[]).forEach(function(s){
    if(TRAY_F!=="all"&&s.slug!==TRAY_F)return;
    c.filed+=s.backlog||0; c.claimed+=s.in_progress||0; c.signoff+=s.in_review||0;});
  (d.filed||[]).forEach(function(f){
    if(TRAY_F!=="all"&&f.store!==TRAY_F)return; c.signed++;});
  return c;}
function renderPaperLine(d){
  var c=plCounts(d);
  var n=$("plFiledN"); if(n)n.textContent=c.filed;
  n=$("plClaimedN"); if(n)n.textContent=c.claimed;
  n=$("plSignoffN"); if(n)n.textContent=c.signoff;
  n=$("plSignedN"); if(n)n.textContent=c.signed;
  /* first poll seeds seen-set so we only animate transitions after load */
  var trs=d.recent_transitions||[];
  if(firstPlPoll){trs.forEach(function(t){if(t.id)PL_SEEN[t.id]=1;}); firstPlPoll=false; return;}
  trs.slice().reverse().forEach(function(t){ /* oldest first so flyers chain L→R */
    if(!t||!t.id||PL_SEEN[t.id])return;
    PL_SEEN[t.id]=1;
    if(TRAY_F!=="all"&&t.store&&t.store!==TRAY_F)return;
    flyPaper(t);});}
function stationCenter(status){
  var id=PL_STATUS[status]; if(!id)return null;
  var el=$(id); if(!el)return null;
  var obj=el.querySelector(".pl-obj")||el;
  var r=obj.getBoundingClientRect();
  return {x:r.left+r.width/2, y:r.top+r.height/2};}
function flyPaper(tr){
  if(PL_ACTIVE>=PL_MAX)return;
  var a=stationCenter(tr.from_status), b=stationCenter(tr.to_status);
  if(!a||!b)return;
  if(Math.abs(a.x-b.x)<4&&Math.abs(a.y-b.y)<4)return;
  PL_ACTIVE++;
  var el=document.createElement("div");
  el.className="pl-flyer";
  el.innerHTML='<div class="sheet"></div>'+spriteChip(tr.author||"");
  el.style.left=a.x+"px"; el.style.top=a.y+"px"; el.style.opacity="1";
  document.body.appendChild(el);
  var t0=Date.now();
  var iv=setInterval(function(){
    var p=Math.min(1,(Date.now()-t0)/PL_MS);
    var e=p*p*(3-2*p); /* smoothstep */
    var x=a.x+(b.x-a.x)*e, y=a.y+(b.y-a.y)*e - Math.sin(p*Math.PI)*18;
    el.style.left=x+"px"; el.style.top=y+"px";
    if(p>=0.85)el.style.opacity=String(Math.max(0,(1-p)/0.15));
    if(p>=1){clearInterval(iv); if(el.parentNode)el.parentNode.removeChild(el); PL_ACTIVE--;}
  },32);}
function stampFor(kind){
  if(kind==="in_review")return {txt:"SIGN-OFF DUE",cls:""};
  if(kind==="founder_decision")return {txt:"AWAITING SIGNATURE",cls:""};
  if(kind==="human_gate")return {txt:"AT THE WINDOW",cls:"amber"};
  if(kind==="timer")return {txt:"EMBARGO",cls:"amber"};
  return {txt:"GONE QUIET",cls:""};}
function formHtml(it,slip){
  var st=stampFor(it.kind);
  return '<div class="form'+(slip?' slip':'')+'">'+
    '<div class="stamp '+st.cls+'">'+esc(st.txt)+'</div>'+
    '<div class="no"><a href="'+esc(it.url||("/admin/tasks/"+it.id))+'">'+
      esc(it.id)+'</a> \\u00b7 P'+esc(it.priority)+
      ' \\u00b7 <span class="tag">'+esc(it.product)+'</span></div>'+
    '<div class="t">'+esc(it.title)+'</div>'+
    '<div class="meta">'+esc(it.note||"")+
      (it.waiting_since?' \\u00b7 sitting '+esc(ago(it.waiting_since)):'')+'</div></div>';}
/* City DNA (pc-40 / CITY_DNA.md): identity registry shared with the plat
   and dispatch — same hash, same palette, same little person everywhere.
   founder-terminal and non-roster authors wear the gold founder chip. */
var DNA_PALETTE=["#3d7a6a","#a8842c","#4a6fa5","#7d5185","#a35b3a","#5f7d3a"];
function dnaHash(s){var h=0,i;for(i=0;i<s.length;i++)h=(h*31+s.charCodeAt(i))|0;return Math.abs(h);}
function spriteChip(author){
  var a=String(author||""); if(!a)return "";
  var roster=/^claude-/.test(a)||/^(grok|codex|cursor)$/.test(a);
  var col=roster?DNA_PALETTE[dnaHash(a)%DNA_PALETTE.length]:"#e9c46a";
  return '<svg class="citizen" viewBox="-5 -23 10 24" aria-hidden="true"><title>'+esc(a)+'</title>'+
    '<line x1="-2" y1="-4" x2="-2" y2="0" stroke="#4a3f2c" stroke-width="2"/>'+
    '<line x1="2" y1="-4" x2="2" y2="0" stroke="#4a3f2c" stroke-width="2"/>'+
    '<rect x="-4" y="-14" width="8" height="11" rx="2.5" fill="'+col+'"/>'+
    '<circle cx="0" cy="-18" r="4.2" fill="#d9b98c" stroke="#4a3f2c" stroke-width=".7"/></svg>';}
var TRAY_F=localStorage.getItem("wl_desk_tray_filter")||"all";
function render(){
  var d=SCENE; if(!d)return;
  var att=d.attention||[];
  /* wl-157/wl-162: per-store skim dropdown — one pick narrows both trays */
  var counts={}; att.forEach(function(it){counts[it.product]=(counts[it.product]||0)+1;});
  if(TRAY_F!=="all"&&!counts[TRAY_F])TRAY_F="all";
  var tf=$("trayFilter");
  if(document.activeElement!==tf){ /* don't rebuild under an open picker */
    var fz='<option value="all"'+(TRAY_F==="all"?" selected":"")+'>all \\u00b7 '+att.length+'</option>';
    Object.keys(counts).sort().forEach(function(s){
      fz+='<option value="'+esc(s)+'"'+(TRAY_F===s?" selected":"")+'>'+
        esc(s)+' \\u00b7 '+counts[s]+'</option>';});
    tf.innerHTML=fz;}
  tf.classList.toggle("on",TRAY_F!=="all");
  var inTray=[], hold=[];
  att.forEach(function(it){
    if(TRAY_F!=="all"&&it.product!==TRAY_F)return;
    (it.kind==="stalled"?hold:inTray).push(it); });
  $("decisionsStack").innerHTML = inTray.length
    ? inTray.map(function(it){return formHtml(it,false);}).join("")
    : '<div class="empty-note">Tray empty \\u2014 nothing to sign</div>';
  $("staleStack").innerHTML = hold.length
    ? hold.map(function(it){return formHtml(it,true);}).join("")
    : '<div class="empty-note">Nothing gone quiet</div>';
  $("inCount").textContent=inTray.length; $("holdCount").textContent=hold.length;

  var hz="";
  (d.stores||[]).forEach(function(s){
    var open=s.backlog+s.in_progress+s.in_review;
    hz+='<div class="hood"><div class="hood-head">'+
      '<span class="nm"><a href="/admin/tickets/'+esc(s.slug)+'">'+esc(s.display||s.slug)+'</a>'+
      ' <span class="tag">'+esc(s.prefix)+'-</span></span>'+
      '<span class="ready-chip'+(s.ready?'':' zero')+'">'+esc(s.ready)+' ready</span></div>'+
      '<div class="piles">'+pile("backlog","filed",s.backlog)+
      pile("doing","claimed",s.in_progress)+pile("review","sign-off",s.in_review)+
      '</div>'+
      '<div class="empty-note" style="margin-top:6px">'+esc(open)+' open work orders \\u00b7 '+
      esc(s.done_total)+' signed off, ever</div></div>';});
  $("hoodList").innerHTML = hz || '<div class="empty-note">No ledgers yet \\u2014 no stores discovered</div>';

  var filed=d.filed||[], freshCount=0, cz="";
  filed.forEach(function(f){
    var fresh=!firstPoll && !seenFiled[f.id]; if(fresh)freshCount++;
    cz+='<div class="clip-item'+(fresh?' fresh':'')+'">'+spriteChip(f.author)+
      '<span class="when">'+esc(ago(f.closed_at))+'</span> '+
      '<a href="/admin/tasks/'+esc(f.id)+'">'+esc(f.id)+'</a> '+
      '<span class="dim">['+esc(f.store)+']</span> '+esc(String(f.title).slice(0,64))+'</div>';});
  $("shippedClip").innerHTML = cz || '<div class="clip-item empty-note">No carbon copies in the window</div>';
  filed.forEach(function(f){seenFiled[f.id]=1;});
  $("padCount").textContent=filed.length;
  $("padWindow").textContent="filed \\u00b7 last "+(d.window_hours||24)+"h";
  if(freshCount>0)thunk();
  renderPaperLine(d);
  firstPoll=false;}
function thunk(){
  var r=$("rubberStamp"), ink=$("inkRing"); if(!r)return;
  r.classList.remove("thunk"); ink.classList.remove("show");
  void r.getBoundingClientRect();
  r.classList.add("thunk"); ink.classList.add("show");}
function poll(){
  fetch("/api/scene",{cache:"no-store"}).then(function(r){
    if(!r.ok)throw 0; return r.json();
  }).then(function(d){SCENE=d; render();
    $("liveChip").className=""; $("liveChip").textContent="LIVE";
  }).catch(function(){
    $("liveChip").className="hold"; $("liveChip").textContent=SCENE?"HOLDING":"NO SIGNAL";});}
poll(); setInterval(poll,15000);
/* CITY_DNA sec.5 (wl-167): live sky — same formula as the plat; sun/moon
   ride the wall clock. Re-derived on setInterval (never rAF). */
function skyColors(hourF){
  if(hourF<5.5||hourF>=21)return ["#232c3a","#3a4152"];
  if(hourF<8)return ["#d9a06a","#e8d3b0"];
  if(hourF<17)return ["#dcebf0","#eef0e2"];
  if(hourF<21)return ["#d98a5f","#e3c9a0"];
  return ["#232c3a","#3a4152"];}
function paintSky(){
  var now=new Date(), hourF=now.getHours()+now.getMinutes()/60;
  var c=skyColors(hourF), night=hourF<5.5||hourF>=21;
  var sky=$("sky"); if(sky)sky.style.background="linear-gradient(180deg,"+c[0]+","+c[1]+")";
  document.body.classList.toggle("night", night);
  var cel=$("celestial"); if(!cel)return;
  if(night){
    cel.className="celestial moon";
    cel.style.left="auto"; cel.style.right="12%"; cel.style.top="22%";
  }else{
    var dayF=Math.min(1,Math.max(0,(hourF-6)/15));
    cel.className="celestial sun";
    cel.style.right="auto";
    cel.style.left=(6+dayF*82)+"%";
    cel.style.top=(58-Math.sin(dayF*Math.PI)*40)+"%";
  }}
setInterval(function(){
  var n=new Date();
  $("clock").textContent=n.toLocaleTimeString([],{hour:"2-digit",minute:"2-digit",second:"2-digit"});
  paintSky();
},1000);
paintSky();

/* ── the work-order drawer (wl-145): tickets open ON the desk ── */
var WO_ID=null;
function stampForStatus(st){
  if(st==="backlog")return {txt:"FILED",cls:""};
  if(st==="in_progress")return {txt:"CLAIMED",cls:"amber"};
  if(st==="in_review")return {txt:"SIGN-OFF DUE",cls:""};
  if(st==="done")return {txt:"SIGNED OFF",cls:"green"};
  if(st==="canceled")return {txt:"CANCELED",cls:"amber"};
  return {txt:String(st||"?").toUpperCase(),cls:""};}
function closeBtnHtml(){return '<button class="wo-close" onclick="closeWO()" title="close (esc)">\\u00d7</button>';}
function closeWO(){WO_ID=null;$("wo").classList.remove("open");$("scrim").classList.remove("open");}
function openWO(id){
  WO_ID=id; $("scrim").classList.add("open"); $("wo").classList.add("open");
  $("woHead").innerHTML='<div class="no">'+esc(id)+'</div>'+
    '<div class="t">pulling the carbon\\u2026</div>'+closeBtnHtml();
  $("woBody").innerHTML='<div class="empty-note">pulling the record\\u2026</div>';
  $("woFoot").innerHTML="";
  fetchWO(id);}
function fetchWO(id){
  fetch("/api/admin/tasks/"+encodeURIComponent(id),{cache:"no-store"})
  .then(function(r){return r.json();}).then(function(d){
    if(WO_ID!==id)return;
    if(!d.ok){$("woBody").innerHTML='<div class="empty-note">no such record</div>';return;}
    renderWO(d.task);})
  .catch(function(){if(WO_ID===id)
    $("woBody").innerHTML='<div class="empty-note">record unreachable \\u2014 try the full page</div>';});}
function renderWO(t){
  var st=stampForStatus(t.status);
  $("woHead").innerHTML='<div class="stamp '+st.cls+'">'+esc(st.txt)+'</div>'+
    '<div class="no">'+esc(t.id)+'</div>'+
    '<div class="t">'+esc(t.title||"")+'</div>'+closeBtnHtml();
  var labels=(t.labels||[]).map(function(l){return '<span class="tag">'+esc(l)+'</span>';}).join(" ");
  var h='<table class="wo-meta">'+
    '<tr><td>priority</td><td>P'+esc(t.priority!=null?t.priority:"3")+'</td></tr>'+
    '<tr><td>routing</td><td>'+(labels||'<span class="dim">none</span>')+'</td></tr>'+
    '<tr><td>filed</td><td>'+esc(t.created_at||"\\u2014")+'</td></tr>'+
    '<tr><td>last touch</td><td>'+esc(ago(t.updated_at)||t.updated_at||"\\u2014")+'</td></tr>'+
    '</table>';
  if(t.description)h+='<div class="wo-desc">'+esc(t.description)+'</div>';
  var cs=(t.comments||[]).slice().reverse();
  h+='<h2>Day book \\u00b7 '+cs.length+'</h2>';
  if(!cs.length)h+='<div class="empty-note">no entries yet</div>';
  cs.forEach(function(c){h+='<div class="wo-entry"><span class="who">'+signer(c.author)+
    '</span> <span class="dim">\\u00b7 '+esc(ago(c.created_at)||c.created_at||"")+'</span>'+
    '<div class="body">'+esc(c.body||"")+'</div></div>';});
  $("woBody").innerHTML=h;
  $("woFoot").innerHTML='verbs beyond a signed note live on <a href="/admin/tasks/'+
    esc(t.id)+'">the full record \\u2197</a>';
  $("woSignAs").innerHTML='SIGNED AS '+signer(window.FOUNDER&&FOUNDER.founder_id||"founder-terminal");
  $("woErr").textContent="";}
function signer(a){
  /* wl-148: aliases are paint, ids are identity — the alias renders,
     the canonical signed id stays visible */
  if(window.FOUNDER&&a===FOUNDER.founder_id&&FOUNDER.founder_alias)
    return esc(FOUNDER.founder_alias)+' <span class="dim">('+esc(a)+')</span>';
  return esc(a||"unsigned");}
function signWO(){
  if(!WO_ID)return;
  /* wl-150: the desk signs for the founder — whoever clicked IS the founder;
     other identities sign via MCP/CLI, never this chair */
  var author=(window.FOUNDER&&FOUNDER.founder_id)||"founder-terminal";
  var body=$("woNote").value.trim(), b=$("woSignBtn");
  if(!body){$("woErr").textContent="nothing to file \\u2014 write the note first";return;}
  b.disabled=true; $("woErr").textContent="";
  fetch("/api/admin/tasks/"+encodeURIComponent(WO_ID)+"/comments",{method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({body:body,author:author})})
  .then(function(r){return r.json();})
  .then(function(j){b.disabled=false;
    if(!j.ok){$("woErr").textContent=j.error||"the desk refused the note";return;}
    $("woNote").value=""; fetchWO(WO_ID); poll();})
  .catch(function(){b.disabled=false;
    $("woErr").textContent="desk unreachable \\u2014 note not filed";});}
document.addEventListener("click",function(e){
  if(e.metaKey||e.ctrlKey||e.shiftKey||e.altKey)return;
  var a=e.target&&e.target.closest?e.target.closest('a[href^="/admin/tasks/"]'):null;
  if(!a)return;
  if(a.closest("#wo"))return; /* the full-record link is the escape hatch */
  var id=a.getAttribute("href").slice("/admin/tasks/".length).split("?")[0];
  if(!id)return;
  e.preventDefault(); openWO(decodeURIComponent(id));});
document.addEventListener("keydown",function(e){if(e.key==="Escape")closeWO();});
$("trayFilter").addEventListener("change",function(){
  TRAY_F=this.value||"all";
  localStorage.setItem("wl_desk_tray_filter",TRAY_F);
  this.blur(); /* release focus so the next poll may rebuild the options */
  render();});
/* wl-168: station click → classic Board filtered to that status (escape hatch) */
document.querySelectorAll(".pl-station").forEach(function(btn){
  btn.addEventListener("click",function(){
    var st=btn.getAttribute("data-status")||"";
    if(!st)return;
    var base=(TRAY_F&&TRAY_F!=="all")?("/admin/tickets/"+encodeURIComponent(TRAY_F)):"/admin/tickets/all";
    window.location.href=base+"?status="+encodeURIComponent(st);
  });});
</script>
"""


@router.get("/admin/desk", response_class=HTMLResponse)
def admin_desk() -> str:
    """The work-order desk as a live model (wl-132): the room you walk into.
    Self-contained page polling /api/scene; the ticket verbs stay on the
    Board/Table/ticket pages, one click away. Mode-aware branding (wl-134)
    and mode-aware directory doors (pc-37 case #4)."""
    # Third naming amendment (pc-39, ratified 2026-07-14): the room name
    # leads alone; suite + engine share one quiet subtitle; the epithet
    # sentence retired to the room guide. Standalone keeps the engine brand,
    # no suite line.
    if _BRAND_MODE == "city":
        h1 = "<span class='fn'>TICKET DESK</span> · TICKETS"
        epithet = "ProtocolCity · powered by WorkLane"
        doors = (
            f"<a href='{_esc(_CITYHALL_URL)}'>City Hall — Projects</a>"
            f"<a href='{_esc(_WORKFORCE_URL)}'>Dispatch — Workers</a>"
        )
    else:
        h1 = "WORKLANE — <span class='fn'>TICKETS</span>"
        epithet = "the work-order desk"
        doors = ""  # standalone: one room, no doors
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(_BRAND_NAME)}</title>
<style>{_DESK_SCENE_CSS}</style></head><body>
<div class="sky" id="sky" aria-hidden="true">
  <div class="celestial sun" id="celestial"></div>
</div>
<header class="nameplate">
  <svg class="kiosk" viewBox="-38 -60 76 62" aria-hidden="true">
    <!-- wl-165 city DNA: the plat's kiosk (CITY_DNA sec.3), ink line-art;
         the awning keeps civic green as the desk's one accent. On the plat,
         papers fly INTO this kiosk — this room is its interior. -->
    <rect x="-30" y="-24" width="60" height="24" fill="none" stroke="var(--ink)"/>
    <rect x="-34" y="-29" width="68" height="5" fill="none" stroke="var(--ink)" stroke-width=".8"/>
    <line x1="-30" y1="-56" x2="-30" y2="-29" stroke="var(--ink)" stroke-width="2"/>
    <line x1="30" y1="-56" x2="30" y2="-29" stroke="var(--ink)" stroke-width="2"/>
    <path d="M-36 -56 h72 l-5 11 h-62 z" fill="#3d7a6a" stroke="var(--ink)" stroke-width=".8"/>
    <line x1="-16" y1="-56" x2="-17.5" y2="-45" stroke="#f3ecdd" stroke-width="4"/>
    <line x1="0" y1="-56" x2="0" y2="-45" stroke="#f3ecdd" stroke-width="4"/>
    <line x1="16" y1="-56" x2="17.5" y2="-45" stroke="#f3ecdd" stroke-width="4"/>
    <rect x="-20" y="-33" width="13" height="4" fill="#fbf6ea" stroke="var(--ink)" stroke-width=".5"/>
    <circle cx="14" cy="-32" r="3" fill="#e9c46a"/>
  </svg>
  <h1>{h1}</h1>
  <span class="epithet">{_esc(epithet)}</span>
  <div class="badges"><span id="clock">—:—:—</span><span id="liveChip" class="hold">NO SIGNAL</span></div>
</header>
<!-- wl-168: the paper line — desk counter between nameplate and the three columns;
     FILED → CLAIMED → SIGN-OFF DUE → SIGNED, live counts, click opens Board. -->
<div class="paper-line" id="paperLine" aria-label="the paper line · work-order stations">
  <div class="pl-rail">
    <button type="button" class="pl-station" id="plFiled" data-status="backlog"
      title="open Board · filed (backlog)">
      <div class="pl-obj" aria-hidden="true">
        <svg viewBox="0 0 48 36" fill="none" stroke="var(--ink)" stroke-width="1.2">
          <rect x="10" y="18" width="28" height="12" fill="#fbf6ea"/>
          <rect x="12" y="12" width="28" height="12" fill="#fbf6ea"/>
          <rect x="14" y="6" width="28" height="12" fill="#fbf6ea"/>
          <line x1="18" y1="10" x2="36" y2="10" stroke="var(--rule)" stroke-width=".8"/>
          <line x1="18" y1="13" x2="34" y2="13" stroke="var(--rule)" stroke-width=".8"/>
        </svg>
      </div>
      <div class="pl-label">Filed</div>
      <div class="pl-count" id="plFiledN">0</div>
    </button>
    <div class="pl-arrow" aria-hidden="true">→</div>
    <button type="button" class="pl-station" id="plClaimed" data-status="in_progress"
      title="open Board · claimed (in progress)">
      <div class="pl-obj" aria-hidden="true">
        <svg viewBox="0 0 48 36" fill="none" stroke="var(--ink)" stroke-width="1.2">
          <rect x="8" y="8" width="22" height="20" fill="#fbf6ea" transform="rotate(-8 19 18)"/>
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
      title="open Board · sign-off due (in review)">
      <div class="pl-obj" aria-hidden="true">
        <svg viewBox="0 0 48 36" fill="none" stroke="var(--ink)" stroke-width="1.2">
          <!-- spike -->
          <line x1="24" y1="4" x2="24" y2="32" stroke="var(--ink)" stroke-width="1.6"/>
          <polygon points="24,2 26.5,7 21.5,7" fill="var(--ink)"/>
          <rect x="14" y="10" width="20" height="5" fill="#fbf6ea" transform="rotate(-12 24 12.5)"/>
          <rect x="14" y="16" width="20" height="5" fill="#fbf6ea" transform="rotate(8 24 18.5)"/>
          <rect x="15" y="22" width="18" height="4" fill="#fbf6ea" transform="rotate(-4 24 24)"/>
        </svg>
      </div>
      <div class="pl-label">Sign-off due</div>
      <div class="pl-count" id="plSignoffN">0</div>
    </button>
    <div class="pl-arrow" aria-hidden="true">→</div>
    <button type="button" class="pl-station" id="plSigned" data-status="done"
      title="open Board · signed (done)">
      <div class="pl-obj" aria-hidden="true">
        <svg viewBox="0 0 48 36" fill="none" stroke="var(--ink)" stroke-width="1.2">
          <rect x="6" y="10" width="20" height="16" fill="#fbf6ea"/>
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
<main class="surface">
  <div>
    <div class="tray"><div class="tray-head">
      <h2>In-tray · needs you (<span id="inCount">0</span>)</h2>
      <select id="trayFilter" title="Narrow both trays to one store"></select></div>
      <div id="decisionsStack"><div class="empty-note">Waiting for the morning filing…</div></div>
    </div>
    <div class="tray"><h2>Hold bin · quiet claims (<span id="holdCount">0</span>)</h2>
      <div id="staleStack"><div class="empty-note">Waiting for the morning filing…</div></div>
    </div>
  </div>
  <div>
    <div class="blotter"><h2>Neighborhood ledgers · open work</h2>
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
  <div>The desk owns the ticket verbs — <a href="/admin/tickets/all">Board</a>
    <a href="/admin/overview">Overview</a>
    <a href="/admin/settings">Settings</a> <a href="/admin/docs/desk">How to read this room</a></div>
  <div>{doors}</div>
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

_REPORT_WINDOW_DAYS = int(os.environ.get("WL_REPORT_WINDOW_DAYS", "7"))
_REPORT_AGING_DAYS = int(os.environ.get("WL_REPORT_AGING_DAYS", "7"))
_REPORT_PRUNE_QUIET_HOURS = int(os.environ.get("WL_REPORT_PRUNE_QUIET_HOURS", "72"))


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
    waiting_on_you = len(_collect_founder_attention_items(now=now))
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
:root {
  --desk:#e9e7e2; --paper:#fdfdfb; --line:#d7d5cf; --rule:#9fb6d9;
  --ink:#1f2328; --dim:#6d7480; --blue:#1c4f9c; --stamp:#c0392b;
  --ok:#1e7a45; --warn:#a8681e; --pink:#fbe9ea; --pinkline:#e2b6ba;
}
* { box-sizing:border-box; margin:0; }
html,body { height:100%; }
body { background:var(--desk); color:var(--ink);
  font:14px/1.5 "IBM Plex Sans",-apple-system,"Helvetica Neue",Helvetica,Arial,sans-serif;
  background-image:linear-gradient(180deg,#dedcd6 0,var(--desk) 220px);
  display:flex; flex-direction:column; }
a { color:var(--blue); text-decoration:none; } a:hover { text-decoration:underline; }
.dim { color:var(--dim); } .ok { color:var(--ok); } .warn { color:var(--warn); }
.mono { font-family:"IBM Plex Mono",ui-monospace,monospace; }
header.nameplate { background:var(--paper); border-bottom:1px solid var(--line);
  border-top:6px solid var(--stamp); box-shadow:0 2px 8px #0002;
  padding:14px 22px 12px; display:flex; align-items:baseline; gap:14px; flex-wrap:wrap; }
h1 { font-size:19px; letter-spacing:.14em; font-weight:700; }
h1 .fn { color:var(--stamp); }
.epithet { color:var(--dim); font-size:13px; }
.badges { margin-left:auto; display:flex; gap:12px; align-items:baseline; font-size:12px;
  color:var(--dim); }
#liveChip { border:1.5px solid var(--ok); color:var(--ok); border-radius:3px;
  font-weight:700; font-size:10px; letter-spacing:.16em; padding:2px 8px;
  text-transform:uppercase; }
#liveChip.hold { border-color:var(--warn); color:var(--warn); }
.room-back { font-size:12px; color:var(--blue); border:1px solid var(--line);
  border-radius:3px; padding:3px 10px; white-space:nowrap; background:var(--paper); }
.room-back:hover { border-color:var(--blue); text-decoration:none; }
main.sheet { flex:1; overflow:auto; padding:20px 22px 26px; max-width:1180px;
  width:100%; margin:0 auto; }
h2 { font-size:11px; letter-spacing:.24em; color:var(--dim); text-transform:uppercase;
  margin:22px 0 10px; font-weight:600; }
h2:first-child { margin-top:0; }
.card { background:var(--paper); border:1px solid var(--line);
  box-shadow:0 2px 6px #0002; padding:14px 16px; }
.verdicts { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
  gap:12px; }
.verdict { background:var(--paper); border:1px solid var(--line);
  box-shadow:0 2px 5px #0002; padding:10px 14px; position:relative; overflow:hidden; }
.verdict::before { content:""; position:absolute; left:0; top:0; bottom:0; width:4px;
  background:var(--rule); }
.verdict.aging::before { background:var(--stamp); }
.verdict.growing::before { background:var(--warn); }
.verdict .nm { font-weight:700; font-size:13px; }
.verdict .nm a { color:var(--ink); } .verdict .nm a:hover { color:var(--blue); }
.verdict .vw { font-size:16px; font-weight:700; margin-top:2px; }
.verdict.aging .vw { color:var(--stamp); } .verdict.growing .vw { color:var(--warn); }
.verdict.keeping .vw, .verdict.steady .vw { color:var(--ok); }
.verdict .meta { font-size:11.5px; color:var(--dim); margin-top:2px; }
.rows { display:grid; grid-template-columns:130px minmax(0,1fr) 90px;
  gap:6px 12px; align-items:center; font-size:13px; }
.rows .lbl { color:var(--dim); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.rows .num { color:var(--dim); text-align:right; font-variant-numeric:tabular-nums; }
/* chart bars are .meter, never .bar — footer.bar wears that class (wl-163) */
.meter { display:block; height:9px; border-radius:2px; background:var(--rule); }
.meter.g { background:#bfd8c4; }
.meter + .meter { margin-top:2px; }
.split { display:flex; height:22px; border-radius:3px; overflow:hidden;
  border:1px solid var(--line); }
.split .you { background:var(--stamp); } .split .rdy { background:#bfd8c4; }
.split .oth { background:var(--rule); }
.legend { display:flex; gap:18px; font-size:12.5px; margin-top:8px; flex-wrap:wrap; }
.li { display:flex; justify-content:space-between; gap:10px; padding:6px 2px;
  border-bottom:1px dotted var(--line); font-size:13px; }
.li:last-child { border-bottom:0; }
.li .age { color:var(--warn); white-space:nowrap; font-variant-numeric:tabular-nums; }
.tag { border:1px solid var(--line); border-radius:3px; padding:0 6px; font-size:11px;
  color:var(--dim); background:#fff; }
.note { font-size:12px; color:var(--dim); margin-top:8px; }
.stamp-count { display:inline-block; border:2.5px solid var(--stamp); color:var(--stamp);
  border-radius:4px; font-weight:800; padding:6px 12px; transform:rotate(-3deg);
  font-size:15px; letter-spacing:.06em;
  mask-image:radial-gradient(circle at 35% 45%, #000 90%, #0007 100%); }
.grid2 { display:grid; grid-template-columns:1fr 1fr; gap:18px; align-items:start; }
@media (max-width:900px){ .grid2 { grid-template-columns:1fr; } }
footer.bar { border-top:1px solid var(--line); background:var(--paper);
  padding:8px 22px; display:flex; justify-content:space-between; gap:14px;
  flex-wrap:wrap; font-size:12px; color:var(--dim); }
footer.bar a { margin-right:14px; }
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
function render(){
  var d=R; if(!d)return;
  var vz="";
  (d.stores||[]).forEach(function(s){
    var cls=s.verdict==="keeping up"?"keeping":esc(s.verdict);
    var extra=s.verdict==="aging"
      ? esc(s.over_aging)+" orders past "+esc(d.aging_days)+"d"
      : "filed "+esc(s.filed)+" \\u00b7 signed "+esc(s.signed);
    vz+='<div class="verdict '+cls+'"><div class="nm"><a href="/admin/tickets/'+
      esc(s.slug)+'">'+esc(s.display||s.slug)+'</a></div>'+
      '<div class="vw">'+esc(s.verdict)+'</div>'+
      '<div class="meta">'+extra+' \\u00b7 net '+(s.net>=0?"+":"")+esc(s.net)+'</div></div>';});
  $("verdictStrip").innerHTML=vz||'<div class="note">no ledgers with activity</div>';

  var maxF=1; (d.stores||[]).forEach(function(s){maxF=Math.max(maxF,s.filed,s.signed);});
  var fz="";
  (d.stores||[]).forEach(function(s){
    fz+='<span class="lbl">'+esc(s.display||s.slug)+'</span>'+
      '<span><span class="meter" style="width:'+pct(s.filed,maxF)+'%"></span>'+
      '<span class="meter g" style="width:'+pct(s.signed,maxF)+'%"></span></span>'+
      '<span class="num">'+esc(s.filed)+' / '+esc(s.signed)+'</span>';});
  $("flowRows").innerHTML=fz;

  var b=d.blocker||{}, tot=Math.max(1,d.open_total||0);
  $("splitBar").innerHTML=
    '<div class="you" style="width:'+pct(b.waiting_on_you,tot)+'%"></div>'+
    '<div class="rdy" style="width:'+pct(b.worker_ready,tot)+'%"></div>'+
    '<div class="oth" style="flex:1"></div>';
  $("splitLegend").innerHTML=
    '<span><b style="color:var(--stamp)">'+esc(b.waiting_on_you)+' waiting on you</b></span>'+
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
    return '<a href="/admin/tasks/'+esc(e.id)+'" class="mono">'+esc(e.id)+'</a> ('+
      esc(Math.round(e.age_days))+'d)';}).join(" \\u00b7 ");
  $("agingNote").innerHTML=oz?("oldest on the rack: "+oz):"the rack is fresh";

  var uz="";
  (d.urgent_unclaimed||[]).forEach(function(e){
    uz+='<div class="li"><span><a href="/admin/tasks/'+esc(e.id)+'" class="mono">'+
      esc(e.id)+'</a> <span class="tag">'+esc(e.store)+'</span> '+
      esc(String(e.title).slice(0,64))+'</span>'+
      '<span class="age">P'+esc(e.priority)+' \\u00b7 '+esc(Math.round(e.age_days))+'d</span></div>';});
  $("urgentList").innerHTML=uz||'<div class="note">nothing urgent sits unclaimed \\u2014 priority holds</div>';

  var p=d.prune||{count:0,items:[]};
  $("pruneStamp").textContent=p.count+" TO PRUNE";
  var pz=(p.items||[]).slice(0,5).map(function(e){
    return '<a href="/admin/tasks/'+esc(e.id)+'" class="mono">'+esc(e.id)+'</a> ('+
      esc(Math.round(e.quiet_days))+'d quiet)';}).join(" \\u00b7 ");
  $("pruneNote").innerHTML=p.count
    ? "quiet past "+esc(Math.round(d.prune_quiet_hours/24))+"d: "+pz+
      ' \\u2014 cancel, demote, or re-label for a lane'
    : "nothing is being carried silently";}
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


def _render_report_page() -> str:
    """The desk's report in the paper voice (wl-156 ruling 4): a bench of the
    Ticket Desk, so the desk palette, one sheet, no scene furniture."""
    if _BRAND_MODE == "city":
        h1 = "<span class='fn'>THE REPORT</span> · OVERVIEW"
        epithet = "ProtocolCity · the desk's strategic view"
    else:
        h1 = "WORKLANE — <span class='fn'>THE REPORT</span>"
        epithet = "the strategic view"
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Overview · {_esc(_BRAND_NAME)}</title>
<style>{_REPORT_CSS}</style></head><body>
<header class="nameplate">
  <a class="room-back" href="/admin/desk">← Ticket Desk</a>
  <h1>{h1}</h1>
  <span class="epithet">{_esc(epithet)}</span>
  <div class="badges"><span id="clock">—:—:—</span><span id="liveChip" class="hold">NO SIGNAL</span></div>
</header>
<main class="sheet">
  <h2>Verdicts · one word per ledger</h2>
  <div class="verdicts" id="verdictStrip"><div class="note">pulling the morning figures…</div></div>
  <div class="grid2" style="margin-top:6px">
    <div>
      <h2>Flow · filed vs signed off, last <span id="w1">7</span> days</h2>
      <div class="card"><div class="rows" id="flowRows"></div>
        <div class="note">blue = filed in · green = signed off · the gap is backlog growth</div></div>
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
  <div>A bench of the Ticket Desk — <a href="/admin/desk">back to the room</a>
    <a href="/admin/tickets/all">Board</a> <a href="/admin/settings">Settings</a>
    <a href="/admin/docs/desk">How to read this room</a></div>
  <div><span class="dim">records request: <a href="/api/report">/api/report</a></span></div>
</footer>
{_REPORT_JS}</body></html>"""


# ── App factory ────────────────────────────────────────────────────────

def create_app():
    """Build the standalone task-board FastAPI app."""
    from fastapi import FastAPI
    from fastapi.staticfiles import StaticFiles

    app = FastAPI(
        title="WorkLane",
        description="WorkLane — standalone local-first ticketing service.",
        docs_url="/api/docs",
        redoc_url=None,
    )
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
