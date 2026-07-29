"""Work queue engine for dev mode (SEO-180).

The queue is a thin layer over a :class:`ProjectTracker`. It loads the
current task list, applies a deterministic priority sort, drops anything
whose blockers aren't Done, and exposes the resulting "ready" set so the
dev dashboard can suggest the next batch.

Dependencies are parsed out of the ticket description: any
``SEO-\\d+`` reference appearing under a Markdown heading whose text
contains "depends" or "blocked" is treated as a blocker. This matches
the convention the Linear export already uses (see SEO-180 itself for
an example) and avoids needing the optional Linear ``relations`` graph.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set

from worklane.trackers.protocol import ProjectTracker, Task, TaskStatus, task_is_gated


# ── parsing helpers ──────────────────────────────────────────────────────

# Match a markdown heading line ("##" / "###" / "**Depends on**" etc.)
# Lower-cased before testing the keyword. We accept both real H2/H3
# headings and bold-style "section" labels because Linear exports use
# both forms.
_HEADING_RE = re.compile(
    r"^(?:#{1,6}\s+|\*\*)\s*(?P<title>[^*\n]+?)\s*(?:\*\*)?\s*$",
    re.MULTILINE,
)
_SEO_TICKET_RE = re.compile(r"\bSEO-(\d+)\b", re.IGNORECASE)
_LOCAL_TICKET_RE = re.compile(r"(?:^|[^A-Za-z0-9_])#(\d+)\b")
_BLOCKER_KEYWORDS = ("depend", "blocked by", "blockers", "requires")


def _extract_ticket_refs(text: str) -> List[str]:
    """Extract ticket references from text, preserving first-seen order.

    Supported forms:
    - ``SEO-123`` (legacy external id)
    - ``#123`` (local numeric id)
    """
    if not text:
        return []
    refs: List[str] = []
    seen: Set[str] = set()
    for m in _SEO_TICKET_RE.finditer(text):
        ref = f"SEO-{m.group(1)}"
        if ref not in seen:
            seen.add(ref)
            refs.append(ref)
    for m in _LOCAL_TICKET_RE.finditer(text):
        ref = m.group(1)
        if ref not in seen:
            seen.add(ref)
            refs.append(ref)
    return refs


def parse_blockers(description: str) -> List[str]:
    """Return ticket IDs (e.g. ``["SEO-164"]``) listed as blockers.

    A blocker is any ``SEO-\\d+`` reference that lives in a section whose
    heading mentions "depends", "blocked by", "blockers", or "requires".
    A section runs from the end of its heading line to the start of the
    next heading (any level), or end-of-text. Returned IDs are
    de-duplicated, preserving first-seen order.
    """
    if not description:
        return []
    text = description

    seen: Set[str] = set()
    out: List[str] = []

    headings = list(_HEADING_RE.finditer(text))
    for i, match in enumerate(headings):
        title = match.group("title").strip().lower()
        if not any(k in title for k in _BLOCKER_KEYWORDS):
            continue
        body_start = match.end()
        body_end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
        refs = _extract_ticket_refs(text[body_start:body_end])
        for ref in refs:
            if ref not in seen:
                seen.add(ref)
                out.append(ref)

    # Fallback for short descriptions like "Depends on #326" without
    # markdown section headers.
    if not out:
        for line in text.splitlines():
            lower = line.lower()
            if not any(k in lower for k in _BLOCKER_KEYWORDS):
                continue
            refs = _extract_ticket_refs(line)
            for ref in refs:
                if ref not in seen:
                    seen.add(ref)
                    out.append(ref)
    return out


# ── public datatypes ─────────────────────────────────────────────────────

@dataclass
class BlockerInfo:
    """Details about a single blocker for a blocked task."""

    ticket_id: str
    title: str  # empty string if unknown
    status: str  # empty string if unknown

    def to_dict(self) -> Dict[str, str]:
        return {"ticket_id": self.ticket_id, "title": self.title, "status": self.status}


@dataclass
class BlockedTask:
    """A backlog task that can't be dispatched because of unresolved blockers."""

    task: Task
    blockers: List[BlockerInfo] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        return {
            **self.task.to_dict(),
            "blockers": [b.to_dict() for b in self.blockers],
        }


@dataclass
class Batch:
    """A group of tickets the dev can dispatch to one terminal at once."""

    tickets: List[Task]
    shared_files: List[str] = field(default_factory=list)

    @property
    def ids(self) -> List[str]:
        return [t.ext_id or t.id for t in self.tickets]

    def to_dict(self) -> Dict[str, object]:
        return {
            "ids": self.ids,
            "titles": [t.title for t in self.tickets],
            "shared_files": list(self.shared_files),
        }


# ── work queue ───────────────────────────────────────────────────────────

# Lower number = higher urgency (matches Linear's priority field, where
# 1 = Urgent and 4 = Low). Tasks with priority 0 ("None") sort last.
def _priority_key(task: Task) -> tuple[int, str]:
    p = int(task.priority or 0)
    bucket = p if 1 <= p <= 4 else 99
    return (bucket, task.updated_at or "")


_READY_STATUSES = (TaskStatus.BACKLOG,)


