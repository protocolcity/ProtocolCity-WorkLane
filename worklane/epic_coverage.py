"""Epic child-coverage guard (wl-347 / pc-978 gap).

Keeps prose child inventories honest with filed board children — no new
schema. Two checks apply when closing an umbrella/epic coordination wrapper:

1. **Structured ``## Children`` section** (opt-in convention): every list
   item under a recognized heading must carry a ticket id (composite
   ``slug-N``, ``#N``, or bare digits). Id-less rows refuse close.
2. **Residual open children**: tickets labeled ``parent:<id>`` /
   ``slice-of:<id>`` (or a ``parent-child`` relation edge) that are not
   ``done``/``canceled`` refuse close.

Non-wrapper tickets are untouched. Epics without a ``## Children`` section
and without filed children still close (historical free-form Done-when
prose is not hard-parsed — that would false-positive).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable, List, Optional, Sequence, Set, Tuple

from worklane.trackers.protocol import TaskStatus

# Labels that mark a coordination wrapper (PROCESS §3.12 / wl-297).
_WRAPPER_LABELS = frozenset({"umbrella", "epic"})

# Headings that open a structured child inventory (case-insensitive).
_CHILDREN_HEADING_RE = re.compile(
    r"^(?:#{1,6}\s+|)\s*"
    r"(?:children|child\s+tickets?|child\s+list|child\s+slices?)\s*$",
    re.IGNORECASE,
)

# Any markdown heading (ends the Children section).
_ANY_HEADING_RE = re.compile(r"^#{1,6}\s+\S")

# List rows: "- item", "* item", "1. item", "- [ ] item", "- [x] item".
_LIST_ITEM_RE = re.compile(
    r"^\s*(?:[-*+]|\d+\.)\s+(?:\[[ xX]\]\s+)?(.+\S)\s*$"
)

# Ticket refs inside a child row: composite slug-N, #N, or bare digits token.
_COMPOSITE_ID_RE = re.compile(r"\b([a-z][a-z0-9]*-\d+)\b", re.IGNORECASE)
_HASH_ID_RE = re.compile(r"(?:^|[^A-Za-z0-9_])#(\d+)\b")
_BARE_DIGITS_RE = re.compile(r"\b(\d{1,9})\b")

_DONE = frozenset({TaskStatus.DONE, TaskStatus.CANCELED})


def is_epic_wrapper(
    labels: Optional[Sequence[str]] = None,
    *,
    gate_note: Optional[str] = None,
) -> bool:
    """True when the ticket is an umbrella/epic coordination wrapper."""
    labs = {(lab or "").strip().lower() for lab in (labels or [])}
    if labs & _WRAPPER_LABELS:
        return True
    note = (gate_note or "").strip().lower()
    if note and ("umbrella" in note or "epic" in note):
        return True
    return False


def extract_ticket_ids_from_line(text: str) -> List[str]:
    """Return ticket id tokens from one child-list line (first-seen order)."""
    found: List[str] = []
    seen: Set[str] = set()
    for m in _COMPOSITE_ID_RE.finditer(text or ""):
        tok = m.group(1).lower()
        if tok not in seen:
            seen.add(tok)
            found.append(tok)
    for m in _HASH_ID_RE.finditer(text or ""):
        tok = m.group(1)
        if tok not in seen:
            seen.add(tok)
            found.append(tok)
    # Bare digits only when no composite/# already present — avoids double-
    # counting "wl-12" as both composite and "12".
    if not found:
        for m in _BARE_DIGITS_RE.finditer(text or ""):
            tok = m.group(1)
            if tok not in seen:
                seen.add(tok)
                found.append(tok)
    return found


def parse_children_section(description: str) -> List[Tuple[str, List[str]]]:
    """Parse ``## Children`` (etc.) list rows → ``(line_text, ids)``.

    Returns an empty list when no recognized heading is present.
    """
    text = description or ""
    lines = text.splitlines()
    in_section = False
    rows: List[Tuple[str, List[str]]] = []
    for ln in lines:
        stripped = ln.strip()
        if not in_section:
            # Allow both "## Children" and bare "Children" / "**Children**"
            candidate = stripped.strip("*").strip()
            if _CHILDREN_HEADING_RE.match(candidate) or _CHILDREN_HEADING_RE.match(
                stripped
            ):
                in_section = True
            continue
        # Next markdown heading ends the section (not a list item).
        if _ANY_HEADING_RE.match(stripped):
            # Could be another Children variant — stop; first section wins.
            break
        if not stripped:
            continue
        m = _LIST_ITEM_RE.match(ln)
        if not m:
            # Non-list prose inside the section is ignored (notes, blank).
            continue
        body = m.group(1).strip()
        rows.append((body, extract_ticket_ids_from_line(body)))
    return rows


def parent_label_forms(parent_id: str, *, product_prefix: Optional[str] = None) -> List[str]:
    """Label suffixes that may point at this parent (raw + composite)."""
    raw = str(parent_id or "").strip()
    if not raw:
        return []
    forms: List[str] = []
    seen: Set[str] = set()

    def _add(s: str) -> None:
        s = s.strip()
        if s and s not in seen:
            seen.add(s)
            forms.append(s)

    _add(raw)
    # composite → also raw numeric tail
    if "-" in raw:
        tail = raw.rsplit("-", 1)[-1]
        if tail.isdigit():
            _add(tail)
    elif raw.isdigit() and product_prefix:
        _add(f"{product_prefix}-{raw}")
        _add(f"{product_prefix.lower()}-{raw}")
    return forms


def find_labeled_children(
    tracker: Any,
    parent_id: str,
    *,
    product_prefix: Optional[str] = None,
) -> List[Any]:
    """Tasks whose labels include ``parent:<form>`` or ``slice-of:<form>``."""
    found: List[Any] = []
    seen: Set[str] = set()
    for form in parent_label_forms(parent_id, product_prefix=product_prefix):
        for prefix in ("parent:", "slice-of:"):
            label = f"{prefix}{form}"
            try:
                kids = tracker.list_tasks(label=label)
            except Exception:
                kids = []
            for t in kids or []:
                tid = str(getattr(t, "id", "") or "")
                if tid and tid not in seen:
                    seen.add(tid)
                    found.append(t)
    return found


def find_relation_children(
    db_path: Optional[Path],
    parent_id: str,
) -> List[str]:
    """Child task ids from ``parent-child`` edges where parent is from_id."""
    if db_path is None:
        return []
    try:
        from worklane import relations as relmod  # noqa: PLC0415
    except Exception:
        return []
    try:
        rels = relmod.list_relations(Path(db_path), task_id=str(parent_id))
    except Exception:
        return []
    out: List[str] = []
    parent_norm = str(parent_id).strip()
    parent_tail = parent_norm.rsplit("-", 1)[-1] if "-" in parent_norm else parent_norm
    for r in rels:
        if getattr(r, "relation_type", None) != "parent-child":
            continue
        frm = str(getattr(r, "from_id", "") or "")
        to = str(getattr(r, "to_id", "") or "")
        if frm in (parent_norm, parent_tail) or (
            frm.isdigit() and parent_tail.isdigit() and int(frm) == int(parent_tail)
        ):
            if to:
                out.append(to)
    return out


def _open_child_summaries(
    tracker: Any,
    child_ids: Iterable[str],
    labeled: Sequence[Any],
) -> List[str]:
    """Return short ``id (status)`` strings for children not done/canceled."""
    by_id = {str(getattr(t, "id", "")): t for t in labeled}
    open_bits: List[str] = []
    seen: Set[str] = set()
    for cid in list(child_ids) + [str(getattr(t, "id", "")) for t in labeled]:
        cid = str(cid or "").strip()
        if not cid or cid in seen:
            continue
        seen.add(cid)
        t = by_id.get(cid) or tracker.get_task(cid)
        if t is None:
            # Missing child id from a ## Children row is a separate check.
            continue
        status = getattr(t, "status", "") or ""
        if status not in _DONE:
            open_bits.append(f"{cid} ({status})")
    return open_bits


def coverage_block_reason(
    task: Any,
    tracker: Any,
    *,
    db_path: Optional[Path] = None,
    product_prefix: Optional[str] = None,
) -> Optional[str]:
    """Return an error string if this epic must not close yet, else None.

    Safe no-op for non-wrapper tickets.
    """
    labels = list(getattr(task, "labels", None) or [])
    gate_note = getattr(task, "gate_note", None)
    if not is_epic_wrapper(labels, gate_note=gate_note):
        return None

    parent_id = str(getattr(task, "id", "") or "").strip()
    if not parent_id:
        return None

    desc = getattr(task, "description", None) or ""
    child_rows = parse_children_section(desc)

    # 1) Structured section: every list row needs a ticket id.
    if child_rows:
        idless = [row for row, ids in child_rows if not ids]
        if idless:
            sample = idless[0][:80]
            return (
                "epic child-coverage (wl-347): ## Children list has "
                f"{len(idless)} row(s) without a ticket id — file the child "
                f"and cite it (e.g. `- [ ] wl-N: …`). First: {sample!r}"
            )
        # Cited ids must resolve in this store.
        missing: List[str] = []
        for _row, ids in child_rows:
            for ref in ids:
                # Prefer composite as-is; get_task accepts raw or ext_id.
                raw = ref
                if "-" in ref:
                    raw = ref.rsplit("-", 1)[-1]
                hit = tracker.get_task(ref) or tracker.get_task(raw)
                if hit is None:
                    missing.append(ref)
        if missing:
            return (
                "epic child-coverage (wl-347): ## Children cites unknown "
                f"ticket id(s): {', '.join(missing[:8])} — file them or "
                "fix the list"
            )

    # 2) Residual open children (labels + relations).
    labeled = find_labeled_children(
        tracker, parent_id, product_prefix=product_prefix
    )
    rel_ids = find_relation_children(db_path, parent_id)
    open_kids = _open_child_summaries(tracker, rel_ids, labeled)
    if open_kids:
        sample = ", ".join(open_kids[:6])
        more = f" (+{len(open_kids) - 6} more)" if len(open_kids) > 6 else ""
        return (
            "epic child-coverage (wl-347): cannot close umbrella/epic while "
            f"child tickets are still open: {sample}{more}. Close or cancel "
            "children first, or leave the parent open (PROCESS §3.11/§3.12)"
        )

    return None


def body_is_done_closeout(body: str) -> bool:
    """True when comment body is a §5 Completed:+Verification: close-out."""
    text = body or ""
    if not re.search(r"^\s*completed\s*:", text, re.IGNORECASE | re.MULTILINE):
        return False
    if not re.search(r"^\s*verification\s*:", text, re.IGNORECASE | re.MULTILINE):
        return False
    return True
