"""Graceful shutdown protocol for in-progress tickets (SEO-180).

When a dev session ends, the work queue should not leave any ticket
stranded in ``in_progress`` with no context. This module walks every
in-progress task, asks ``git log`` whether anything actually landed for
it, then writes a closeout comment via the :class:`ProjectTracker`
adapter and (optionally) transitions the task's status.

We deliberately do **not** auto-fire on `atexit` — the dev launcher
runs uvicorn with ``--reload``, which restarts the process every time a
file changes. An atexit hook would spam comments on every reload. The
intended trigger is the "Run shutdown protocol" button on the dev
dashboard or the ``python -m worklane.devqueue shutdown`` CLI in
:mod:`worklane.devqueue.__main__` (next iteration).

The function returns a :class:`ShutdownReport` so callers can render
the result without re-running the protocol — the dashboard uses this to
show a per-ticket summary card after the developer clicks the button.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from worklane.trackers.protocol import ProjectTracker, Task, TaskStatus


def _repo_root() -> Path:
    """Host repo root (the repo that contains ``worklane/``).

    Derived from this module's own location so devqueue stays host-agnostic.
    """
    return Path(__file__).resolve().parents[2]


_CLOSEOUT_AUTHOR = "devqueue"


# ── result types ─────────────────────────────────────────────────────────

@dataclass
class ShutdownTicketResult:
    task_id: str
    title: str
    commits: List[str]
    new_status: str
    comment_body: str
    applied: bool

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "title": self.title,
            "commits": list(self.commits),
            "new_status": self.new_status,
            "comment_body": self.comment_body,
            "applied": self.applied,
        }


@dataclass
class ShutdownReport:
    results: List[ShutdownTicketResult] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    applied: bool = False

    def to_dict(self) -> dict:
        return {
            "results": [r.to_dict() for r in self.results],
            "skipped": list(self.skipped),
            "applied": self.applied,
        }


# ── git helpers ──────────────────────────────────────────────────────────

def _git_log_for_ticket(
    ticket_ext_id: str, *, repo: Optional[Path] = None
) -> List[str]:
    """Return short commit lines (``"<hash> <subject>"``) referencing the ticket.

    We grep ``git log`` server-side so this stays fast on long-lived
    repos. Returns an empty list if git is unavailable or the call
    fails — the caller treats that the same as "no commits found".
    """
    if not ticket_ext_id:
        return []
    repo_path = repo or _repo_root()
    try:
        proc = subprocess.run(
            [
                "git",
                "log",
                f"--grep={ticket_ext_id}",
                "--pretty=format:%h %s",
                "--no-merges",
            ],
            cwd=str(repo_path),
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


# ── closeout protocol ────────────────────────────────────────────────────

def _resolve_ticket_id(task: Task) -> str:
    """The identifier we feed git/grep — prefer the external Linear ID."""
    return task.ext_id or task.id


def _build_comment(task: Task, commits: List[str]) -> str:
    """Render the markdown closeout comment for a single ticket."""
    header = "**devqueue shutdown closeout**"
    if commits:
        body_lines = [
            header,
            "",
            f"Found {len(commits)} commit(s) referencing this ticket since it was opened:",
            "",
        ]
        body_lines.extend(f"- `{line}`" for line in commits[:20])
        if len(commits) > 20:
            body_lines.append(f"- … {len(commits) - 20} more")
        body_lines.extend(
            [
                "",
                "Status moved to **In Review** — please confirm the diff and "
                "transition to Done when verified.",
            ]
        )
        return "\n".join(body_lines)
    return "\n".join(
        [
            header,
            "",
            "No commits referencing this ticket were found in `git log`.",
            "",
            "Marked as still in progress with no code landed yet — resume "
            "from where you left off, or move back to Backlog if the work "
            "should be deferred.",
        ]
    )


def run_shutdown(
    tracker: ProjectTracker,
    *,
    apply: bool = False,
    repo: Optional[Path] = None,
) -> ShutdownReport:
    """Execute the close-out protocol against ``tracker``.

    By default this is a dry run: it computes what would happen and
    returns a :class:`ShutdownReport` describing the proposed comments
    and status transitions. Pass ``apply=True`` to actually write the
    comments and update statuses via the tracker.

    Behavior per in-progress task:

    * Scan ``git log`` for commits whose subject contains the ticket
      ID (e.g. ``SEO-180``).
    * If commits exist → render a "found N commits" comment and
      propose moving the ticket to ``in_review``.
    * If no commits → render a "no code landed yet" comment and
      leave the status as ``in_progress`` so the developer can decide
      whether to resume or demote.

    Tickets that lack any usable identifier are skipped (their IDs are
    surfaced in :attr:`ShutdownReport.skipped` so the dashboard can
    flag them).
    """
    report = ShutdownReport(applied=apply)
    in_progress = tracker.list_tasks(status=TaskStatus.IN_PROGRESS, limit=200)
    for task in in_progress:
        ext = _resolve_ticket_id(task)
        if not ext:
            report.skipped.append(task.id or "<unknown>")
            continue
        commits = _git_log_for_ticket(ext, repo=repo)
        new_status = TaskStatus.IN_REVIEW if commits else TaskStatus.IN_PROGRESS
        comment = _build_comment(task, commits)

        if apply:
            try:
                tracker.add_comment(ext, comment, author=_CLOSEOUT_AUTHOR)
            except KeyError:
                report.skipped.append(ext)
                continue
            if new_status != task.status:
                tracker.update_status(ext, new_status, actor=_CLOSEOUT_AUTHOR)

        report.results.append(
            ShutdownTicketResult(
                task_id=ext,
                title=task.title,
                commits=commits,
                new_status=new_status,
                comment_body=comment,
                applied=apply,
            )
        )
    return report


__all__ = [
    "ShutdownReport",
    "ShutdownTicketResult",
    "run_shutdown",
]
