"""Board rendering helpers for WorkLane (extracted from host, ADR-025 Phase 1b).

This module contains the rendering helpers, query utilities, and constants
that task_server.py needs to render the WL board UI.  All symbols here are
WL-internal — no core.* imports.

Previously these lived in core/web/routes/admin_tasks.py and were imported
from there (ticket #407).  admin_tasks.py now re-exports them from here so
that tradeOS-internal callers (ops_tickets_feed.py) keep working unchanged.
"""

from __future__ import annotations

import os
import re
import urllib.parse
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from worklane.products import (
    ProductSpec,
    discover_products,
    get_product,
    prefixed_task_id,
    product_slugs,
    product_trackers,
    split_task_id,
)
from worklane.rendering import _badge, _esc, _label_chip
from worklane.trackers import (
    Task,
    TaskComment,
    TaskStatus,
    get_default_tracker,
    task_is_gated,
)


# ── Canonical Tickets app paths ───────────────────────────────────────────
TICKETS_APP_ALL = "/admin/tickets/all"
TICKETS_APP_TRADEOS = "/admin/tickets/tradeos"
TICKETS_APP_OPS = "/admin/tickets/ops"

_WORK_QUEUE_PATH = "/admin/work-queue"


# ── vocab → labels ────────────────────────────────────────────────────────
_STATUS_LABELS = {
    TaskStatus.BACKLOG:      "Backlog",
    TaskStatus.IN_PROGRESS:  "In Progress",
    TaskStatus.IN_REVIEW:    "In Review",
    TaskStatus.DONE:         "Done",
    TaskStatus.CANCELED:     "Canceled",
}

_STATUS_TIERS = {
    TaskStatus.BACKLOG:      "neutral",
    TaskStatus.IN_PROGRESS:  "info",
    TaskStatus.IN_REVIEW:    "warning",
    TaskStatus.DONE:         "positive",
    TaskStatus.CANCELED:     "neutral",
}

_PRIORITY_LABELS: Dict[int, str] = {1: "Urgent", 2: "High", 3: "Normal", 4: "Low", 0: "—"}
_PRIORITY_TIERS: Dict[int, str] = {1: "critical", 2: "warning", 3: "neutral", 4: "neutral", 0: "neutral"}

# wl-10: named facets get their own chip row (top-N + "more"); everything
# else (parent:, epic:, size:, needs:, one-off composites) falls into the
# "other" bucket, collapsed by default behind its own toggle.
_CHIP_FACET_PREFIXES = ("area:", "sys:", "product:", "lane:", "type:")
_CHIP_TOP_N = 6

# Ticket work area — orthogonal to Dev vs Work queue chrome.
PRODUCT_LABEL_TRADEOS = "product:tradeos"
PRODUCT_LABEL_OPS = "product:ops"

# Composite task ids in merged views (``t-`` = tradeOS repo DB, ``o-`` = Ops Cockpit DB).
TASK_ID_PREFIX_TRADEOS = "t"
TASK_ID_PREFIX_OPS = "o"

# Kanban columns — Canceled is omitted from the board.
_BOARD_COLUMNS: List[str] = [
    TaskStatus.BACKLOG,
    TaskStatus.IN_REVIEW,
    TaskStatus.IN_PROGRESS,
    TaskStatus.DONE,
]

# Byline icon for any worker identity. Identities come from the store's
# signed comments and render verbatim — no baked-in agent roster (wl-84):
# which agents exist is the host deployment's business, not the product's.
WORKER_BYLINE_ICON = "·"


# ── Badge / label helpers ─────────────────────────────────────────────────

def _label_tier(label: str) -> str:
    s = label.lower()
    if s.startswith("product:"):
        return "positive"
    if s.startswith("area:"):
        return "info"
    if s.startswith("sys:"):
        return "warning"
    if s in ("bug",):
        return "critical"
    if s in ("feature",):
        return "positive"
    return "neutral"


def _render_labels(labels: List[str]) -> str:
    if not labels:
        return "<span class='dim'>—</span>"
    return " ".join(_label_chip(l, _label_tier(l)) for l in labels)


def _render_status_badge(status: str) -> str:
    return _badge(_STATUS_LABELS.get(status, status), _STATUS_TIERS.get(status, "neutral"))


def _render_priority_badge(priority: int) -> str:
    p = int(priority or 0)
    return _badge(_PRIORITY_LABELS.get(p, "—"), _PRIORITY_TIERS.get(p, "neutral"))


# ── Product / priority parsing ────────────────────────────────────────────

def _embed_product_query_param(list_path: str) -> bool:
    return not (list_path or "").startswith("/admin/tickets/")


def wq_product_sql_label(product: str) -> Optional[str]:
    if product == "tradeos":
        return PRODUCT_LABEL_TRADEOS
    if product == "ops":
        return PRODUCT_LABEL_OPS
    return None


def parse_wq_priority(raw: Optional[str]) -> Optional[int]:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    try:
        v = int(s)
    except ValueError:
        return None
    return v if v in (1, 2, 3, 4) else None


def parse_wq_product(raw: Optional[str]) -> str:
    if raw is None:
        return ""
    s = str(raw).strip().lower()
    if s in ("", "all"):
        return ""
    if s == "trade_os":
        return "tradeos"
    if s in ("ops", "op"):
        return "ops"
    if s in product_slugs():
        return s
    return ""


def product_scope_from_list_path(list_path: str) -> str:
    p = (list_path or "").rstrip("/")
    if p.endswith("/ops"):
        return "ops"
    tail = p.rsplit("/", 1)[-1]
    if tail in product_slugs():
        return tail
    return ""


def tickets_app_path(slug: str) -> str:
    """Canonical Pool path for a product surface (``all`` for no scope)."""
    s = (slug or "").strip().lower()
    if s and s in product_slugs():
        return f"/admin/tickets/{s}"
    return TICKETS_APP_ALL


# ── Ops tracker ───────────────────────────────────────────────────────────

def ops_tickets_db_path() -> Path:
    """SQLite file for Ops-scoped tickets.

    Local-first default keeps ticketing runtime under the protocol root:
    ``worklane/local/data/ops_tickets.db``.
    Override with ``OPS_TICKETS_DB`` when needed.
    """
    override = (os.environ.get("OPS_TICKETS_DB") or "").strip()
    if override:
        return Path(override)
    wl_root = Path(__file__).parent
    default = wl_root / "local" / "data" / "ops_tickets.db"
    legacy_hidden = wl_root / ".local" / "data" / "ops_tickets.db"
    legacy_root = wl_root.parent / "local" / "data" / "ops_tickets.db"
    if default.exists():
        return default
    if legacy_hidden.exists():
        return legacy_hidden
    if legacy_root.exists():
        return legacy_root
    return default


def get_ops_ticket_tracker() -> Any:
    from worklane.trackers.sqlite import PRODUCT_LABEL_OPS, SQLiteTracker

    return SQLiteTracker(
        db_path=ops_tickets_db_path(),
        product_default=PRODUCT_LABEL_OPS,
    )


def parse_surface_task_id(task_id: str) -> Tuple[str, str]:
    """Composite id → (product slug, raw store id). Registry-driven."""
    return split_task_id(task_id)


# ── Task list queries ─────────────────────────────────────────────────────

def list_tasks_for_product_scope(
    tracker: Any,
    product: str,
    *,
    limit: Optional[int] = None,
) -> List[Task]:
    plab = wq_product_sql_label(product)
    if plab:
        return tracker.list_tasks(label=plab, limit=limit)
    return tracker.list_tasks(limit=limit)


# ── Multi-product queries (registry-driven; supersede the dual pair) ──────

def list_tasks_for_wq_multi(
    products: List[Tuple[ProductSpec, Any]],
    *,
    status: Optional[str],
    label: Optional[str],
    priority: Optional[int],
    product: str,
    limit: int = 500,
) -> List[Task]:
    """Tasks across product stores; ``product`` scopes to one slug, "" = all.

    In the one-DB-per-product model a product scope selects a store — no
    ``product:*`` label filtering is involved.
    """
    p = (product or "").strip().lower()
    merged: List[Task] = []
    for spec, tracker in products:
        if p and spec.slug != p:
            continue
        tasks = tracker.list_tasks(
            status=status, label=(label or "").strip() or None,
            priority=priority, limit=limit,
        )
        merged.extend(
            replace(t, id=f"{spec.prefix}-{t.id}") for t in tasks
        )
    merged.sort(key=lambda x: x.updated_at or "", reverse=True)
    return merged[:limit]