class WorkQueue:
    """Read-only view over the tracker's ready queue.

    Construct once per request — instances cache the loaded task list so
    repeated lookups (priority filter, dependency check) don't re-hit
    the tracker. Mutating tracker state should still go through
    ``ProjectTracker`` directly.
    """

    def __init__(self, tracker: ProjectTracker, *, limit: int = 500) -> None:
        self._tracker = tracker
        self._limit = limit
        self._all: List[Task] = list(tracker.list_tasks(limit=limit))
        self._by_ext: Dict[str, Task] = {}
        for t in self._all:
            if t.ext_id:
                self._by_ext[t.ext_id] = t
            self._by_ext[t.id] = t

    # ── accessors ────────────────────────────────────────────────────

    @property
    def all_tasks(self) -> List[Task]:
        return list(self._all)

    def by_status(self, status: str) -> List[Task]:
        return [t for t in self._all if t.status == status]

    def in_progress(self) -> List[Task]:
        return self.by_status(TaskStatus.IN_PROGRESS)

    # ── filtering ────────────────────────────────────────────────────

    def _is_done(self, ticket_id: str) -> bool:
        t = self._by_ext.get(ticket_id)
        return t is not None and t.status == TaskStatus.DONE

    def blockers_for(self, task: Task) -> List[str]:
        return parse_blockers(task.description)

    def is_ready(self, task: Task) -> bool:
        """True if every parsed blocker for ``task`` is Done.

        Unknown blockers (referenced ticket isn't in the local tracker)
        count as still blocking — safer than dispatching work whose
        prerequisites we can't verify.
        """
        for bid in self.blockers_for(task):
            if not self._is_done(bid):
                return False
        return True

    def ready(
        self,
        *,
        statuses: Sequence[str] = _READY_STATUSES,
        labels: Optional[Sequence[str]] = None,
    ) -> List[Task]:
        """Return tasks eligible for dispatch, sorted by priority.

        ``statuses`` — which statuses count as "ready to start". Defaults
        to backlog only; pass ``(BACKLOG, IN_PROGRESS)`` to include
        already-claimed work in the surface.
        ``labels`` — optional whitelist; only tickets whose label set
        intersects the filter survive.
        """
        wanted_statuses = set(statuses)
        wanted_labels = set(labels) if labels else None

        candidates = [t for t in self._all if t.status in wanted_statuses]
        if wanted_labels is not None:
            candidates = [
                t for t in candidates if wanted_labels.intersection(t.labels)
            ]
        candidates = [t for t in candidates if self.is_ready(t)]
        candidates = [t for t in candidates if not task_is_gated(t)]
        # wl-297: defense-in-depth — umbrella coordination wrappers never dispatch
        candidates = [t for t in candidates if "umbrella" not in t.labels]
        candidates.sort(key=_priority_key)
        return candidates

    # ── blocked ──────────────────────────────────────────────────────

    def blocked(self) -> List[BlockedTask]:
        """Backlog tasks that fail is_ready() — blockers not yet done.

        Returns each task paired with details about its unresolved
        blockers so the dashboard can show *why* it's stuck.
        """
        out: List[BlockedTask] = []
        for t in self._all:
            if t.status != TaskStatus.BACKLOG:
                continue
            blocker_ids = self.blockers_for(t)
            if not blocker_ids:
                continue  # no blockers declared — it's ready, not blocked
            unresolved: List[BlockerInfo] = []
            for bid in blocker_ids:
                dep = self._by_ext.get(bid)
                if dep is None:
                    unresolved.append(BlockerInfo(ticket_id=bid, title="", status=""))
                elif dep.status != TaskStatus.DONE:
                    unresolved.append(BlockerInfo(
                        ticket_id=dep.ext_id or dep.id,
                        title=dep.title,
                        status=dep.status,
                    ))
            if unresolved:
                out.append(BlockedTask(task=t, blockers=unresolved))
        out.sort(key=lambda bt: _priority_key(bt.task))
        return out

    # ── orphans ──────────────────────────────────────────────────────

    def orphans(self) -> List[Task]:
        """Tickets stuck in ``in_progress`` from a previous session.

        The dashboard surfaces these on startup so the developer can
        resume, comment, or hand them off rather than letting them rot.
        """
        return self.by_status(TaskStatus.IN_PROGRESS)


def find_orphans(tracker: ProjectTracker) -> List[Task]:
    """Convenience wrapper used by routes that don't need a full queue."""
    return list(tracker.list_tasks(status=TaskStatus.IN_PROGRESS, limit=200))


# ── dispatch prompt ──────────────────────────────────────────────────────

def build_dispatch_prompt(tickets: Iterable[Task]) -> str:
    """Render the Claude Code prompt for an approved batch.

    Output is the literal text the developer pastes into a fresh Claude
    Code session — e.g. ``"work SEO-164, SEO-180"``. The session
    instructions in :doc:`AGENTS.md` already define the per-ticket
    protocol, so the prompt only needs to name the tickets.
    """
    ids = [t.ext_id or t.id for t in tickets if (t.ext_id or t.id)]
    if not ids:
        return ""
    if len(ids) == 1:
        return f"work {ids[0]}"
    return "work " + ", ".join(ids)


__all__ = [
    "Batch",
    "BlockedTask",
    "BlockerInfo",
    "WorkQueue",
    "build_dispatch_prompt",
    "find_orphans",
    "parse_blockers",
]
