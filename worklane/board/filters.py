"""Work-queue URL query helpers and filter-bar HTML."""
from __future__ import annotations

import urllib.parse
from typing import Dict, List, Optional, Tuple

from worklane.board.constants import (
    _CHIP_FACET_PREFIXES,
    _CHIP_TOP_N,
    _STATUS_LABELS,
)
from worklane.board.product import _embed_product_query_param, parse_wq_product
from worklane.board.queries import (
    _wq_gate_counts,
    _wq_status_counts,
    list_tasks_for_product_scope,
)
from worklane.products import get_product
from worklane.rendering import _esc
from worklane.trackers import Task, TaskStatus, get_default_tracker

def _wq_query_for_view(
    view: str,
    status: str,
    label: str,
    priority: Optional[int],
    *,
    product: str = "",
    list_path: str = "",
    gate: str = "",
) -> str:
    parts: List[Tuple[str, str]] = [("view", view)]
    if status:
        parts.append(("status", status))
    if label:
        parts.append(("label", label))
    if priority is not None:
        parts.append(("priority", str(priority)))
    if gate:
        parts.append(("gate", gate))
    prod = parse_wq_product(product)
    if prod and _embed_product_query_param(list_path):
        parts.append(("product", prod))
    return urllib.parse.urlencode(parts)


def _render_wq_quick_buckets(
    *,
    list_path: str,
    current_view: str,
    current_status: str,
    label: str,
    priority: Optional[int],
    product: str = "",
    gate: str = "",
    counts: Dict[str, int],
) -> str:
    st = (current_status or "").strip()
    total = sum(counts.get(s, 0) for s in TaskStatus.ALL)

    def _href(status_key: str) -> str:
        return (
            f"{list_path}?"
            f"{_wq_query_for_view(current_view, status_key, label, priority, product=product, list_path=list_path, gate=gate)}"
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
    gate: str = "",
    merged_scope_tasks: Optional[List[Task]] = None,
) -> str:
    prod = parse_wq_product(product)
    if merged_scope_tasks is not None:
        all_tasks = merged_scope_tasks
    else:
        tracker = get_default_tracker()
        all_tasks = list_tasks_for_product_scope(tracker, prod, limit=None)
    counts = _wq_status_counts(all_tasks)
    gate_counts = _wq_gate_counts(all_tasks)
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
        or gate
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
        gate=gate,
        counts=counts,
    )

    # Gate class chips: Ready · For You · Deferred · Tracking (wl-434)
    open_total = sum(gate_counts.values())
    _gate_specs: List[Tuple[str, str, int]] = [
        ("", "All open", open_total),
        ("none", "Ready", gate_counts.get("", 0)),
        ("human", "For You", gate_counts.get("human", 0)),
        ("deferred", "Deferred", gate_counts.get("deferred", 0)),
        ("tracking", "Tracking", gate_counts.get("tracking", 0)),
    ]
    gate_chip_parts: List[str] = []
    for gval, glabel, gcnt in _gate_specs:
        is_active = gate == gval
        active_cls = " wq-gate-chip--active" if is_active else ""
        href = (
            f"{list_path}?"
            f"{_wq_query_for_view(current_view, status, label, priority, product=product, list_path=list_path, gate=gval)}"
        )
        gate_chip_parts.append(
            f"<a href='{_esc(href)}' class='wq-gate-chip{active_cls}' "
            f"title='Show {_esc(glabel)} tickets'>"
            f"{_esc(glabel)}"
            f"<span class='wq-gate-chip-count'>{gcnt}</span>"
            f"</a>"
        )
    gate_row = (
        "<div class='wq-cmdbar-row wq-gate-row' aria-label='Filter by gate class'>"
        + "".join(gate_chip_parts)
        + "</div>"
    )

    prod_hidden = (
        f"<input type='hidden' name='product' value='{_esc(prod)}'/>"
        if (prod and _embed_product_query_param(list_path))
        else ""
    )
    gate_hidden = (
        f"<input type='hidden' name='gate' value='{_esc(gate)}'/>"
        if gate
        else ""
    )

    advanced_body = (
        f"<form method='get' action='{_esc(list_path)}' class='wq-advanced-form ts-filter-form'>"
        f"<input type='hidden' name='view' value='{_esc(current_view)}'/>"
        f"{prod_hidden}"
        f"{gate_hidden}"
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
        + "</div>"
        + gate_row
        + f"<div id='wq-advanced-panel' class='wq-advanced-panel'{panel_hidden}>{advanced_body}</div>"
        + "</div>"
    )