def list_tasks_for_scope_multi(
    products: List[Tuple[ProductSpec, Any]],
    product: str,
    *,
    limit: Optional[int] = None,
) -> List[Task]:
    p = (product or "").strip().lower()
    merged: List[Task] = []
    for spec, tracker in products:
        if p and spec.slug != p:
            continue
        tasks = tracker.list_tasks(limit=limit)
        merged.extend(replace(t, id=f"{spec.prefix}-{t.id}") for t in tasks)
    merged.sort(key=lambda x: x.updated_at or "", reverse=True)
    if limit is not None:
        merged = merged[:limit]
    return merged


# Ownership marker line per PROTOCOL.md §3 — `Owner: <agent-id> (<model>)`.
# The parenthetical and anything after it is presentation, not identity.
_OWNER_LINE_RE = re.compile(r"^Owner:\s*([^\n(]+)", re.MULTILINE)


def _extract_owner(comments: List[Any]) -> str:
    for c in sorted(comments, key=lambda c: c.created_at or "", reverse=True):
        m = _OWNER_LINE_RE.search(c.body or "")
        if m:
            return m.group(1).strip()
    return ""


def _load_preview_comments_multi(
    products: List[Tuple[ProductSpec, Any]],
    tasks: List[Task],
    *,
    tradeos_preview: Optional[Dict[str, Dict[str, str]]] = None,
) -> Dict[str, Dict[str, str]]:
    by_slug: Dict[str, Any] = {spec.slug: tr for spec, tr in products}
    preview: Dict[str, Dict[str, str]] = {}
    for t in tasks:
        cid = str(t.id)
        slug, raw = split_task_id(cid)
        if slug == "tradeos" and tradeos_preview:
            entry = tradeos_preview.get(cid)
            if entry:
                preview[cid] = entry
                continue
        tr = by_slug.get(slug)
        if tr is None or not hasattr(tr, "list_comments"):
            continue
        try:
            comments = tr.list_comments(raw)
        except Exception:
            continue
        if not comments:
            continue
        latest = max(comments, key=lambda c: c.created_at or "")
        body = (latest.body or "").strip().replace("\r", "")
        first_line = next((ln.strip() for ln in body.split("\n") if ln.strip()), "")
        if len(first_line) > 180:
            first_line = first_line[:177].rstrip() + "…"
        preview[cid] = {
            "body": first_line,
            "author": latest.author or "",
            "created_at": latest.created_at or "",
            "owner": _extract_owner(comments),
        }
    return preview


# ── Work-queue query helpers ──────────────────────────────────────────────

def _wq_query_for_view(
    view: str,
    status: str,
    label: str,
    priority: Optional[int],
    *,
    product: str = "",
    list_path: str = "",
) -> str:
    parts: List[Tuple[str, str]] = [("view", view)]
    if status:
        parts.append(("status", status))
    if label:
        parts.append(("label", label))
    if priority is not None:
        parts.append(("priority", str(priority)))
    prod = parse_wq_product(product)
    if prod and _embed_product_query_param(list_path):
        parts.append(("product", prod))
    return urllib.parse.urlencode(parts)


def _wq_status_counts(tasks: List[Task]) -> Dict[str, int]:
    counts: Dict[str, int] = {s: 0 for s in TaskStatus.ALL}
    for t in tasks:
        if t.status in counts:
            counts[t.status] += 1
    return counts


def _wq_column_counts(
    scope_tasks: List[Task],
    *,
    status: Optional[str] = None,
    label: Optional[str] = None,
    priority: Optional[int] = None,
) -> Dict[str, int]:
    """Per-status counts of the *filtered* scope — what each board column
    would hold if the page fetch were uncapped. Must mirror the
    list_tasks_for_wq filter semantics (exact label membership, priority
    equality) or headers drift from the cards under active filters."""
    counts: Dict[str, int] = {s: 0 for s in TaskStatus.ALL}
    st = (status or "").strip()
    lb = (label or "").strip()
    for t in scope_tasks:
        if st and t.status != st:
            continue
        if lb and lb not in (t.labels or []):
            continue
        if priority is not None and t.priority != priority:
            continue
        if t.status in counts:
            counts[t.status] += 1
    return counts


def _render_wq_quick_buckets(
    *,
    list_path: str,
    current_view: str,
    current_status: str,
    label: str,
    priority: Optional[int],
    product: str = "",
    counts: Dict[str, int],
) -> str:
    st = (current_status or "").strip()
    total = sum(counts.get(s, 0) for s in TaskStatus.ALL)

    def _href(status_key: str) -> str:
        return (
            f"{list_path}?"
            f"{_wq_query_for_view(current_view, status_key, label, priority, product=product, list_path=list_path)}"
        )

    specs: List[tuple] = [
        ("", "wq-bucket--all", "All", total),
        (TaskStatus.BACKLOG, "wq-bucket--backlog", _STATUS_LABELS[TaskStatus.BACKLOG], counts.get(TaskStatus.BACKLOG, 0)),
        (TaskStatus.IN_PROGRESS, "wq-bucket--progress", _STATUS_LABELS[TaskStatus.IN_PROGRESS], counts.get(TaskStatus.IN_PROGRESS, 0)),
        (TaskStatus.IN_REVIEW, "wq-bucket--review", _STATUS_LABELS[TaskStatus.IN_REVIEW], counts.get(TaskStatus.IN_REVIEW, 0)),
        (TaskStatus.DONE, "wq-bucket--done", _STATUS_LABELS[TaskStatus.DONE], counts.get(TaskStatus.DONE, 0)),
    ]
    parts: List[str] = []
    for key, css, lbl, cnt in specs:
        is_active = (not key and not st) or (bool(key) and st == key)
        active_cls = " wq-bucket--active" if is_active else ""
        bucket_key = key if key else "__all__"
        parts.append(
            f"<a href='{_esc(_href(key))}' class='wq-bucket {css}{active_cls}' "
            f"data-wq-bucket='{_esc(bucket_key)}' "
            f"title='Show {_esc(lbl)} tasks'>"
            f"<span class='wq-bucket-val' data-wq-bucket-count='{_esc(bucket_key)}'>{cnt}</span>"
            f"<span class='wq-bucket-lbl'>{_esc(lbl)}</span>"
            f"</a>"
        )
    cnc_n = counts.get(TaskStatus.CANCELED, 0)
    is_c = st == TaskStatus.CANCELED
    c_cls = " wq-bucket--active" if is_c else ""
    parts.append(
        f"<a href='{_esc(_href(TaskStatus.CANCELED))}' "
        f"class='wq-bucket wq-bucket--canceled{c_cls}' data-wq-bucket='{_esc(TaskStatus.CANCELED)}' "
        f"title='Canceled'>"
        f"<span class='wq-bucket-val' data-wq-bucket-count='{_esc(TaskStatus.CANCELED)}'>{cnc_n}</span>"
        f"<span class='wq-bucket-lbl'>{_esc(_STATUS_LABELS[TaskStatus.CANCELED])}</span>"
        f"</a>"
    )
    return (
        "<div class='wq-buckets' role='tablist' aria-label='Quick filters by status'>"
        + "".join(parts)
        + "</div>"
    )


