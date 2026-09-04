#!/usr/bin/env python3
"""Dry-run (and, once approved, apply) relations backfill for wl-20.

Parses existing prose ``Depends on #N`` / ``Blocked by #N`` declarations
and ``parent:`` / ``slice-of:`` / numeric ``epic:`` labels into
``task_relations`` rows via :mod:`worklane.relations`.

Usage:
    python3 scripts/backfill_relations.py --db PATH [--apply] [--report PATH]

Default mode is dry-run: no writes, just a report. --apply inserts rows
inside the relations helpers (cycle-safe, idempotent). Never run --apply
against a live product store without founder sign-off on the dry-run
report (wl-20 / wl-7 precedent).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

# Allow running as scripts/… without an editable install.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from worklane.relations import apply_backfill  # noqa: E402


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Backfill task_relations from prose Depends-on / parent labels (wl-20)"
    )
    parser.add_argument(
        "--db",
        required=True,
        type=Path,
        help="Path to a product SQLite store (fixture or product .db)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write relations (default is dry-run only)",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="Optional path to write the JSON report",
    )
    args = parser.parse_args(argv)

    if not args.db.exists():
        print(f"error: db not found: {args.db}", file=sys.stderr)
        return 2

    report = apply_backfill(args.db, dry_run=not args.apply)
    payload = report.to_dict()
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"

    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text, encoding="utf-8")
        print(f"wrote report → {args.report}", file=sys.stderr)

    # Always print a short summary; full JSON to stdout when no --report.
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(
        f"[{mode}] planned={payload['planned_count']} "
        f"applied={payload['applied_count']} "
        f"skip_existing={payload['skipped_existing_count']} "
        f"skip_missing={payload['skipped_missing_count']} "
        f"skip_cycle={payload['skipped_cycle_count']}",
        file=sys.stderr,
    )
    if args.report is None:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
