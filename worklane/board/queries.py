"""Task list / count queries and owner-preview extraction."""
from __future__ import annotations

import re
from dataclasses import replace
from typing import Any, Dict, List, Optional, Tuple

from worklane.board.product import wq_product_sql_label
from worklane.products import ProductSpec, known_prefix_slug, split_task_id
from worklane.trackers import Task, TaskStatus

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

def _search_terms_for_store(
    spec: ProductSpec, q: Optional[str]
) -> Tuple[Optional[str], Optional[str]]:
    """Return ``(q_text, q_id)`` for one store (wl-493).

    ``q_text`` is the stripped query for title/ext_id LIKE. ``q_id`` is a
    bare rowid when ``q`` is digits, or the numeric rest of a composite
    whose prefix maps to this store (including legacy aliases). Wrong-store
    composites stay title-only so ``q=pc-1389`` does not hit ``wl-1389``.
    """
    raw = (q or "").strip()
    if not raw:
        return None, None
    slug = known_prefix_slug(raw)
    if slug is not None:
        _prefix, rest = raw.split("-", 1)
        q_id = rest if slug == spec.slug and rest else None
        return raw, q_id
    if raw.isdigit():
        return raw, raw
    return raw, None


def _task_id_matches_q(composite_id: str, q: Optional[str]) -> bool:
    """True when ``composite_id`` is an exact id hit for search ranking."""
    raw = (q or "").strip().lower()
    if not raw:
        return False
    tid = str(composite_id).lower()
    if tid == raw:
        return True
    if raw.isdigit() and tid.rsplit("-", 1)[-1] == raw:
        return True
    return False


def list_tasks_for_wq_multi(
    products: List[Tuple[ProductSpec, Any]],
    *,
    status: Optional[str],
    label: Optional[str],
    priority: Optional[int],
    product: str,
    gate_type: Optional[str] = None,
    limit: int = 500,
    q: Optional[str] = None,
    include_description: bool = True,
) -> List[Task]:
    """Tasks across product stores; ``product`` scopes to one slug, "" = all.

    In the one-DB-per-product model a product scope selects a store — no
    ``product:*`` label filtering is involved.

    ``q`` (wl-493) is a bound id/title search: SQL LIKE on each store, not
    a Python scan of every description. Exact id hits sort ahead of title
    hits so a closed composite still surfaces under a tight limit.
    """
    p = (product or "").strip().lower()
    q_norm = (q or "").strip() or None
    merged: List[Task] = []
    for spec, tracker in products:
        if p and spec.slug != p:
            continue
        kwargs: Dict[str, Any] = {
            "status": status,
            "label": (label or "").strip() or None,
            "priority": priority,
            "gate_type": gate_type,
            "limit": limit,
        }
        q_text, q_id = _search_terms_for_store(spec, q_norm)
        if q_text:
            kwargs["q"] = q_text
        if q_id:
            kwargs["q_id"] = q_id
        if not include_description:
            kwargs["include_description"] = False
        try:
            tasks = tracker.list_tasks(**kwargs)
        except TypeError:
            if q_text or q_id:
                tasks = []
            else:
                kwargs.pop("include_description", None)
                tasks = tracker.list_tasks(**kwargs)
        merged.extend(
            replace(t, id=f"{spec.prefix}-{t.id}") for t in tasks
        )
    merged.sort(key=lambda x: x.updated_at or "", reverse=True)
    if q_norm:
        merged.sort(key=lambda x: 0 if _task_id_matches_q(str(x.id), q_norm) else 1)
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


def status_counts_for_scope_multi(
    products: List[Tuple[ProductSpec, Any]],
    product: str,
) -> Dict[str, int]:
    """Unfiltered per-status counts across product stores (wl-354).

    Uses SQL GROUP BY when the tracker exposes ``count_tasks_by_status``;
    falls back to full list materialization only for non-SQLite adapters.
    """
    counts: Dict[str, int] = {s: 0 for s in TaskStatus.ALL}
    p = (product or "").strip().lower()
    for spec, tracker in products:
        if p and spec.slug != p:
            continue
        part = _tracker_status_counts(tracker)
        for s, n in part.items():
            if s in counts:
                counts[s] += int(n)
    return counts