def _render_work_queue_filters(
    *,
    list_path: str,
    current_view: str,
    status: str,
    label: str,
    priority: Optional[int],
    product: str = "",
    merged_scope_tasks: Optional[List[Task]] = None,
    view_toggle_html: str = "",
) -> str:
    prod = parse_wq_product(product)
    if merged_scope_tasks is not None:
        all_tasks = merged_scope_tasks
    else:
        tracker = get_default_tracker()
        all_tasks = list_tasks_for_product_scope(tracker, prod, limit=None)
    counts = _wq_status_counts(all_tasks)
    label_set: Dict[str, int] = {}
    for t in all_tasks:
        for lb in (t.labels or []):
            label_set[lb] = label_set.get(lb, 0) + 1

    facet_labels: Dict[str, List[str]] = {p: [] for p in _CHIP_FACET_PREFIXES}
    other_labels: List[str] = []
    for lb in label_set:
        low = lb.lower()
        facet = next((p for p in _CHIP_FACET_PREFIXES if low.startswith(p)), None)
        if facet:
            facet_labels[facet].append(lb)
        else:
            other_labels.append(lb)

    priority_options = ""
    for val, lab in (
        ("", "All priorities"),
        ("1", "Urgent"),
        ("2", "High"),
        ("3", "Normal"),
        ("4", "Low"),
    ):
        sel = priority is None if val == "" else priority == int(val)
        priority_options += (
            f"<option value='{_esc(val)}'{' selected' if sel else ''}>"
            f"{_esc(lab)}</option>"
        )

    def _chip_href(lb: str) -> str:
        q = _wq_query_for_view(
            current_view, status, lb, priority, product=product, list_path=list_path
        )
        return f"{list_path}?{q}"

    def _sort_by_count(keys: List[str]) -> List[str]:
        return sorted(keys, key=lambda k: (-label_set[k], k.lower()))

    def _chip_html(lb: str, *, overflow: bool) -> str:
        active_cls = " active" if lb == label else ""
        overflow_cls = " wq-chip--overflow" if overflow else ""
        return (
            f"<a href='{_esc(_chip_href(lb))}' "
            f"class='notif-filter-chip{active_cls}{overflow_cls}' "
            f"title='Filter by {_esc(lb)}'>"
            f"{_esc(lb)}<span class='chip-count'>{label_set[lb]}</span></a>"
        )

    def _chip_group(keys: List[str], heading: str, *, other: bool = False) -> str:
        if not keys:
            return ""
        ordered = _sort_by_count(keys)
        top = [] if other else ordered[:_CHIP_TOP_N]
        overflow = ordered if other else ordered[_CHIP_TOP_N:]
        active_in_overflow = bool(label) and label in overflow
        default_collapsed = "0" if active_in_overflow else "1"
        chips = "".join(_chip_html(lb, overflow=False) for lb in top)
        chips += "".join(_chip_html(lb, overflow=True) for lb in overflow)
        more_n = len(overflow)
        more_btn = (
            f"<button type='button' class='wq-chip-more' "
            f"onclick='adminBoardToggleChipGroup(this)'>"
            f"+{more_n} more&hellip;</button>"
            if more_n
            else ""
        )
        group_cls = "wq-chip-group" + (" wq-chip-group--other" if other else "")
        return (
            f"<div class='{group_cls}' data-collapsed='{default_collapsed}' "
            f"data-default-collapsed='{default_collapsed}'>"
            f"<span class='wq-chip-group-label'>{_esc(heading)}</span>"
            f"<div class='wq-chip-row'>{chips}{more_btn}</div>"
            "</div>"
        )

    chip_sections = "".join(
        _chip_group(facet_labels[p], p.rstrip(":")) for p in _CHIP_FACET_PREFIXES
    ) + _chip_group(other_labels, "other", other=True)
    chip_search = (
        "<div class='wq-chip-search'>"
        "<input type='text' class='ts-filter-input wq-chip-search-input' "
        "placeholder='Search labels…' autocomplete='off' "
        "oninput='adminBoardFilterChipSearch(this.value)'/>"
        "</div>"
        if label_set
        else ""
    )

    reset_q = _wq_query_for_view(
        current_view, "", "", None, product="", list_path=list_path
    )
    has_filters = bool(
        (status or "").strip()
        or label
        or priority is not None
        or (bool(prod) and _embed_product_query_param(list_path))
    )
    reset_html = (
        f"<a href='{_esc(list_path)}?{_esc(reset_q)}' class='btn btn-secondary'>Reset all</a>"
        if has_filters
        else ""
    )

    st_hidden = f"<input type='hidden' name='status' value='{_esc(status)}'/>" if (status or "").strip() else ""

    advanced_open = bool((label or "").strip() or priority is not None)

    buckets = _render_wq_quick_buckets(
        list_path=list_path,
        current_view=current_view,
        current_status=status,
        label=label,
        priority=priority,
        product=product,
        counts=counts,
    )

    prod_hidden = (
        f"<input type='hidden' name='product' value='{_esc(prod)}'/>"
        if (prod and _embed_product_query_param(list_path))
        else ""
    )

    advanced_body = (
        f"<form method='get' action='{_esc(list_path)}' class='wq-advanced-form ts-filter-form'>"
        f"<input type='hidden' name='view' value='{_esc(current_view)}'/>"
        f"{prod_hidden}"
        f"{st_hidden}"
        "<div class='ts-filter-field'>"
        "<label class='dim' for='wq-label'>Label</label>"
        f"<input id='wq-label' name='label' value='{_esc(label)}' "
        "placeholder='area:assets, epic:…' class='ts-filter-input' "
        "autocomplete='off'/>"
        "</div>"
        "<div class='ts-filter-field'>"
        "<label class='dim' for='wq-prio'>Priority</label>"
        f"<select id='wq-prio' name='priority' class='ts-filter-select'>{priority_options}</select>"
        "</div>"
        "<div class='ts-filter-field ts-filter-actions'>"
        "<label class='dim'>&nbsp;</label>"
        "<span class='wq-filter-actions'>"
        "<button type='submit' class='btn btn-secondary'>Apply</button>"
        f"{reset_html}"
        "</span>"
        "</div>"
        "</form>"
        + (
            f"<div class='wq-filter-chips'>{chip_search}{chip_sections}</div>"
            if chip_sections
            else ""
        )
        + "<p class='wq-advanced-hint dim'>Tip: click a bucket above for status scope; use this section to narrow by label or priority.</p>"
    )

    # Jump-to-ticket search: single input, client-side navigates to /admin/tasks/<id>.
    # Lives inline in the command bar — ticket-number lookup is the most
    # frequent ops action and shouldn't hide behind a disclosure.
    #
    # Composite ids (wl-503) always navigate directly. A bare number resolves
    # within the current product's store when scoped (data-scope-prefix);
    # in the All view (no scope) it's ambiguous across stores, so JS asks
    # /api/admin/tasks/resolve and shows a disambiguation popover (wl-76).
    scoped_prefix = ""
    if prod:
        scoped_spec = get_product(prod)
        if scoped_spec:
            scoped_prefix = scoped_spec.prefix
    jump_placeholder = "503" if scoped_prefix else "t-503 / wl-503"
    jump_form = (
        "<form class='wq-jump-form' "
        f"data-scope-prefix='{_esc(scoped_prefix)}' "
        # adminBoardJumpSubmit is async; its return value is a Promise
        # (truthy), so it can't cancel the native submit itself — cancel
        # unconditionally and let the handler drive navigation (wl-82).
        "onsubmit=\"adminBoardJumpSubmit(this); return false;\">"
        "<label class='dim' for='wq-jump-id'>Jump&nbsp;#</label>"
        "<input id='wq-jump-id' name='id' type='text' inputmode='text' "
        f"placeholder='{_esc(jump_placeholder)}' autocomplete='off' "
        "class='ts-filter-input wq-jump-input'/>"
        "<div id='wq-jump-ambiguous' class='wq-jump-ambiguous' hidden></div>"
        "</form>"
    )

    # One command bar (wl-36): counts + jump + filters toggle + view toggle in
    # a single ~36px row; the advanced panel opens below the bar.
    toggle_open_cls = " wq-adv-toggle--open" if advanced_open else ""
    panel_hidden = "" if advanced_open else " hidden"
    adv_toggle = (
        f"<button type='button' class='wq-adv-toggle{toggle_open_cls}' "
        f"aria-expanded='{'true' if advanced_open else 'false'}' "
        "aria-controls='wq-advanced-panel' "
        "onclick=\"var p=document.getElementById('wq-advanced-panel');"
        "p.hidden=!p.hidden;this.classList.toggle('wq-adv-toggle--open',!p.hidden);"
        "this.setAttribute('aria-expanded',p.hidden?'false':'true');\">"
        "Filters <span class='wq-adv-caret'>&#9662;</span></button>"
    )

    return (
        "<div class='wq-filter wq-cmdbar'>"
        "<div class='wq-cmdbar-row'>"
        + buckets
        + "<span class='wq-cmdbar-spacer'></span>"
        + jump_form
        + adv_toggle
        + view_toggle_html
        + "</div>"
        + f"<div id='wq-advanced-panel' class='wq-advanced-panel'{panel_hidden}>{advanced_body}</div>"
        + "</div>"
    )


