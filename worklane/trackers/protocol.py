"""ProjectTracker Protocol and shared value types (SEO-164).

Any module that wants to read or write work items talks to this interface,
never directly to Linear, SQLite, or any other backing store. Two concrete
adapters live alongside: :mod:`worklane.trackers.sqlite` (default) and
:mod:`worklane.trackers.linear` (optional bridge).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable


# Canonical status vocabulary used across adapters. Adapters are free to
# map these to their own internal vocab — Linear uses "Backlog / In
# Progress / In Review / Done / Canceled"; SQLiteTracker stores the raw
# string below in the ``status`` column.
class TaskStatus:
    BACKLOG = "backlog"
    IN_PROGRESS = "in_progress"
    IN_REVIEW = "in_review"
    DONE = "done"
    CANCELED = "canceled"

    ALL = (BACKLOG, IN_PROGRESS, IN_REVIEW, DONE, CANCELED)


@dataclass
class TaskComment:
    id: Optional[str]
    task_id: str
    body: str
    author: str = ""
    created_at: str = ""


@dataclass
class Task:
    """Shared task shape — one row per work item across all adapters.

    ``id`` is the adapter's internal identifier (integer PK stringified
    for SQLiteTracker, Linear's ``SEO-171`` identifier for LinearTracker).
    ``ext_id`` is an optional cross-system reference — e.g. SQLiteTracker
    stores the original Linear identifier here after the migration script
    imports it, so cross-references in old doc links keep resolving.
    """

    id: str
    title: str
    description: str = ""
    status: str = TaskStatus.BACKLOG
    priority: int = 3  # 1=urgent, 2=high, 3=normal, 4=low (matches Linear)
    labels: List[str] = field(default_factory=list)
    ext_id: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""
    # wl-21: gates as data. gate_type is None (no gate), "human" (blocks
    # the ready queue until manually cleared; action-shaped note golds For You),
    # "timer" (blocks until gate_until, then auto-thaws — see task_is_gated()),
    # "deferred" (wl-261: parked indefinitely; never enters ready or For You),
    # or "tracking" (wl-434: structural epic/umbrella; never ready or For You;
    # stays listable for chief-of-staff decomposition).
    gate_type: Optional[str] = None
    gate_until: Optional[str] = None
    gate_note: Optional[str] = None
    # wl-250: entry channel — how the ticket entered the system.
    # Values: "mcp", "cli", "api", "agent", "import", "unknown", or None.
    intake: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "ext_id": self.ext_id,
            "title": self.title,
            "description": self.description,
            "status": self.status,
            "priority": self.priority,
            "labels": list(self.labels),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "gate_type": self.gate_type,
            "gate_until": self.gate_until,
            "gate_note": self.gate_note,
            "intake": self.intake,
        }


# Allowed non-empty gate_type values for create/update ("" clears).
# Keep in sync with API / MCP validators and task_is_gated.
GATE_TYPES = frozenset({"human", "timer", "deferred", "tracking"})
# Gates that always withhold ready (no calendar thaw).
GATE_TYPES_ALWAYS = frozenset({"human", "deferred", "tracking"})
# Gates that withhold ready and must never paint For You / Map gold.
GATE_TYPES_NO_ATTENTION = frozenset({"deferred", "tracking"})


def task_is_gated(task: "Task") -> bool:
    """True if ``task`` should be withheld from the ready queue right now.

    Human / deferred / tracking gates withhold until someone clears them
    (gate_type="").  Timer gates withhold until ``gate_until`` passes, then
    auto-thaw — computed here at read time rather than by mutating the row,
    so there's no trigger event to miss (unlike the dependency-freeze label,
    which is flipped by a write path).  A timer gate with an unparseable or
    missing ``gate_until`` fails safe (stays gated).  Unknown non-empty
    gate_type values also fail closed (withhold ready) so cross-product
    tracking umbrellas never leak into worker feeds (wl-434).
    """
    if not task.gate_type:
        return False
    if task.gate_type in GATE_TYPES_ALWAYS:
        return True
    if task.gate_type == "timer":
        if not task.gate_until:
            return True
        try:
            until = datetime.fromisoformat(task.gate_until.replace("Z", "+00:00"))
        except ValueError:
            return True
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) < until
    # Unknown gate class: fail closed (withhold from ready).
    return True


@runtime_checkable
class ProjectTracker(Protocol):
    """Interface every tracker adapter implements.

    Methods are deliberately synchronous — tracker calls happen from
    request handlers and CLI scripts, and both adapters are either local
    SQLite (no network) or MCP-fronted (caller decides async boundary).
    """

    name: str

    def list_tasks(
        self,
        *,
        status: Optional[str] = None,
        label: Optional[str] = None,
        priority: Optional[int] = None,
        gate_type: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Task]: ...

    def get_task(self, task_id: str) -> Optional[Task]: ...

    def create_task(
        self,
        *,
        title: str,
        description: str = "",
        status: str = TaskStatus.BACKLOG,
        priority: int = 3,
        labels: Optional[List[str]] = None,
        ext_id: Optional[str] = None,
        actor: str = "",
    ) -> Task: ...

    def update_status(
        self, task_id: str, status: str, actor: str = ""
    ) -> Optional[Task]: ...

    def add_comment(self, task_id: str, body: str, author: str = "") -> TaskComment: ...

    def list_comments(self, task_id: str) -> List[TaskComment]: ...
