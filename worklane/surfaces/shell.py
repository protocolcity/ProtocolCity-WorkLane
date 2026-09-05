"""D1 page shell extracted from task_server (code-efficiency first cut).

HTML chrome for the Ticketing benches: scope nav, context strip, and the
``_task_page`` wrapper. Routes stay on ``task_server``; this module is
render-only. Brand tokens come from :mod:`worklane.surfaces.chrome`
(wl-222) rather than a second copy.

Imported symbols are re-exported from ``worklane.task_server`` so existing
tests and callers keep working.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

from worklane.board import (
    TICKETS_APP_ALL,
    _wq_query_for_view,
    tickets_app_path,
)
from worklane.products import (
    default_product_slug,
    discover_products,
    get_product,
)
from worklane.rendering import _css, _esc
from worklane.surfaces.chrome import (
    _BRAND_HEADER_HTML,
    _BRAND_MODE,
    _BRAND_NAME,
    _BRAND_SUBTITLE,
)

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
# Brand tokens: worklane.surfaces.chrome (wl-222).

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