# ── Board rendering ───────────────────────────────────────────────────────

def _detect_worker(preview: Dict[str, str]) -> Optional[Tuple[str, str]]:
    # Newest Owner: marker wins, then the signed latest-comment author
    # (PROTOCOL.md §3.8: any stable signed id is an identity) — rendered
    # verbatim, no roster (wl-84).
    for candidate in (preview.get("owner") or "", preview.get("author") or ""):
        candidate = candidate.strip()
        if candidate:
            return (WORKER_BYLINE_ICON, candidate)
    # Fallback for unsigned history: parse an Owner: line out of the
    # coordination text itself.
    m = _OWNER_LINE_RE.search(preview.get("body") or "")
    if m and m.group(1).strip():
        return (WORKER_BYLINE_ICON, m.group(1).strip())
    return None


def _scoped_labels(labels: Optional[List[str]], scope_product: str) -> List[str]:
    """Drop the label the view already states (wl-55): in a scoped view the
    product:<scope> chip is redundant on every card. Other product: labels
    (cross-files, mislabels) still render; the All view hides nothing."""
    if not scope_product:
        return list(labels or [])
    return [l for l in (labels or []) if l != f"product:{scope_product}"]


def _render_task_card(
    t: Task, preview: Dict[str, str], scope_product: str = ""
) -> str:
    ext = t.ext_id or ""
    id_label = f"#{t.id}"
    if ext:
        id_label = f"#{t.id} · {ext}"
    ext_html = f"<span class='tb-card-id'>{_esc(id_label)}</span>"
    priority_badge = _render_priority_badge(int(t.priority or 3))
    chip_labels = _scoped_labels(t.labels, scope_product)
    labels_html = (
        " ".join(_label_chip(l, _label_tier(l)) for l in chip_labels[:4])
        if chip_labels else ""
    )
    extra_labels = (
        f"<span class='dim tb-card-more'>+{len(chip_labels) - 4}</span>"
        if len(chip_labels) > 4 else ""
    )
    updated_attr = _esc(t.updated_at or "")
    updated_human = (
        f"<span class='tb-card-ago' data-iso='{updated_attr}'>"
        f"{_esc((t.updated_at or '')[:19])}</span>"
        if t.updated_at else ""
    )
    preview_body = preview.get("body") if preview else ""
    preview_author = preview.get("author") if preview else ""
    preview_html = ""
    if preview_body:
        body_short = preview_body[:200] + ("…" if len(preview_body) > 200 else "")
        author_chip = (
            f"<span class='tb-card-preview-author'>{_esc(preview_author)}</span>"
            if preview_author else ""
        )
        preview_html = (
            "<div class='tb-card-preview'>"
            f"{author_chip}"
            f"<span>{_esc(body_short)}</span>"
            "</div>"
        )
    prio_class = f"tb-prio-{int(t.priority or 3)}"
    has_detail = bool(preview_html)
    detail_html = ""
    if has_detail:
        detail_html = (
            "<div class='tb-card-detail'>"
            + preview_html
            + "</div>"
        )
    # Byline on every column (wl-54): backlog reads as "responsible",
    # done reads as "worked by" — same signal, the newest identity on record.
    worker_html = ""
    if preview:
        worker = _detect_worker(preview)
        if worker:
            icon, label_text = worker
            worker_html = (
                f"<div class='tb-card-worker'>"
                f"<span>{icon}</span> <span>{_esc(label_text)}</span>"
                f"</div>"
            )
    decision_html = ""
    # Any needs:* label reads as "waiting on somebody" — label vocabulary is
    # store data, so no specific label names are special-cased here (wl-84).
    if any((l or "").strip().lower().startswith("needs:") for l in (t.labels or [])):
        decision_html = "<div class='tb-card-decision'>Needs input</div>"
    gate_html = ""
    if task_is_gated(t):
        if t.gate_type == "timer" and t.gate_until:
            gate_label = f"Gated until {t.gate_until[:10]}"
        else:
            gate_label = "Gated"
        gate_html = f"<div class='tb-card-gate'>{_badge(gate_label, 'warning')}</div>"
    # 3-row anatomy (wl-36): [id · priority] / [title, 2-line clamp] /
    # [labels · age]. The whole card toggles the detail; links/controls opt out.
    expand_attr = (
        " onclick=\"if(event.target.closest('a,button,select,input'))return;"
        "this.classList.toggle('expanded');\""
        if has_detail else ""
    )
    meta_row = ""
    if labels_html or updated_human:
        meta_row = (
            "<div class='tb-card-meta'>"
            + labels_html + extra_labels
            + (f"<span class='tb-card-meta-ago'>{updated_human}</span>" if updated_human else "")
            + "</div>"
        )
    return (
        f"<article class='tb-card {prio_class}{' tb-card--has-detail' if has_detail else ''}' draggable='true' "
        f"data-task-id='{_esc(t.id)}' data-status='{_esc(t.status)}' "
        f"ondragstart='adminBoardDragStart(event)' "
        f"ondragend='adminBoardDragEnd(event)'{expand_attr}>"
        "<header class='tb-card-head'>"
        f"{ext_html}"
        f"<span class='tb-card-priority'>{priority_badge}</span>"
        "</header>"
        f"<a class='tb-card-title' href='/admin/tasks/{_esc(t.id)}'>"
        f"{_esc(t.title)}</a>"
        + decision_html
        + gate_html
        + worker_html
        + meta_row
        + detail_html
        + "</article>"
    )


# Cards shown per column before the rest collapse behind "Show all N" —
# same cap on every column so the pagination rhythm doesn't vary by status.
# Columns are fixed-height and scroll, so the cap is a DOM-weight guard, not
# a viewport fit: 50 cards scroll fine; beyond that the button takes over.
_BOARD_COLUMN_CAP = 50


def _render_column_body(
    col_status: str,
    col_tasks: List[Task],
    previews: Dict[str, Dict[str, str]],
    scope_product: str = "",
) -> str:
    if not col_tasks:
        return "<div class='tb-col-empty dim'>No tasks in this column.</div>"
    visible = col_tasks[:_BOARD_COLUMN_CAP]
    hidden = col_tasks[_BOARD_COLUMN_CAP:]
    cards_visible = "".join(
        _render_task_card(t, previews.get(t.id, {}), scope_product)
        for t in visible
    )
    if not hidden:
        return cards_visible
    cards_hidden = "".join(
        _render_task_card(t, previews.get(t.id, {}), scope_product)
        for t in hidden
    )
    more_id = f"tb-col-more-{_esc(col_status)}"
    hidden_id = f"tb-col-hidden-{_esc(col_status)}"
    return (
        cards_visible
        + f"<div class='tb-col-more' id='{more_id}'>"
        + f"<button class='btn' onclick='document.getElementById(\"{hidden_id}\").hidden=false;"
        + f"this.parentElement.hidden=true;'>Show all {len(col_tasks)}</button>"
        + "</div>"
        + f"<div id='{hidden_id}' hidden>{cards_hidden}</div>"
    )


