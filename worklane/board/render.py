"""Board cards, columns, comments, and claim-age byline."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from worklane.board.badges import _label_tier, _render_priority_badge
from worklane.board.constants import (
    OWNER_BYLINE_ICON,
    _BOARD_COLUMNS,
    _STATUS_LABELS,
)
from worklane.rendering import _badge, _esc, _label_chip
from worklane.trackers import Task, TaskComment, TaskStatus, task_is_gated

def _detect_owner(preview: Dict[str, str]) -> Optional[Tuple[str, str]]:
    # Newest Owner: marker wins, then the signed latest-comment author
    # (PROTOCOL.md §3.8: any stable signed id is an identity) — rendered
    # verbatim, no roster (wl-84). `owner` already comes from _extract_owner,
    # which scans every comment's full body for an Owner: line regardless of
    # signing status, so a second body-regex pass here would never find
    # anything that didn't already win above (wl-3).
    for candidate in (preview.get("owner") or "", preview.get("author") or ""):
        candidate = candidate.strip()
        if candidate:
            return (OWNER_BYLINE_ICON, candidate)
    return None


_INFLIGHT_STATUSES = (TaskStatus.IN_PROGRESS, TaskStatus.IN_REVIEW)


def _claim_stale_minutes() -> int:
    """Staleness threshold for claim-age badges — env override (wl-104),
    default matches the existing 90-min stalled-ticket convention
    (PROTOCOL.md §4) though this is a distinct, narrower check (no comment
    since the claim itself, not just no update)."""
    try:
        return int(os.environ.get("TICKETING_CLAIM_STALE_MINUTES", "90"))
    except ValueError:
        return 90


def _parse_iso_ts(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _owner_claim_html(
    t: Task, preview: Dict[str, str], *, now: Optional[datetime] = None
) -> str:
    """Owner byline for a ticket, plus claim age + staleness hint when the
    ticket is in_progress/in_review (wl-104). Shared by Board cards and
    Table rows so the two views never drift."""
    if not preview:
        return ""
    owner = _detect_owner(preview)
    if not owner:
        return ""
    icon, label_text = owner
    parts = [f"<span>{icon}</span> <span>{_esc(label_text)}</span>"]
    if t.status in _INFLIGHT_STATUSES:
        claimed_at = (preview.get("owner_claimed_at") or "").strip()
        if claimed_at:
            esc_claimed = _esc(claimed_at)
            parts.append(
                f"<span class='tb-card-ago tb-card-claim-age' data-iso='{esc_claimed}'>"
                f"{_esc(claimed_at[:19])}</span>"
            )
            ts = _parse_iso_ts(claimed_at)
            no_comment_since_claim = (preview.get("created_at") or "").strip() == claimed_at
            if ts is not None and no_comment_since_claim:
                now_ = now or datetime.now(ts.tzinfo or timezone.utc)
                if (now_ - ts) >= timedelta(minutes=_claim_stale_minutes()):
                    parts.append(
                        "<span class='tb-card-stale' "
                        "title='Claimed, no comment since — stale claim.'>stale</span>"
                    )
    return "".join(parts)


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
    # in_progress/in_review also get claim age + staleness (wl-104).
    claim_html = _owner_claim_html(t, preview)
    owner_html = f"<div class='tb-card-owner'>{claim_html}</div>" if claim_html else ""
    decision_html = ""
    # Any needs:* label reads as "waiting on somebody" — label vocabulary is
    # store data, so no specific label names are special-cased here (wl-84).
    if any((l or "").strip().lower().startswith("needs:") for l in (t.labels or [])):
        decision_html = "<div class='tb-card-decision'>Needs input</div>"
    gate_html = ""
    if task_is_gated(t):
        if t.gate_type == "timer" and t.gate_until:
            gate_label = f"Gated until {t.gate_until[:10]}"
            gate_tier = "warning"
        elif t.gate_type == "deferred":
            gate_label = "Deferred"
            gate_tier = "neutral"
        elif t.gate_type == "tracking":
            gate_label = "Tracking"
            gate_tier = "neutral"
        elif t.gate_type == "human":
            gate_label = "For You"
            gate_tier = "warning"
        else:
            gate_label = "Gated"
            gate_tier = "warning"
        gate_html = f"<div class='tb-card-gate'>{_badge(gate_label, gate_tier)}</div>"
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
        f"<a class='tb-card-title' href='/admin/desk?open={_esc(t.id)}'>"
        f"{_esc(t.title)}</a>"
        + decision_html
        + gate_html
        + owner_html
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
        return "<div class='tb-col-empty dim'>No tickets in this column.</div>"
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



def _render_comments(
    comments: List[TaskComment],
    founder_id: str = "",
    founder_alias: str = "",
) -> str:
    """Comment trail, newest first. wl-149: founder-signed entries render
    the alias with the canonical id dimmed beside it (aliases are paint,
    ids are identity — same signer treatment as the desk drawer, wl-148)."""
    if not comments:
        return "<p class='dim'>No comments yet.</p>"
    parts: List[str] = []
    for c in reversed(comments):
        author = _esc(c.author or "anon")
        if founder_alias and (c.author or "") == founder_id:
            author = f"{_esc(founder_alias)} <span class='dim'>({_esc(founder_id)})</span>"
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

