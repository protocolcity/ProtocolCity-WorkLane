"""Shared ticket-reference parsing primitives (devqueue + trackers)."""
from __future__ import annotations

import re
from typing import List, Set

_HEADING_RE = re.compile(
    r"^(?:#{1,6}\s+|\*\*)\s*(?P<title>[^*\n]+?)\s*(?:\*\*)?\s*$",
    re.MULTILINE,
)
_SEO_TICKET_RE = re.compile(r"\bSEO-(\d+)\b", re.IGNORECASE)
_LOCAL_TICKET_RE = re.compile(r"(?:^|[^A-Za-z0-9_])#(\d+)\b")
_BLOCKER_KEYWORDS = ("depend", "blocked by", "blockers", "requires")

# Parent-epic references are membership, not dependency — counting them deadlocks.
_EPIC_REF_RE = re.compile(r"\bepic[ \t]*:[ \t]*#\d+", re.IGNORECASE)

# Inline blocker declarations (PROTOCOL.md: "use `Depends on #NNN`").
_REF_TOKEN = r"(?:#\d+|SEO-\d+)\b"
_BLOCKER_DECL_RE = re.compile(
    r"(?:\bdepends?\s+on\b|\bblocked\s+by\b|^[ \t]*blockers?\b)[ \t]*:?[ \t]*"
    rf"(?P<refs>{_REF_TOKEN}(?:[ \t]*(?:,|;|/|&|\+|\band\b)?[ \t]*{_REF_TOKEN})*)",
    re.IGNORECASE | re.MULTILINE,
)


def _extract_ticket_refs(text: str) -> List[str]:
    """Return ticket refs from *text* in first-seen order (SEO-N before #N)."""
    refs: List[str] = []
    seen: Set[str] = set()
    for m in _SEO_TICKET_RE.finditer(text or ""):
        ref = f"SEO-{m.group(1)}"
        if ref not in seen:
            seen.add(ref)
            refs.append(ref)
    for m in _LOCAL_TICKET_RE.finditer(text or ""):
        ref = m.group(1)
        if ref not in seen:
            seen.add(ref)
            refs.append(ref)
    return refs