def _render_task_board(
    tasks: List[Task],
    previews: Dict[str, Dict[str, str]],
    column_counts: Optional[Dict[str, int]] = None,
    scope_product: str = "",
) -> str:
    grouped: Dict[str, List[Task]] = {col: [] for col in _BOARD_COLUMNS}
    for t in tasks:
        if t.status in grouped:
            grouped[t.status].append(t)

    # Backlog is a triage queue: order by urgency (Urgent → Low), stable so
    # the tracker's recency order still breaks ties. Other columns stay on
    # recency — Done/In Progress read as activity feeds, not queues.
    grouped[TaskStatus.BACKLOG].sort(key=lambda t: int(t.priority or 3))

    cols_html_parts: List[str] = []
    for col_status in _BOARD_COLUMNS:
        col_tasks = grouped[col_status]
        label = _STATUS_LABELS.get(col_status, col_status)
        body_html = _render_column_body(
            col_status, col_tasks, previews, scope_product
        )
        # Empty columns collapse to a slim rail (wl-36). Both the normal
        # header and the rail label are always in the DOM; CSS shows one
        # based on .tb-col--rail, so the JS poll only toggles the class.
        rail_cls = " tb-col--rail" if not col_tasks else ""
        # Header count is the true scope count (wl-47) — the page fetch is
        # capped, so len(col_tasks) may be only the most-recent slice. The
        # cap-note span is always in the DOM so the poll can toggle it.
        shown = len(col_tasks)
        scope_n = shown
        if column_counts:
            scope_n = max(shown, int(column_counts.get(col_status, 0) or 0))
        truncated = shown > 0 and scope_n > shown
        note_hidden = "" if truncated else " hidden"
        note_text = f"most recent {shown} shown" if truncated else ""
        cols_html_parts.append(
            f"<section class='tb-col{rail_cls}'"
            f" data-status='{_esc(col_status)}'"
            f" ondragover='adminBoardDragOver(event)'"
            f" ondragleave='adminBoardDragLeave(event)'"
            f" ondrop='adminBoardDrop(event)'>"
            "<header class='tb-col-head'>"
            f"<h3>{_esc(label)}</h3>"
            f"<span class='tb-col-cap-note' data-cap-note{note_hidden}>"
            f"{_esc(note_text)}</span>"
            f"<span class='tb-col-count' data-count>{scope_n}</span>"
            "</header>"
            "<div class='tb-rail-label' aria-hidden='true'>"
            f"<span class='tb-rail-name'>{_esc(label)}</span>"
            f"<span class='tb-rail-count' data-count>{scope_n}</span>"
            "</div>"
            f"<div class='tb-col-body'>{body_html}</div>"
            "</section>"
        )

    return (
        "<div class='tb-board' id='admin-task-board'>"
        + "".join(cols_html_parts)
        + "</div>"
    )


def _render_view_toggle(
    current_view: str,
    status: str,
    label: str,
    priority: Optional[int] = None,
    *,
    list_path: str = TICKETS_APP_ALL,
    product: str = "",
) -> str:
    def _cls(name: str) -> str:
        return "tb-view-btn" + (" active" if name == current_view else "")

    def _href(view: str) -> str:
        st = (status or "").strip()
        q = _wq_query_for_view(
            view, st, label, priority, product=product, list_path=list_path
        )
        return f"{list_path}?{q}"

    return (
        "<div class='tb-view-toggle'>"
        f"<a class='{_cls('board')}' href='{_esc(_href('board'))}'>Board</a>"
        f"<a class='{_cls('table')}' href='{_esc(_href('table'))}'>Table</a>"
        "</div>"
    )


def _render_comments(comments: List[TaskComment]) -> str:
    if not comments:
        return "<p class='dim'>No comments yet.</p>"
    parts: List[str] = []
    for c in reversed(comments):
        author = _esc(c.author or "anon")
        when = _esc(c.created_at[:19] if c.created_at else "")
        body = _esc(c.body).replace("\n", "<br/>")
        parts.append(
            "<div style='border:1px solid var(--border, #2a2a2a);"
            "border-radius:6px; padding:10px; margin-bottom:8px;'>"
            f"<div class='dim' style='font-size:var(--fs-xs); margin-bottom:6px;'>"
            f"{author} · {when}</div>"
            f"<div style='font-size:var(--fs-sm); line-height:1.5;'>{body}</div>"
            "</div>"
        )
    return "".join(parts)


