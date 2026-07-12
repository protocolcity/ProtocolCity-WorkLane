"""Dev-mode work queue (SEO-180).

Semi-autonomous ticket dispatch, consumed by ``worklane/task_server.py``
(the ``/api/dev/queue/*`` routes) and ``worklane/mcp/handlers.py``
(``wl_ready`` blocker filtering) — see this package's TRUTH.md. The queue reads
tasks through the :class:`worklane.trackers.protocol.ProjectTracker`
abstraction (default: SQLiteTracker), prioritizes them, drops anything
whose blockers aren't Done, and groups overlapping work into batches the
developer can approve in one click.

Public surface:

* :class:`WorkQueue` — load + prioritize + filter ready tasks.
* :func:`find_orphans` — in-progress tickets left over from a previous
  session, surfaced on dev dashboard startup.
* :func:`group_by_file_conflict` — bucket tickets that touch the same
  files into one terminal so two agents don't stomp on each other.
* :func:`build_dispatch_prompt` — render the Claude Code prompt for an
  approved batch (e.g. ``"work SEO-164, SEO-180"``).
* :func:`run_shutdown` — graceful close-out: scan git for SEO-XXX
  commits, write a closeout comment per in-progress ticket, and
  optionally transition status. Safe to call by hand or wire to a
  shutdown button on the dashboard.

Nothing in this module hits Linear directly — the ProjectTracker
abstraction owns persistence. Linear is retired; legacy SEO-XXX ext_ids
are preserved for historical reference only (see this package's TRUTH.md).
"""

from __future__ import annotations

from worklane.devqueue.conflicts import (
    extract_file_refs,
    group_by_file_conflict,
)
from worklane.devqueue.queue import (
    Batch,
    BlockedTask,
    BlockerInfo,
    WorkQueue,
    build_dispatch_prompt,
    find_orphans,
    parse_blockers,
)
from worklane.devqueue.shutdown import (
    ShutdownReport,
    ShutdownTicketResult,
    run_shutdown,
)

__all__ = [
    "Batch",
    "BlockedTask",
    "BlockerInfo",
    "ShutdownReport",
    "ShutdownTicketResult",
    "WorkQueue",
    "build_dispatch_prompt",
    "extract_file_refs",
    "find_orphans",
    "group_by_file_conflict",
    "parse_blockers",
    "run_shutdown",
]