def column_counts_for_scope_multi(
    products: List[Tuple[ProductSpec, Any]],
    product: str,
    *,
    status: Optional[str] = None,
    label: Optional[str] = None,
    priority: Optional[int] = None,
    gate_type: Optional[str] = None,
) -> Dict[str, int]:
    """Filtered per-status counts for board column headers (wl-354).

    Mirrors ``_wq_column_counts`` / list_tasks filter semantics without
    loading full task rows when the tracker supports SQL aggregates.
    """
    counts: Dict[str, int] = {s: 0 for s in TaskStatus.ALL}
    p = (product or "").strip().lower()
    for spec, tracker in products:
        if p and spec.slug != p:
            continue
        part = _tracker_status_counts(
            tracker,
            status=status,
            label=label,
            priority=priority,
            gate_type=gate_type,
        )
        for s, n in part.items():
            if s in counts:
                counts[s] += int(n)
    return counts


def _tracker_status_counts(
    tracker: Any,
    *,
    status: Optional[str] = None,
    label: Optional[str] = None,
    priority: Optional[int] = None,
    gate_type: Optional[str] = None,
) -> Dict[str, int]:
    count_fn = getattr(tracker, "count_tasks_by_status", None)
    if callable(count_fn):
        return count_fn(
            status=status,
            label=(label or "").strip() or None,
            priority=priority,
            gate_type=gate_type,
        )
    # Non-SQL adapter fallback — materialize then count in Python.
    tasks = tracker.list_tasks(
        status=status,
        label=(label or "").strip() or None,
        priority=priority,
        gate_type=gate_type,
        limit=None,
    )
    return _wq_status_counts(tasks)


# Ownership marker line per PROTOCOL.md §3 — `Owner: <agent-id> (<model>)`.
# The parenthetical and anything after it is presentation, not identity.
# The id itself is bounded to PROTOCOL.md §5.2's kebab-case charset (never
# whitespace/parens/colons) rather than "anything but a real newline" —
# a comment body with a literal backslash-n (shell-escaping artifact, not
# an actual line break) has no real newline to stop at, so a looser class
# used to swallow the whole rest of the marker — Workdir:, Start:, Plan: —
# into the byline (wl-129).
_OWNER_LINE_RE = re.compile(r"^Owner:\s*([A-Za-z0-9_.-]+)", re.MULTILINE)


def _extract_owner_claim(comments: List[Any]) -> Tuple[str, str]:
    """Return (owner, claimed_at) from the newest comment carrying an Owner:
    marker — claimed_at is that comment's own timestamp, not the latest
    comment's, so claim age stays correct even after later activity."""
    for c in sorted(comments, key=lambda c: c.created_at or "", reverse=True):
        m = _OWNER_LINE_RE.search(c.body or "")
        if m:
            return m.group(1).strip(), (c.created_at or "")
    return "", ""


def _extract_owner(comments: List[Any]) -> str:
    return _extract_owner_claim(comments)[0]


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
        owner, owner_claimed_at = _extract_owner_claim(comments)
        preview[cid] = {
            "body": first_line,
            "author": latest.author or "",
            "created_at": latest.created_at or "",
            "owner": owner,
            "owner_claimed_at": owner_claimed_at,
        }
    return preview

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
    gate_type: Optional[str] = None,
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
        if gate_type is not None:
            tgt = t.gate_type or ""
            if gate_type == "":
                if tgt:
                    continue
            elif tgt != gate_type:
                continue
        if t.status in counts:
            counts[t.status] += 1
    return counts


def _wq_gate_counts(tasks: List[Task]) -> Dict[str, int]:
    """Count open tickets by gate class across all open statuses."""
    counts: Dict[str, int] = {"": 0, "human": 0, "deferred": 0, "tracking": 0}
    for t in tasks:
        if t.status in (TaskStatus.DONE, TaskStatus.CANCELED):
            continue
        gt = t.gate_type or ""
        if gt in counts:
            counts[gt] += 1
    return counts


def _parse_gate_filter(gate: str) -> Optional[str]:
    """URL gate param → gate_type kwarg for list_tasks/column_counts.

    "" → None (no filter); "none" → "" (ungated/Ready);
    "human"/"deferred"/"timer"/"tracking" → exact.
    """
    g = (gate or "").strip()
    if not g:
        return None
    if g == "none":
        return ""
    if g in ("human", "deferred", "timer", "tracking"):
        return g
    return None

