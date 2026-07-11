#!/usr/bin/env python3
"""Host-neutral WorkLane CLI — HTTP client + local portability (wl-13/wl-22).

Unlike :mod:`worklane.cli.task` (which imports the tracker and
reads/writes the SQLite store for day-to-day CRUD), this CLI speaks the
HTTP API on ``TASK_PORT`` for list/show/comment/status/label. That keeps
it the thing a *host* vendors for ticket access without a passthrough
(PROTOCOL.md §6): stdlib-only for the HTTP surface, composite ids
(``t-1095``, ``wl-13``) as the API resolves them.

Export/import (wl-22) are the exception: they are pure store operations
with no server routes yet, so those two subcommands import
:mod:`worklane.portability` and touch SQLite directly. They
never call the live HTTP API and import only CREATES rows.

Usage:
    wl create --title T --description D --product P [--priority N] [--label L ...] [--author A]
    wl list [--status S] [--label L] [--priority N] [--product P] [--limit N] [--json]
    wl show <ID> [--json]
    wl comment <ID> "body..." [--author A] [--stdin]
    wl status <ID> <STATUS>
    wl label <ID> [--add L ...] [--remove L ...]
    wl demo [--force] [--product SLUG]
    wl export --product <slug> [--out FILE]
    wl import <FILE> --product <slug>

Base URL:  WL_BASE_URL env var, default http://127.0.0.1:8799
Product:   --product flag, default WL_PRODUCT env var, else server default
Signing:   --author flag or WL_AGENT_ID env var (PROTOCOL.md §3.8) — required
           for `comment` (the API rejects unsigned writes with a 400)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

_PRIORITY_NAMES = {1: "urgent", 2: "high", 3: "normal", 4: "low"}
_STATUS_CHOICES = ("backlog", "in_progress", "in_review", "done", "canceled")


def _base_url() -> str:
    return (os.environ.get("WL_BASE_URL") or "http://127.0.0.1:8799").rstrip("/")


class ApiError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def _request(method: str, path: str, params: Optional[Dict[str, Any]] = None,
             body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    url = _base_url() + path
    if params:
        qs = {k: v for k, v in params.items() if v not in (None, "")}
        if qs:
            url = f"{url}?{urllib.parse.urlencode(qs)}"

    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            err = json.loads(raw).get("error", raw)
        except (json.JSONDecodeError, AttributeError):
            err = raw
        raise ApiError(exc.code, str(err)) from exc
    except urllib.error.URLError as exc:
        raise ApiError(-1, f"cannot reach WL at {_base_url()}: {exc.reason}") from exc

    if not payload.get("ok", False):
        raise ApiError(200, str(payload.get("error", "unknown API error")))
    return payload


def _fmt_priority(p: int) -> str:
    return f"P{p} ({_PRIORITY_NAMES.get(p, '?')})"


def _fmt_labels(labels: List[str]) -> str:
    return ", ".join(labels) if labels else "(none)"


def _resolve_author(cli_value: str) -> str:
    return (cli_value or os.environ.get("WL_AGENT_ID") or "").strip()


def cmd_create(args: argparse.Namespace) -> None:
    title = (args.title or "").strip()
    if not title:
        print("Error: --title is required", file=sys.stderr)
        sys.exit(1)
    description = args.description or ""
    if not description.strip():
        print(
            "Error: --description is required (PROTOCOL.md §5 intake — "
            "state the problem and expected outcome)",
            file=sys.stderr,
        )
        sys.exit(1)
    author = _resolve_author(args.author)
    if not author:
        print(
            "Error: ticket intake must be signed (PROTOCOL.md §3.8). "
            "Pass --author <agent-id> or set WL_AGENT_ID.",
            file=sys.stderr,
        )
        sys.exit(1)
    product = (args.product or "").strip()
    if not product:
        print("Error: --product is required (or set WL_PRODUCT)", file=sys.stderr)
        sys.exit(1)

    body: Dict[str, Any] = {
        "title": title,
        "description": description,
        "author": author,
        "surface": product,
    }
    if args.priority is not None:
        body["priority"] = args.priority
    if args.label:
        body["labels"] = args.label

    payload = _request("POST", "/api/admin/tasks", body=body)
    task = payload["task"]
    print(f"Created {task['id']}: {task['title']}")


def cmd_list(args: argparse.Namespace) -> None:
    payload = _request(
        "GET",
        "/api/admin/tasks",
        params={
            "status": args.status,
            "label": args.label,
            "priority": args.priority,
            "product": args.product,
            "limit": args.limit,
        },
    )
    tasks = payload.get("tasks", [])
    if args.json:
        print(json.dumps(tasks, indent=2))
        return
    if not tasks:
        print("No tasks found.")
        return
    for t in tasks:
        print(f"  {t['id']:<8} {_fmt_priority(t['priority']):<14} {t['status']:<13} {t['title']}")
        if args.verbose:
            print(f"           labels: {_fmt_labels(t.get('labels', []))}")


def cmd_show(args: argparse.Namespace) -> None:
    payload = _request("GET", f"/api/admin/tasks/{urllib.parse.quote(args.id, safe='')}")
    task = payload["task"]
    if args.json:
        print(json.dumps(task, indent=2))
        return

    print(f"{task['id']}: {task['title']}")
    print(f"  Status:   {task['status']}")
    print(f"  Priority: {_fmt_priority(task['priority'])}")
    print(f"  Labels:   {_fmt_labels(task.get('labels', []))}")
    print(f"  Created:  {task.get('created_at', '')}")
    print(f"  Updated:  {task.get('updated_at', '')}")
    if task.get("description"):
        print(f"\n--- Description ---\n{task['description']}")

    comments = task.get("comments", [])
    if comments:
        print(f"\n--- Comments ({len(comments)}) ---")
        for c in comments:
            author = c.get("author") or "anonymous"
            print(f"\n  [{c.get('created_at', '')}] {author}:")
            for line in (c.get("body") or "").splitlines():
                print(f"    {line}")


def cmd_comment(args: argparse.Namespace) -> None:
    body = args.body
    if args.stdin:
        body = sys.stdin.read()
    author = _resolve_author(args.author)
    if not author:
        print(
            "Error: comments must be signed (PROTOCOL.md §3.8). "
            "Pass --author <agent-id> or set WL_AGENT_ID.",
            file=sys.stderr,
        )
        sys.exit(1)
    payload = _request(
        "POST",
        f"/api/admin/tasks/{urllib.parse.quote(args.id, safe='')}/comments",
        body={"body": body, "author": author},
    )
    print(f"Comment added to {args.id} (comment #{payload['comment']['id']})")


def cmd_status(args: argparse.Namespace) -> None:
    payload = _request(
        "PATCH",
        f"/api/admin/tasks/{urllib.parse.quote(args.id, safe='')}",
        body={"status": args.new_status},
    )
    print(f"{payload['task']['id']} -> {payload['task']['status']}")


def cmd_label(args: argparse.Namespace) -> None:
    if not args.add and not args.remove:
        print("Error: at least one of --add or --remove is required", file=sys.stderr)
        sys.exit(1)
    payload = _request(
        "PATCH",
        f"/api/admin/tasks/{urllib.parse.quote(args.id, safe='')}/labels",
        body={"add": args.add or [], "remove": args.remove or []},
    )
    print(f"{payload['task']['id']} labels: {_fmt_labels(payload['task'].get('labels', []))}")


def cmd_demo(args: argparse.Namespace) -> None:
    """Bootstrap an isolated demo product store (local SQLite; no HTTP).

    Never touches real product DBs (tradeos / worklane / worklane /
    ops). Safe for fresh-clone quickstart — board tab appears on next server
    discover cycle.
    """
    from worklane import demo as demo_mod

    product = (args.product or demo_mod.DEFAULT_DEMO_SLUG).strip()
    try:
        report = demo_mod.bootstrap_demo(slug=product, force=bool(args.force))
    except demo_mod.DemoError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps(report, indent=2))
        return

    print(report["message"])
    print(f"  product: {report['slug']}")
    print(f"  db:      {report['db_path']}")
    print(f"  tasks:   {report['task_count']}")
    by_status = report.get("by_status") or {}
    if by_status:
        parts = [f"{k}={v}" for k, v in sorted(by_status.items())]
        print(f"  status:  {', '.join(parts)}")
    if not report.get("skipped"):
        print(
            "Open the board: http://127.0.0.1:8799/admin/tickets/"
            f"{report['slug']}?view=board"
        )
        print(
            "Claim the backlog ticket labeled 'Claim me' via MCP wl_claim "
            "or `wl status` + Owner comment."
        )


def cmd_export(args: argparse.Namespace) -> None:
    """Local store export (JSONL). Read-only; does not use the HTTP API."""
    from worklane import portability

    product = (args.product or "").strip()
    if not product:
        print("Error: --product is required for export", file=sys.stderr)
        sys.exit(1)
    try:
        lines = list(portability.export_product(product))
    except portability.PortabilityError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8") as fh:
            for line in lines:
                fh.write(line + "\n")
        print(f"Exported {len(lines)} ticket(s) to {args.out}", file=sys.stderr)
        return
    for line in lines:
        print(line)


def cmd_import(args: argparse.Namespace) -> None:
    """Local store import (JSONL). Creates only; never updates/deletes."""
    from worklane import portability

    product = (args.product or "").strip()
    if not product:
        print("Error: --product is required for import", file=sys.stderr)
        sys.exit(1)
    path = Path(args.file)
    if not path.is_file():
        print(f"Error: file not found: {path}", file=sys.stderr)
        sys.exit(1)
    try:
        with path.open("r", encoding="utf-8") as fh:
            report = portability.import_jsonl(fh, product)
    except portability.PortabilityError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(report.to_dict(), indent=2))
    print(
        f"Imported {len(report.created)} ticket(s); "
        f"skipped {len(report.collisions)} collision(s)",
        file=sys.stderr,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wl",
        description="Host-neutral WorkLane CLI (HTTP API client)",
    )
    sub = parser.add_subparsers(dest="command")

    p_create = sub.add_parser("create", help="Create a new task (signed intake)")
    p_create.add_argument("--title")
    p_create.add_argument("--description", default="")
    p_create.add_argument(
        "--product",
        default=os.environ.get("WL_PRODUCT", ""),
        help="Ticket surface/product slug (required; or set WL_PRODUCT)",
    )
    p_create.add_argument("--priority", type=int, choices=[1, 2, 3, 4])
    p_create.add_argument("--label", action="append", metavar="LABEL")
    p_create.add_argument("--author", default="")

    p_list = sub.add_parser("list", help="List tasks")
    p_list.add_argument("--status", choices=_STATUS_CHOICES)
    p_list.add_argument("--label")
    p_list.add_argument("--priority", type=int, choices=[1, 2, 3, 4])
    p_list.add_argument("--product", default=os.environ.get("WL_PRODUCT", ""))
    p_list.add_argument("--limit", type=int)
    p_list.add_argument("--json", action="store_true")
    p_list.add_argument("-v", "--verbose", action="store_true")

    p_show = sub.add_parser("show", help="Show task detail")
    p_show.add_argument("id")
    p_show.add_argument("--json", action="store_true")

    p_comment = sub.add_parser("comment", help="Add a signed comment")
    p_comment.add_argument("id")
    p_comment.add_argument("body", nargs="?", default="")
    p_comment.add_argument("--stdin", action="store_true")
    p_comment.add_argument("--author", default="")

    p_status = sub.add_parser("status", help="Update task status")
    p_status.add_argument("id")
    p_status.add_argument("new_status", choices=_STATUS_CHOICES)

    p_label = sub.add_parser("label", help="Add or remove labels")
    p_label.add_argument("id")
    p_label.add_argument("--add", action="append", metavar="LABEL")
    p_label.add_argument("--remove", action="append", metavar="LABEL")

    p_demo = sub.add_parser(
        "demo",
        help=(
            "Bootstrap an isolated demo product store with seeded tickets "
            "(local SQLite; never touches real product DBs)"
        ),
    )
    p_demo.add_argument(
        "--product",
        default=os.environ.get("WL_DEMO_PRODUCT", "demo"),
        help="Demo product slug (default: demo; protected real products refused)",
    )
    p_demo.add_argument(
        "--force",
        action="store_true",
        help="Wipe and re-seed the demo store only (never other products)",
    )
    p_demo.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable report",
    )

    p_export = sub.add_parser(
        "export",
        help="Export a product store to JSONL (local SQLite; read-only)",
    )
    p_export.add_argument(
        "--product",
        default=os.environ.get("WL_PRODUCT", ""),
        help="Product slug (required; or set WL_PRODUCT)",
    )
    p_export.add_argument("--out", metavar="FILE", help="Write JSONL to FILE (default: stdout)")

    p_import = sub.add_parser(
        "import",
        help="Import JSONL into a product store (local SQLite; create-only)",
    )
    p_import.add_argument("file", help="JSONL file to import")
    p_import.add_argument(
        "--product",
        default=os.environ.get("WL_PRODUCT", ""),
        help="Destination product slug (required; or set WL_PRODUCT)",
    )

    return parser


_COMMANDS = {
    "create": cmd_create,
    "list": cmd_list,
    "show": cmd_show,
    "comment": cmd_comment,
    "status": cmd_status,
    "label": cmd_label,
    "demo": cmd_demo,
    "export": cmd_export,
    "import": cmd_import,
}


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)
    try:
        _COMMANDS[args.command](args)
    except ApiError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