def _board_styles() -> str:
    return """
<style>
.tb-toolbar { display:flex; align-items:center; gap:12px; margin-bottom:12px;
              flex-wrap:wrap; }
.tb-toolbar .tb-view-toggle { margin-bottom:0; }
.tb-toolbar .tb-quick-add { margin-left:auto; }

.tb-view-toggle { display:inline-flex; gap:0; border:1px solid var(--border);
                  border-radius:var(--r-md); overflow:hidden; margin-bottom:12px; }
.tb-view-btn { padding:6px 16px; font-size:var(--fs-sm); color:var(--muted);
               text-decoration:none; background:var(--bg2); transition:all .12s; }
.tb-view-btn:hover { color:var(--fg); background:var(--hover-tint); }
.tb-view-btn.active { color:var(--neon); background:color-mix(in srgb, var(--neon) 10%, transparent);
                      font-weight:600; }
.tb-view-btn + .tb-view-btn { border-left:1px solid var(--border); }

/* Flex board (wl-36): real columns share space, empty ones collapse to rails. */
.tb-board { display:flex; gap:12px; align-items:stretch; }
.tb-col { flex:1 1 0; min-width:220px;
          border:1px solid var(--border); border-radius:var(--r-lg);
          background:var(--bg2); display:flex; flex-direction:column;
          height:calc(100vh - 180px); min-height:200px;
          transition:border-color .15s, background .15s, flex-basis .2s; }
.tb-col.drag-over { border-color:var(--neon);
                    background:var(--clr-interactive-bg); }
.tb-col-head { display:flex; align-items:center; justify-content:space-between;
               padding:4px 10px; border-bottom:1px solid var(--border);
               position:sticky; top:0; background:var(--bg2); z-index:1; }
.tb-col-head h3 { margin:0; font-size:var(--fs-sm); font-weight:600;
                  color:var(--fg); text-transform:uppercase;
                  letter-spacing:.06em; }
.tb-col-count { font-size:10px; color:var(--dim);
                border:1px solid var(--border); border-radius:999px;
                padding:0 8px; font-variant-numeric:tabular-nums; }
.tb-col-cap-note { font-size:10px; color:var(--dim); font-style:italic;
                   margin-left:auto; padding-right:8px; white-space:nowrap;
                   font-variant-numeric:tabular-nums; }
.tb-col-cap-note[hidden] { display:none; }
.tb-col-body { flex:1 1 auto; min-height:0; overflow-y:auto; padding:4px 6px 6px; }
.tb-col-body > * + * { margin-top:6px; }
.tb-col-empty { padding:10px 0; font-size:var(--fs-xs); text-align:center; }

/* Empty-column rail (wl-36): slim vertical strip; header/body hide, label shows. */
.tb-rail-label { display:none; }
.tb-col--rail { flex:0 0 34px; min-width:34px; border-style:dashed; }
.tb-col--rail .tb-col-head, .tb-col--rail .tb-col-body { display:none; }
.tb-col--rail .tb-rail-label {
  display:flex; flex-direction:column; align-items:center; gap:10px;
  padding:12px 0; flex:1 1 auto; }
.tb-rail-name { writing-mode:vertical-rl; font-size:10px; font-weight:600;
                text-transform:uppercase; letter-spacing:.14em;
                color:var(--dim); }
.tb-rail-count { font-size:10px; color:var(--dim);
                 font-variant-numeric:tabular-nums; }

.tb-card { display:block; border:1px solid var(--border);
           border-radius:var(--r-md); background:var(--bg);
           padding:7px 9px; cursor:grab; transition:all .12s;
           font-size:var(--fs-sm); line-height:1.45; position:relative; }
.tb-card:hover { border-color:var(--neon); transform:translateY(-1px); }
.tb-card.dragging { opacity:.5; cursor:grabbing; }

/* Priority stripe (wl-36): systematic left-edge encoding — signal for
   High/Urgent, border-tone for Normal, none for Low. Inset box-shadow so it
   survives the border-color change on hover. */
.tb-card.tb-prio-1 { box-shadow: inset 3px 0 0 var(--red);
                     background: color-mix(in srgb, var(--red) 5%, var(--bg)); }
.tb-card.tb-prio-2 { box-shadow: inset 3px 0 0 var(--neon); }
.tb-card.tb-prio-3 { box-shadow: inset 3px 0 0 var(--border); }
.tb-card.tb-prio-4 { opacity:.85; }
.tb-card-head { display:flex; align-items:center; justify-content:space-between;
                gap:6px; margin-bottom:3px; }
.tb-card-ext { font-family:var(--font-mono); font-size:10px; color:var(--dim);
               font-weight:600; letter-spacing:.05em; }
.tb-card-id { font-family:var(--font-mono); font-size:11px; color:var(--neon);
              font-weight:700; letter-spacing:.03em; opacity:.85; }
.tb-card-priority { flex:0 0 auto; }
.tb-card-title { display:-webkit-box; -webkit-line-clamp:2;
                 -webkit-box-orient:vertical; overflow:hidden;
                 color:var(--fg); text-decoration:none;
                 font-weight:500; word-break:break-word;
                 font-size:var(--fs-xs); line-height:1.4; }
.tb-card-title:hover { color:var(--neon); }
/* Byline (wl-54): responsible/worked-by identity, shown on every column. */
.tb-card-worker { display:flex; align-items:center; gap:4px; margin-top:4px;
                  font-family:var(--font-mono); font-size:10px;
                  color:var(--muted); letter-spacing:.03em; }
/* Gate chip (wl-21): shown while gate_type withholds the ticket from ready. */
.tb-card-gate { margin-top:4px; }
/* Meta row (wl-36): labels + age share one line; age right-aligned. */
.tb-card-meta { margin-top:5px; display:flex; flex-wrap:wrap; gap:3px;
                align-items:center; }
.tb-card-meta .badge { font-size:9px; padding:1px 5px; }
.tb-card-meta-ago { margin-left:auto; font-size:10px; color:var(--dim);
                    white-space:nowrap; }
.tb-card--has-detail { cursor:pointer; }
.tb-card-more { font-size:10px; }
/* Collapsible detail section — hidden by default, shown on click */
.tb-card-detail { display:none; margin-top:8px; padding-top:8px;
                  border-top:1px solid var(--border); }
.tb-card.expanded .tb-card-detail { display:block; }
.tb-card-preview { font-size:var(--fs-xs); color:var(--muted);
                   line-height:1.5; max-height:120px; overflow-y:auto; }
.tb-card-preview-author { font-family:var(--font-mono);
                          font-size:10px; color:var(--dim);
                          margin-bottom:4px; display:block; }
@media (max-width:1100px) {
  .tb-board { flex-wrap:wrap; }
  .tb-col { flex:1 1 45%; height:auto; }
  /* Wrapped layout has no fixed column height, so clamp the body instead —
     with the 50-card cap a full column would otherwise be several
     thousand px tall. */
  .tb-col-body { max-height:70vh; }
  .tb-col--rail { flex:1 1 100%; min-height:34px; }
  .tb-col--rail .tb-rail-label { flex-direction:row; padding:0 12px; }
  .tb-col--rail .tb-rail-name { writing-mode:horizontal-tb; }
}

/* ── In-progress pulse animation ─────────────────────────────────── */
@keyframes tb-ip-pulse {
  0%, 100% { box-shadow: inset 3px 0 0 var(--neon), 0 0 6px color-mix(in srgb, var(--neon) 14%, transparent); }
  50%       { box-shadow: inset 3px 0 0 var(--neon), 0 0 20px color-mix(in srgb, var(--neon) 40%, transparent); }
}
.tb-card[data-status="in_progress"] {
  animation: tb-ip-pulse 2.8s ease-in-out infinite;
  border-color: color-mix(in srgb, var(--neon) 38%, transparent);
}
.tb-card[data-status="in_progress"]:hover {
  animation-play-state: paused;
}

/* ── Activity strip ───────────────────────────────────────────────── */
#admin-activity-strip {
  display:flex; align-items:center; gap:0; flex-wrap:nowrap;
  overflow-x:auto; overflow-y:hidden;
  background:color-mix(in srgb, var(--neon) 4%, transparent);
  border:1px solid color-mix(in srgb, var(--neon) 20%, transparent);
  border-radius:var(--r-md);
  padding:6px 12px;
  margin-bottom:10px;
  min-height:32px;
  scrollbar-width:none;
}
#admin-activity-strip:empty::before {
  content: 'No tasks in progress';
  font-size:var(--fs-xs);
  color:var(--dim);
}
.tb-strip-item {
  display:flex; align-items:center; gap:6px;
  flex-shrink:0;
  font-size:var(--fs-xs);
  color:var(--muted);
  text-decoration:none;
  padding:2px 10px 2px 0;
  border-right:1px solid color-mix(in srgb, var(--neon) 18%, transparent);
  margin-right:10px;
  white-space:nowrap;
  overflow:hidden;
  max-width:320px;
}
.tb-strip-item:last-child { border-right:none; margin-right:0; }
.tb-strip-item:hover { color:var(--fg); }
.tb-strip-dot {
  display:inline-block;
  width:7px; height:7px;
  border-radius:50%;
  background:var(--neon);
  flex-shrink:0;
  animation:tb-ip-pulse 2s ease-in-out infinite;
}
.tb-strip-id { font-family:var(--font-mono); color:var(--neon);
               font-weight:700; font-size:10px; flex-shrink:0; }
.tb-strip-title { font-weight:500; color:var(--fg);
                  overflow:hidden; text-overflow:ellipsis; max-width:140px; }
.tb-strip-comment { color:var(--dim); overflow:hidden;
                    text-overflow:ellipsis; max-width:140px; }

/* ── Compact inline filter toolbar ───────────────────────────────── */
.tb-toolbar { display:flex; align-items:center; gap:8px; margin-bottom:10px;
              flex-wrap:wrap; }
.tb-toolbar-filter { display:contents; }
.tb-toolbar-filter select,
.tb-toolbar-filter input[type=text] {
  padding:4px 8px; font-size:var(--fs-xs);
  background:var(--bg2); border:1px solid var(--border);
  border-radius:var(--r-sm); color:var(--fg);
  height:28px; line-height:1;
}
.tb-toolbar-filter input[type=text] { min-width:130px; }
.tb-toolbar-filter .btn { padding:4px 10px; font-size:var(--fs-xs); height:28px; }
.tb-toolbar .tb-quick-add { margin-left:auto; }

/* Quick-add modal (floats over board/table views). */
.tb-qa-overlay { position:fixed; inset:0; background:rgba(0,0,0,.55);
                 z-index:1000; display:flex; align-items:flex-start;
                 justify-content:center; padding-top:12vh; }
.tb-qa-overlay[hidden] { display:none; }
.tb-qa-modal { width:min(520px, 92vw); background:var(--bg2);
               border:1px solid var(--neon); border-radius:var(--r-lg);
               box-shadow:0 12px 48px rgba(0,0,0,.45);
               padding:18px 20px; }
.tb-qa-modal header { display:flex; align-items:center; justify-content:space-between;
                      margin-bottom:12px; }
.tb-qa-modal header h3 { margin:0; font-size:var(--fs-md); color:var(--neon);
                         letter-spacing:.04em; text-transform:uppercase; }
.tb-qa-modal header button { background:none; border:0; color:var(--dim);
                             font-size:20px; cursor:pointer; padding:0 4px; }
.tb-qa-modal header button:hover { color:var(--fg); }
.tb-qa-modal form { display:flex; flex-direction:column; gap:10px; }
.tb-qa-modal input, .tb-qa-modal select { width:100%; }
.tb-qa-row { display:grid; grid-template-columns:140px 1fr; gap:10px; }
.tb-qa-actions { display:flex; gap:10px; align-items:center;
                 justify-content:flex-end; margin-top:4px; }
.tb-qa-actions #admin-qa-status { margin-right:auto; font-size:var(--fs-xs); }
</style>
"""


