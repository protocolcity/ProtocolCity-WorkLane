"""LinearTracker — optional bridge to Linear SaaS (SEO-164).

The tradeOS runtime does not call Linear directly; Claude Code terminals
use the Linear MCP server for interactive read/write. This adapter
exists so code that talks to ``ProjectTracker`` can be pointed at a
Linear mirror when an operator still wants external visibility
(``TRADEOS_TRACKER=linear``).

The current implementation is intentionally a stub: every mutating
method raises ``NotImplementedError`` so no half-working sync can race
with the Linear MCP writes. Read methods return empty lists — enough
to keep a dev dashboard booting without a network call. A full
implementation that shells out to ``linear-cli`` or hits Linear's GraphQL
API can land in a follow-up ticket once a runtime need surfaces.
"""

from __future__ import annotations

from typing import List, Optional

from worklane.trackers.protocol import ProjectTracker, Task, TaskComment, TaskStatus


class LinearTracker(ProjectTracker):
    name = "linear"

    def __init__(self) -> None:
        # Placeholder for an API key / workspace slug once a runtime
        # implementation is needed. Intentionally no os.environ read
        # today so a missing key is not a crash.
        pass

    def list_tasks(
        self,
        *,
        status: Optional[str] = None,
        label: Optional[str] = None,
        priority: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> List[Task]:
        return []

    def get_task(self, task_id: str) -> Optional[Task]:
        return None

    def create_task(
        self,
        *,
        title: str,
        description: str = "",
        status: str = TaskStatus.BACKLOG,
        priority: int = 3,
        labels: Optional[List[str]] = None,
        ext_id: Optional[str] = None,
    ) -> Task:
        raise NotImplementedError(
            "LinearTracker is a read-only stub. Use the Linear MCP server "
            "for interactive writes, or switch to TRADEOS_TRACKER=sqlite."
        )

    def update_status(self, task_id: str, status: str) -> Optional[Task]:
        raise NotImplementedError(
            "LinearTracker is a read-only stub. Use the Linear MCP server "
            "for interactive writes, or switch to TRADEOS_TRACKER=sqlite."
        )

    def add_comment(self, task_id: str, body: str, author: str = "") -> TaskComment:
        raise NotImplementedError(
            "LinearTracker is a read-only stub. Use the Linear MCP server "
            "for interactive writes, or switch to TRADEOS_TRACKER=sqlite."
        )

    def list_comments(self, task_id: str) -> List[TaskComment]:
        return []
