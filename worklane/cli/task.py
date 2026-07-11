#!/usr/bin/env python3
"""WorkLane CLI — reads/writes the canonical WL SQLite DB.

No external dependencies. Uses worklane.trackers.sqlite.SQLiteTracker
against ``worklane/local/data/tradeos.db`` by default.

Usage:
    python -m worklane.cli.task list [--status STATUS] [--label LABEL] [--priority N] [--limit N] [--json]
    python -m worklane.cli.task show <ID>
    python -m worklane.cli.task create --title "..." [--description "..."] [--priority N] [--label L ...]
    python -m worklane.cli.task status <ID> <STATUS>
    python -m worklane.cli.task comment <ID> "body..."
    python -m worklane.cli.task counts

Host launchers typically wrap this (tradeOS: ``./tradeos task ...``).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Allow direct script invocation: ensure the host repo root is on sys.path
# so 'import worklane' resolves before we import from it.
_HOST_ROOT = Path(__file__).resolve().parents[2]
if str(_HOST_ROOT) not in sys.path:
    sys.path.insert(0, str(_HOST_ROOT))

from worklane.trackers.sqlite import SQLiteTracker
from worklane.trackers.protocol import TaskStatus

_PRIORITY_NAMES = {1: "urgent", 2: "high", 3: "normal", 4: "low"}
_STATUS_CHOICES = list(TaskStatus.ALL)


def _get_tracker() -> SQLiteTracker:
    return SQLiteTracker()


def _fmt_priority(p: int) -> str:
    return f"P{p} ({_PRIORITY_NAMES.get(p, '?')})"


def _fmt_labels(labels: list) -> str:
    return ", ".join(labels) if labels else "(none)"


def cmd_list(args: argparse.Namespace) -> None:
    tracker = _get_tracker()
    tasks = tracker.list_tasks(
        status=args.status,
        label=args.label,
        priority=args.priority,
        limit=args.limit,
    )
    if args.json:
        print(json.dumps([t.to_dict() for t in tasks], indent=2))
        return

    if not tasks:
        print("No tasks found.")
        return

    for t in sorted(tasks, key=lambda x: (x.priority, int(x.id))):
        labels = _fmt_labels(t.labels)
        print(f"  #{t.id:<5} {_fmt_priority(t.priority):<14} {t.status:<13} {t.title}")
        if args.verbose:
            print(f"         labels: {labels}")


def cmd_show(args: argparse.Namespace) -> None:
    tracker = _get_tracker()
    task = tracker.get_task(args.id)
    if not task:
        print(f"Task {args.id} not found.", file=sys.stderr)
        sys.exit(1)

    print(f"#{task.id}: {task.title}")
    print(f"  Status:   {task.status}")
    print(f"  Priority: {_fmt_priority(task.priority)}")
    print(f"  Labels:   {_fmt_labels(task.labels)}")
    if task.ext_id:
        print(f"  Ext ID:   {task.ext_id}")
    print(f"  Created:  {task.created_at}")
    print(f"  Updated:  {task.updated_at}")

    if task.description:
        print(f"\n--- Description ---\n{task.description}")

    comments = tracker.list_comments(task.id)
    if comments:
        print(f"\n--- Comments ({len(comments)}) ---")
        for c in comments:
            author = c.author or "anonymous"
            print(f"\n  [{c.created_at}] {author}:")
            for line in c.body.splitlines():
                print(f"    {line}")


def cmd_create(args: argparse.Namespace) -> None:
    tracker = _get_tracker()

    desc = args.description or ""
    if args.description_file:
        desc = Path(args.description_file).read_text(encoding="utf-8")

    # PROCESS.md §5 intake (wl-26): signed filer + real problem statement.
    author = (getattr(args, "author", "") or os.environ.get("WL_AGENT_ID") or "").strip()
    if not author:
        print(
            "Error: intake must be signed (PROCESS.md §3.8/§5). "
            "Pass --author <agent-id> or set WL_AGENT_ID.",
            file=sys.stderr,
        )
        sys.exit(1)
    if not desc.strip():
        print(
            "Error: --description (or --description-file) is required — "
            "state the problem and expected outcome (PROCESS.md §5 intake).",
            file=sys.stderr,
        )
        sys.exit(1)

    task = tracker.create_task(
        title=args.title,
        description=desc,
        priority=args.priority,
        labels=args.label or [],
    )
    tracker.add_comment(str(task.id), f"Intake: filed by {author}", author=author)
    print(f"Created #{task.id}: {task.title}")


def cmd_status(args: argparse.Namespace) -> None:
    tracker = _get_tracker()
    task = tracker.update_status(args.id, args.new_status)
    if not task:
        print(f"Task {args.id} not found.", file=sys.stderr)
        sys.exit(1)
    print(f"#{task.id} → {task.status}")


def cmd_comment(args: argparse.Namespace) -> None:
    tracker = _get_tracker()
    body = args.body
    if body.startswith("@"):
        body = Path(body[1:]).read_text(encoding="utf-8")
    elif args.stdin:
        body = sys.stdin.read()
    # PROCESS.md §3.8 — every comment is signed. --author wins; WL_AGENT_ID
    # lets a lane set its identity once in its environment.
    author = (args.author or os.environ.get("WL_AGENT_ID") or "").strip()
    if not author:
        print(
            "Error: comments must be signed (PROCESS.md §3.8). "
            "Pass --author <agent-id> or set WL_AGENT_ID.",
            file=sys.stderr,
        )
        sys.exit(1)
    comment = tracker.add_comment(args.id, body, author=author)
    print(f"Comment added to #{args.id} (comment #{comment.id})")


def cmd_update(args: argparse.Namespace) -> None:
    description = args.description
    if args.description_file:
        description = Path(args.description_file).read_text(encoding="utf-8")
    if args.title is None and description is None and args.priority is None:
        print(
            "Error: at least one of --title, --description, "
            "--description-file, --priority is required",
            file=sys.stderr,
        )
        sys.exit(1)
    tracker = _get_tracker()
    task = tracker.update_task(
        args.id,
        title=args.title,
        description=description,
        priority=args.priority,
        actor=args.actor or "",
    )
    if not task:
        print(f"Task {args.id} not found.", file=sys.stderr)
        sys.exit(1)
    print(f"Updated #{task.id}: {task.title}")


def cmd_label(args: argparse.Namespace) -> None:
    if not args.add and not args.remove:
        print(
            "Error: at least one of --add or --remove is required",
            file=sys.stderr,
        )
        sys.exit(1)
    tracker = _get_tracker()
    task = tracker.update_labels(
        args.id,
        add=args.add or [],
        remove=args.remove or [],
        actor=args.actor or "",
    )
    if not task:
        print(f"Task {args.id} not found.", file=sys.stderr)
        sys.exit(1)
    print(f"#{task.id} labels: {_fmt_labels(task.labels)}")


def cmd_counts(args: argparse.Namespace) -> None:
    tracker = _get_tracker()
    all_tasks = tracker.list_tasks()
    counts: dict[str, int] = {}
    for t in all_tasks:
        counts[t.status] = counts.get(t.status, 0) + 1
    total = len(all_tasks)
    print(f"Total: {total}")
    for s in TaskStatus.ALL:
        c = counts.get(s, 0)
        if c > 0:
            print(f"  {s:<13} {c}")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="task",
        description="Local task CLI — reads/writes tradeos.db directly",
    )
    sub = parser.add_subparsers(dest="command")

    # list
    p_list = sub.add_parser("list", help="List tasks")
    p_list.add_argument("--status", choices=_STATUS_CHOICES)
    p_list.add_argument("--label")
    p_list.add_argument("--priority", type=int, choices=[1, 2, 3, 4])
    p_list.add_argument("--limit", type=int)
    p_list.add_argument("--json", action="store_true")
    p_list.add_argument("-v", "--verbose", action="store_true")

    # show
    p_show = sub.add_parser("show", help="Show task detail")
    p_show.add_argument("id")

    # create
    p_create = sub.add_parser("create", help="Create a task")
    p_create.add_argument("--title", required=True)
    p_create.add_argument("--description", default="")
    p_create.add_argument("--description-file", default=None)
    p_create.add_argument("--priority", type=int, default=3)
    p_create.add_argument("--label", action="append")
    p_create.add_argument("--author", default="")

    # status
    p_status = sub.add_parser("status", help="Update task status")
    p_status.add_argument("id")
    p_status.add_argument("new_status", choices=_STATUS_CHOICES)

    # comment
    p_comment = sub.add_parser("comment", help="Add a comment")
    p_comment.add_argument("id")
    p_comment.add_argument("body", nargs="?", default="")
    p_comment.add_argument("--stdin", action="store_true")
    p_comment.add_argument("--author", default="")

    # counts
    sub.add_parser("counts", help="Show status counts")

    # update
    p_update = sub.add_parser("update", help="Edit task fields (title/description/priority)")
    p_update.add_argument("id", help="Task ID")
    p_update.add_argument("--title", default=None, help="New title")
    p_update.add_argument("--description", default=None, help="New description (inline)")
    p_update.add_argument("--description-file", default=None, dest="description_file",
                          help="Path to a file whose contents replace the description")
    p_update.add_argument("--priority", type=int, choices=[1, 2, 3, 4], default=None,
                          help="New priority (1=urgent … 4=low)")
    p_update.add_argument("--actor", default="", help="Actor name for audit trail")

    # label
    p_label = sub.add_parser("label", help="Add or remove labels on a task")
    p_label.add_argument("id", help="Task ID")
    p_label.add_argument("--add", action="append", metavar="LABEL",
                         help="Label to add (repeatable)")
    p_label.add_argument("--remove", action="append", metavar="LABEL",
                         help="Label to remove (repeatable)")
    p_label.add_argument("--actor", default="", help="Actor name for audit trail")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    {
        "list": cmd_list,
        "show": cmd_show,
        "create": cmd_create,
        "status": cmd_status,
        "comment": cmd_comment,
        "counts": cmd_counts,
        "update": cmd_update,
        "label": cmd_label,
    }[args.command](args)


if __name__ == "__main__":
    main()