def _client_js() -> str:
    return r"""
<script>
  async function adminTaskStatusChange(sel) {
    var taskId = sel.getAttribute('data-task-id');
    var status = sel.value;
    sel.disabled = true;
    try {
      var resp = await fetch('/api/admin/tasks/' + encodeURIComponent(taskId), {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status: status })
      });
      var j = await resp.json();
      if (!j.ok) { showToast('Update failed: ' + (j.error || resp.status), 'error'); return; }
      showToast('Status updated', 'success');
    } catch (e) {
      showToast('Network error', 'error');
    } finally {
      sel.disabled = false;
    }
  }

  /* ──────────────────────────────────────────────────────────────────
     Kanban board (SEO-205)
     ────────────────────────────────────────────────────────────────── */

  var __ADMIN_BOARD_POLL_MS = 10000;
  var __ADMIN_BOARD_COLUMNS = ['backlog', 'in_review', 'in_progress', 'done'];
  var __ADMIN_BOARD_COLUMN_CAP = 50; // mirror of Python _BOARD_COLUMN_CAP (wl-11)
  var __ADMIN_BOARD_LABELS = {
    'backlog': 'Backlog',
    'in_progress': 'In Progress',
    'in_review': 'In Review',
    'done': 'Done',
    'canceled': 'Canceled'
  };
  var __ADMIN_BOARD_POLL_HANDLE = null;

  function adminBoardFmtAgo(iso) {
    if (!iso) return '';
    var then = Date.parse(iso);
    if (isNaN(then)) return iso.slice(0, 19);
    var diff = Math.max(0, Math.floor((Date.now() - then) / 1000));
    if (diff < 45)        return 'just now';
    if (diff < 90)        return 'a minute ago';
    if (diff < 3600)      return Math.floor(diff / 60) + 'm ago';
    if (diff < 5400)      return 'an hour ago';
    if (diff < 86400)     return Math.floor(diff / 3600) + 'h ago';
    if (diff < 172800)    return 'yesterday';
    if (diff < 2592000)   return Math.floor(diff / 86400) + 'd ago';
    if (diff < 5184000)   return 'a month ago';
    if (diff < 31536000)  return Math.floor(diff / 2592000) + 'mo ago';
    return Math.floor(diff / 31536000) + 'y ago';
  }

  function adminBoardEscape(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  /* wl-76: Jump-# box. Composite ids (wl-503) navigate directly; a bare
     number resolves against the current product scope when the command
     bar is scoped to one, otherwise (All view) it's ambiguous across
     stores and /api/admin/tasks/resolve is asked to disambiguate. */
  async function adminBoardJumpSubmit(form) {
    var box = document.getElementById('wq-jump-ambiguous');
    if (box) { box.hidden = true; box.innerHTML = ''; }
    var v = (form.elements['id'].value || '').trim().replace(/^#/, '');
    if (!v) return false;
    if (/^[A-Za-z]+-\d+$/.test(v)) {
      window.location.href = '/admin/tasks/' + v;
      return false;
    }
    if (!/^\d+$/.test(v)) {
      if (typeof showToast === 'function') showToast('Use a number or an id like wl-503', 'error');
      return false;
    }
    var prefix = form.getAttribute('data-scope-prefix') || '';
    if (prefix) {
      window.location.href = '/admin/tasks/' + prefix + '-' + v;
      return false;
    }
    try {
      var resp = await fetch('/api/admin/tasks/resolve?id=' + encodeURIComponent(v));
      var j = await resp.json();
      if (j.ok && j.match) {
        window.location.href = '/admin/tasks/' + j.match;
      } else if (j.ok && j.candidates && j.candidates.length && box) {
        box.innerHTML = j.candidates.map(function (c) {
          return "<a href='/admin/tasks/" + adminBoardEscape(c.id) + "'>" +
            adminBoardEscape(c.id) + ' — ' + adminBoardEscape(c.title) + '</a>';
        }).join('');
        box.hidden = false;
      } else if (typeof showToast === 'function') {
        showToast('No ticket #' + v + ' found', 'error');
      }
    } catch (e) {
      if (typeof showToast === 'function') showToast('Network error', 'error');
    }
    return false;
  }

  // wl-9: card markup is server-rendered only (_render_task_card). The poll
  // returns task.card_html; JS never reimplements badge/chip/worker anatomy.

  function adminActivityStripRebuild(tasks) {
    var strip = document.getElementById('admin-activity-strip');
    if (!strip) return;
    var inProgress = (tasks || []).filter(function(t) { return t.status === 'in_progress'; });
    if (!inProgress.length) {
      strip.innerHTML = '';
      return;
    }
    strip.innerHTML = inProgress.map(function(t) {
      var idStr = '#' + adminBoardEscape(t.id);
      var title = adminBoardEscape((t.title || '').slice(0, 60));
      var comment = '';
      if (t.last_comment_preview) {
        comment = ' — ' + adminBoardEscape((t.last_comment_preview || '').slice(0, 70));
      }
      return "<a class='tb-strip-item' href='/admin/tasks/" + adminBoardEscape(t.id) + "'"
           + " title='" + adminBoardEscape(t.title) + "'>"
           + "<span class='tb-strip-dot'></span>"
           + "<span class='tb-strip-id'>" + idStr + "</span>"
           + "<span class='tb-strip-title'>" + title + "</span>"
           + (comment ? "<span class='tb-strip-comment'>" + comment + "</span>" : "")
           + "</a>";
    }).join('');
  }

  function adminSummaryRebuild(tasks) {
    var bar = document.querySelector('.ts-summary-bar');
    if (!bar) return;
    var inFlight = 0, inReview = 0, done = 0, waiting = 0, backlog = 0;
    (tasks || []).forEach(function(t) {
      if (t.status === 'in_progress') inFlight++;
      else if (t.status === 'in_review') inReview++;
      else if (t.status === 'done') done++;
      else if (t.status === 'backlog') backlog++;
      if ((t.labels || []).indexOf('needs:decision') >= 0
          && t.status !== 'done' && t.status !== 'canceled') waiting++;
    });
    var parts = [
      "<span class='ts-sum-chip ts-sum-flight'>" + inFlight + " in flight</span>"
    ];
    if (inReview) parts.push("<span class='ts-sum-chip ts-sum-review'>" + inReview + " in review</span>");
    parts.push("<span class='ts-sum-chip ts-sum-done'>" + done + " done</span>");
    if (waiting) parts.push("<span class='ts-sum-chip ts-sum-waiting'>" + waiting + " waiting on you</span>");
    parts.push("<span class='ts-sum-chip ts-sum-backlog'>" + backlog + " backlog</span>");
    bar.innerHTML = parts.join(' &middot; ');
  }

  function adminBoardRebuild(tasks, columnCounts) {
    adminActivityStripRebuild(tasks);
    adminSummaryRebuild(tasks);
    var board = document.getElementById('admin-task-board');
    if (!board) return;
    var bucket = {};
    __ADMIN_BOARD_COLUMNS.forEach(function(c) { bucket[c] = []; });
    (tasks || []).forEach(function(t) {
      if (bucket.hasOwnProperty(t.status)) bucket[t.status].push(t);
    });
    var cols = board.querySelectorAll('.tb-col');
    cols.forEach(function(col) {
      var status = col.getAttribute('data-status');
      var list = bucket[status] || [];
      // Mirror of the SSR backlog sort in _render_task_board (wl-8):
      // urgency first, stable so recency breaks ties.
      if (status === 'backlog') {
        list = list.slice().sort(function(a, b) {
          return (parseInt(a.priority, 10) || 3) - (parseInt(b.priority, 10) || 3);
        });
      }
      var body = col.querySelector('.tb-col-body');
      // wl-47: header shows the true (uncapped) scope count for the current
      // filters; the cap note says how much of it the capped fetch holds.
      var scopeN = list.length;
      if (columnCounts && typeof columnCounts === 'object') {
        var n = Number(columnCounts[status] || 0);
        if (isFinite(n) && n > scopeN) scopeN = n;
      }
      var countEls = col.querySelectorAll('[data-count]');
      countEls.forEach(function(el) { el.textContent = String(scopeN); });
      var capNote = col.querySelector('[data-cap-note]');
      if (capNote) {
        var truncated = list.length > 0 && scopeN > list.length;
        capNote.hidden = !truncated;
        capNote.textContent = truncated
          ? 'most recent ' + list.length + ' shown' : '';
      }
      // Empty columns collapse to rails (wl-36); a rail inflating back into
      // a column is itself the signal that work entered that state.
      col.classList.toggle('tb-col--rail', !list.length);
      if (!body) return;
      if (!list.length) {
        body.innerHTML = "<div class='tb-col-empty dim'>No tasks in this column.</div>";
        return;
      }
      var visible = list.slice(0, __ADMIN_BOARD_COLUMN_CAP);
      var hidden = list.slice(__ADMIN_BOARD_COLUMN_CAP);
      // wl-9: swap pre-rendered card HTML (same bytes as SSR) by task id.
      var html = visible.map(function(t) { return t.card_html || ''; }).join('');
      if (hidden.length) {
        var moreId = 'tb-col-more-' + status;
        var hiddenId = 'tb-col-hidden-' + status;
        html += "<div class='tb-col-more' id='" + moreId + "'>"
              + "<button class='btn' onclick='document.getElementById(\"" + hiddenId + "\").hidden=false;this.parentElement.hidden=true;'>Show all " + list.length + "</button></div>"
              + "<div id='" + hiddenId + "' hidden>"
              + hidden.map(function(t) { return t.card_html || ''; }).join('')
              + "</div>";
      }
      body.innerHTML = html;
    });
    adminBoardTouchRelativeTime();
  }

  function adminBoardTouchRelativeTime() {
    var spans = document.querySelectorAll('.tb-card-ago[data-iso]');
    for (var i = 0; i < spans.length; i++) {
      var iso = spans[i].getAttribute('data-iso');
      if (iso) spans[i].textContent = adminBoardFmtAgo(iso);
    }
  }

  // wl-10: advanced-filters chip groups collapse overflow/one-off labels
  // behind a per-group toggle; the search box expands + filters live.
  function adminBoardToggleChipGroup(btn) {
    var group = btn.closest('.wq-chip-group');
    if (!group) return;
    var collapsed = group.getAttribute('data-collapsed') === '1';
    group.setAttribute('data-collapsed', collapsed ? '0' : '1');
  }

  function adminBoardFilterChipSearch(query) {
    var q = (query || '').trim().toLowerCase();
    var container = document.querySelector('.wq-filter-chips');
    if (!container) return;
    container.classList.toggle('wq-chips-searching', !!q);
    var groups = container.querySelectorAll('.wq-chip-group');
    groups.forEach(function(g) {
      var chips = g.querySelectorAll('.notif-filter-chip');
      var anyVisible = false;
      chips.forEach(function(c) {
        var match = !q || (c.textContent || '').toLowerCase().indexOf(q) !== -1;
        c.classList.toggle('wq-chip-search-hidden', !match);
        if (match) anyVisible = true;
      });
      if (q) {
        g.setAttribute('data-collapsed', '0');
        g.style.display = anyVisible ? '' : 'none';
      } else {
        g.setAttribute('data-collapsed', g.getAttribute('data-default-collapsed') || '1');
        g.style.display = '';
      }
    });
  }

  function adminBoardUpdateFilterBuckets(scopeCounts, scopeTotal) {
    if (!scopeCounts || typeof scopeCounts !== 'object') return;
    var total = Number(scopeTotal);
    if (!isFinite(total)) {
      total = 0;
      Object.keys(scopeCounts).forEach(function(k) {
        total += Number(scopeCounts[k] || 0);
      });
    }
    var allEl = document.querySelector("[data-wq-bucket-count='__all__']");
    if (allEl) allEl.textContent = String(total);
    Object.keys(scopeCounts).forEach(function(status) {
      var el = document.querySelector("[data-wq-bucket-count='" + status + "']");
      if (el) el.textContent = String(Number(scopeCounts[status] || 0));
    });
  }

  function adminWorkQueueApiQuery() {
    var g = window.__WQ_POLL_PARAMS || {};
    var q = new URLSearchParams();
    q.set('with_preview', '1');
    q.set('limit', '500');
    if (g.status) q.set('status', g.status);
    if (g.label) q.set('label', g.label);
    if (g.priority) q.set('priority', g.priority);
    if (g.product) q.set('product', g.product);
    return q.toString();
  }

  async function adminBoardFetch() {
    try {
      var resp = await fetch('/api/admin/tasks?' + adminWorkQueueApiQuery(), {
        headers: { 'Accept': 'application/json' }
      });
      var j = await resp.json();
      if (!j || !j.ok) return;
      adminBoardRebuild(j.tasks || [], j.column_counts || null);
      adminBoardUpdateFilterBuckets(j.scope_counts || {}, j.scope_total);
    } catch (e) {
      /* Silent — a transient 500 or network blip shouldn't nuke the UI. */
    }
  }

  function adminBoardDragStart(ev) {
    var card = ev.currentTarget;
    card.classList.add('dragging');
    var taskId = card.getAttribute('data-task-id');
    ev.dataTransfer.setData('text/plain', taskId);
    ev.dataTransfer.effectAllowed = 'move';
  }

  function adminBoardDragEnd(ev) {
    ev.currentTarget.classList.remove('dragging');
    document.querySelectorAll('.tb-col.drag-over').forEach(function(c) {
      c.classList.remove('drag-over');
    });
  }

  function adminBoardDragOver(ev) {
    ev.preventDefault();
    ev.dataTransfer.dropEffect = 'move';
    ev.currentTarget.classList.add('drag-over');
  }

  function adminBoardDragLeave(ev) {
    if (ev.currentTarget === ev.target) {
      ev.currentTarget.classList.remove('drag-over');
    }
  }

  async function adminBoardDrop(ev) {
    ev.preventDefault();
    var col = ev.currentTarget;
    col.classList.remove('drag-over');
    var taskId = ev.dataTransfer.getData('text/plain');
    if (!taskId) return;
    var newStatus = col.getAttribute('data-status');
    var card = document.querySelector(".tb-card[data-task-id='" + CSS.escape(taskId) + "']");
    if (!card) return;
    if (card.getAttribute('data-status') === newStatus) return;
    var prevParent = card.parentNode;
    var body = col.querySelector('.tb-col-body');
    if (body) body.insertBefore(card, body.firstChild);
    card.setAttribute('data-status', newStatus);
    try {
      var resp = await fetch('/api/admin/tasks/' + encodeURIComponent(taskId), {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status: newStatus })
      });
      var j = await resp.json();
      if (!j.ok) throw new Error(j.error || ('HTTP ' + resp.status));
      if (typeof showToast === 'function') {
        showToast('Moved to ' + (__ADMIN_BOARD_LABELS[newStatus] || newStatus), 'success');
      }
      adminBoardFetch();
    } catch (e) {
      if (prevParent) prevParent.appendChild(card);
      if (typeof showToast === 'function') {
        showToast('Move failed: ' + e.message, 'error');
      }
    }
  }

  function adminBoardInit() {
    var hasBoard = !!document.getElementById('admin-task-board');
    var hasStrip = !!document.getElementById('admin-activity-strip');
    // wl-38: Table view has no live poll target, but its Age column reuses
    // the same .tb-card-ago[data-iso] relative-time hook as Board cards.
    var hasTable = !!document.querySelector('.ts-timetable-table');
    if (!hasBoard && !hasStrip && !hasTable) return;
    adminBoardTouchRelativeTime();
    if (!hasBoard && !hasStrip) return;
    adminBoardFetch();
    if (__ADMIN_BOARD_POLL_HANDLE) clearInterval(__ADMIN_BOARD_POLL_HANDLE);
    __ADMIN_BOARD_POLL_HANDLE = setInterval(function() {
      if (document.visibilityState === 'hidden') return;
      adminBoardFetch();
    }, __ADMIN_BOARD_POLL_MS);
    document.addEventListener('visibilitychange', function() {
      if (document.visibilityState === 'visible') adminBoardFetch();
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', adminBoardInit);
  } else {
    adminBoardInit();
  }
</script>
"""


# ── Preview comment helpers ───────────────────────────────────────────────

def _load_preview_comments(
    tracker: Any, tasks: List[Task]
) -> Dict[str, Dict[str, str]]:
    preview: Dict[str, Dict[str, str]] = {}
    if not hasattr(tracker, "list_comments"):
        return preview
    for t in tasks:
        try:
            comments = tracker.list_comments(t.id)
        except Exception:
            continue
        if not comments:
            continue
        latest = max(comments, key=lambda c: c.created_at or "")
        body = (latest.body or "").strip().replace("\r", "")
        first_line = next((ln.strip() for ln in body.split("\n") if ln.strip()), "")
        if len(first_line) > 180:
            first_line = first_line[:177].rstrip() + "…"
        preview[t.id] = {
            "body": first_line,
            "author": latest.author or "",
            "created_at": latest.created_at or "",
            "owner": _extract_owner(comments),
        }
    return preview


