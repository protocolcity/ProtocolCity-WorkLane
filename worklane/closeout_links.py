"""Close-out Links landing-ref guard (wl-396 / wf-171).

Hands must evidence landing on main in the ``Links:`` section of a §5
close-out. Full ``git merge-base --is-ancestor`` reachability is an agent
duty (PROCESS §5.1.3) and is not probed here — the server often has no
checkout of the workdir. This module enforces the cheap mechanical floor:

**Links must contain at least one git commit SHA token** (7–40 hex digits).

That rejects path-only / prose-only Links that let tickets close while the
work sits only on a shift branch (workforce/wf-171). PR URLs that embed a
commit SHA also pass; bare PR numbers without a SHA do not.
"""

from __future__ import annotations

import re
from typing import List, Optional, Set

# Git short SHAs are typically ≥7; full objects are 40. Avoid matching
# ticket suffixes, short hex noise (e.g. "abc123"), or pure digit ids.
_COMMIT_SHA_RE = re.compile(r"\b[0-9a-fA-F]{7,40}\b")

# Section body after "Links:" until Follow-ups: (or end of comment).
_LINKS_SECTION_RE = re.compile(
    r"(?im)^\s*Links\s*:\s*(.*?)(?=^\s*Follow-ups\s*:|\Z)",
    re.DOTALL | re.MULTILINE,
)

_CLOSEOUT_START_RE = re.compile(r"^\s*Completed\s*:", re.IGNORECASE)

LINKS_SHA_HINT = (
    "Links: must cite a landing commit SHA (7–40 hex) — "
    "path-only / prose-only Links are rejected (PROCESS §5, wl-396 / wf-171). "
    "Agent still verifies the SHA is an ancestor of origin/main before close "
    "(PROCESS §5.1.3); engine enforces presence only."
)


def find_commit_shas(text: str) -> List[str]:
    """Return commit-SHA tokens found in *text* (first-seen order)."""
    seen: Set[str] = set()
    out: List[str] = []
    for m in _COMMIT_SHA_RE.finditer(text or ""):
        sha = m.group(0).lower()
        if sha not in seen:
            seen.add(sha)
            out.append(sha)
    return out


def extract_links_section(body: str) -> str:
    """Return the Links: section body from a §5 close-out comment, or ''."""
    m = _LINKS_SECTION_RE.search(body or "")
    if not m:
        return ""
    return (m.group(1) or "").strip()


def links_missing_landing_sha(links_text: str) -> Optional[str]:
    """Return an error string when *links_text* has no commit SHA, else None."""
    if find_commit_shas(links_text or ""):
        return None
    return LINKS_SHA_HINT


def closeout_links_violation(body: str) -> Optional[str]:
    """If *body* is a Completed: close-out, require a SHA in Links.

    Non-close-out comments return None. Callers that already enforce
    Verification:/Links: presence should run this after that check.
    """
    text = body or ""
    first_line = next((ln.strip() for ln in text.split("\n") if ln.strip()), "")
    if not _CLOSEOUT_START_RE.match(first_line):
        return None
    # Missing Links: is handled by the existing §5 section guard.
    if "Links:" not in text and "links:" not in text.lower():
        return None
    return links_missing_landing_sha(extract_links_section(text))
