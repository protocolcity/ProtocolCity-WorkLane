"""File-conflict detection for the dev work queue (SEO-180).

When two ready tickets touch overlapping files we want them to land in
the same terminal — otherwise two parallel agents race on the same line
ranges and one rebases the other into mush. This module does the static
analysis: pull file references out of ticket descriptions, then bucket
tickets whose file sets intersect into shared groups.

The extractor is intentionally lenient: it matches on path-shaped tokens
(``segment/segment[.ext]``) rather than requiring backticks or fenced
code, because Linear ticket descriptions wrap paths inconsistently. We
filter to a small allowlist of source-file extensions so prose sentences
like "AAPL/SPY pair" don't get treated as paths.
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Sequence, Set

from worklane.devqueue.queue import Batch
from worklane.trackers.protocol import Task


# Extensions we treat as source files. Only files we'd actually edit —
# lock files, cached binaries, and one-shot CSV samples are deliberately
# excluded so the conflict graph doesn't pick up shared report fixtures.
_SOURCE_EXTS = (
    ".py", ".pyi", ".md", ".yaml", ".yml", ".json", ".toml",
    ".html", ".css", ".js", ".ts", ".tsx", ".sh", ".sql",
)

# Match a path-like token: at least one slash, no whitespace, ends with
# one of our source extensions. We strip surrounding markdown noise
# (backticks, parentheses, commas, periods) before matching.
_PATH_RE = re.compile(
    r"(?P<path>[A-Za-z0-9_./\-]+/[A-Za-z0-9_./\-]+\.(?:"
    + "|".join(ext.lstrip(".") for ext in _SOURCE_EXTS)
    + r"))"
)


def extract_file_refs(description: str) -> List[str]:
    """Return distinct file paths mentioned in ``description``.

    Order is preserved (first occurrence wins) so callers can show the
    most prominent path first in UI summaries.
    """
    if not description:
        return []
    seen: Set[str] = set()
    out: List[str] = []
    for match in _PATH_RE.finditer(description):
        path = match.group("path").strip().rstrip(".,);:'\"")
        if path and path not in seen:
            seen.add(path)
            out.append(path)
    return out


def group_by_file_conflict(tasks: Sequence[Task]) -> List[Batch]:
    """Bucket ``tasks`` so each :class:`Batch` shares no files with others.

    Implementation: build the undirected "shares-a-file" graph, then run
    union-find. Tickets that mention zero files become singleton
    batches — they don't conflict with anyone, so they ship one at a
    time and the developer can still parallelize them across terminals
    without risk.

    Input order is preserved within each batch (first matching ticket
    leads), and batches are returned in the order their lead ticket
    appears in ``tasks`` so the dashboard ranking stays stable.
    """
    if not tasks:
        return []

    files_per_task: List[List[str]] = [extract_file_refs(t.description) for t in tasks]
    parent = list(range(len(tasks)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    file_owner: Dict[str, int] = {}
    for idx, files in enumerate(files_per_task):
        for f in files:
            if f in file_owner:
                union(file_owner[f], idx)
            else:
                file_owner[f] = idx

    groups: Dict[int, List[int]] = {}
    for idx in range(len(tasks)):
        root = find(idx)
        groups.setdefault(root, []).append(idx)

    ordered_roots = sorted(groups.keys(), key=lambda r: min(groups[r]))
    batches: List[Batch] = []
    for root in ordered_roots:
        members = sorted(groups[root])
        shared: List[str] = []
        seen: Set[str] = set()
        for m in members:
            for f in files_per_task[m]:
                if f not in seen:
                    seen.add(f)
                    shared.append(f)
        batches.append(
            Batch(tickets=[tasks[m] for m in members], shared_files=shared)
        )
    return batches


__all__ = ["extract_file_refs", "group_by_file_conflict"]
