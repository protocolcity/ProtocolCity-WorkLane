"""wl-23: Done-ticket archival — keep the hot store small.

The hot task DB carries every done/canceled ticket in the path of every board
render and pulse scan. This module moves *cold* terminal tickets (done or
canceled, untouched for N days) out of the hot DB into a sibling
``<stem>_archive.db`` that shares the exact same schema.

This is **archival, not deletion** (PROTOCOL.md §Prohibited-adjacent): the rows
are copied with their internal ``id`` preserved, so comments and relations stay
consistent and a ticket can be moved back with :func:`restore_archived_tickets`.
The archive DB is a plain WL store — read it with an ordinary
``SQLiteTracker(db_path=archive_path)``.

Slice 1: pure engine + counts. Slice 2 (task_server): Settings compact action,
ticket-detail read-through to archived ids, board/scope_counts stay hot-only.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Sequence, Union

from worklane.trackers.sqlite import SQLiteTracker
from worklane.trackers.protocol import TaskStatus

# Terminal statuses eligible for archival. In_progress / in_review / backlog are
# never archived — they are live work regardless of age.
TERMINAL_STATUSES: Sequence[str] = (TaskStatus.DONE, TaskStatus.CANCELED)

DEFAULT_ARCHIVE_AGE_DAYS = 90

# Columns copied verbatim so a restore is byte-for-byte (id preserved).
_TASK_COLUMNS = (
    "id",
    "ext_id",
    "title",
    "description",
    "status",
    "priority",
    "labels",
    "created_at",
    "updated_at",
)
_COMMENT_COLUMNS = ("id", "task_id", "body", "author", "created_at")
_RELATION_COLUMNS = ("id", "from_id", "to_id", "relation_type", "created_at")


@dataclass
class ArchiveResult:
    """Counts of what a single archive/restore pass moved."""

    tickets: int
    comments: int
    relations: int
    source_path: str
    archive_path: str


def archive_db_path_for(db_path: Path) -> Path:
    """Sibling archive store for ``db_path`` (``tradeos.db`` -> ``tradeos_archive.db``)."""
    db_path = Path(db_path)
    return db_path.with_name(f"{db_path.stem}_archive{db_path.suffix}")


def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    """Parse a WL timestamp to an aware UTC datetime.

    WL writes two shapes: app code uses ``datetime.isoformat()`` (``+00:00``),
    the SQL column DEFAULT uses ``strftime(...'Z')``. Both must compare
    correctly, so we normalise rather than lexicographically compare (Python 3.9
    ``fromisoformat`` rejects a trailing ``Z``).
    """
    if not ts:
        return None
    raw = ts.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _ensure_archive_schema(archive_path: Path) -> None:
    """Create the archive DB with the identical live schema.

    Reuses ``SQLiteTracker._connect`` so the archive DDL never drifts from the
    hot DDL (the wl-9 dual-source lesson) — a single connect runs the schema.
    """
    with SQLiteTracker(db_path=archive_path)._connect():
        pass


def _copy_and_delete(
    src: sqlite3.Connection,
    dst: sqlite3.Connection,
    ids: List[int],
) -> ArchiveResult:
    """Copy the given task ids (+ their comments/relations) src->dst, then
    delete them from src. Caller owns the transactions."""
    # The archive is a cold denormalised store: a relation may point at a
    # counterpart still live in the hot DB, so FK enforcement is off for the
    # insert side. INSERT OR REPLACE keeps a re-run after a partial failure idempotent.
    dst.execute("PRAGMA foreign_keys=OFF")

    marks = ",".join("?" * len(ids))

    task_rows = src.execute(
        f"SELECT {', '.join(_TASK_COLUMNS)} FROM tasks WHERE id IN ({marks})",
        ids,
    ).fetchall()
    comment_rows = src.execute(
        f"SELECT {', '.join(_COMMENT_COLUMNS)} FROM task_comments "
        f"WHERE task_id IN ({marks})",
        ids,
    ).fetchall()
    relation_rows = src.execute(
        f"SELECT {', '.join(_RELATION_COLUMNS)} FROM task_relations "
        f"WHERE from_id IN ({marks}) OR to_id IN ({marks})",
        ids + ids,
    ).fetchall()

    _insert_rows(dst, "tasks", _TASK_COLUMNS, task_rows)
    _insert_rows(dst, "task_comments", _COMMENT_COLUMNS, comment_rows)
    _insert_rows(dst, "task_relations", _RELATION_COLUMNS, relation_rows)

    # Delete explicitly (do not lean on cascade / PRAGMA state) in child->parent
    # order so nothing is orphaned in the hot DB.
    src.execute(
        f"DELETE FROM task_comments WHERE task_id IN ({marks})", ids
    )
    src.execute(
        f"DELETE FROM task_relations WHERE from_id IN ({marks}) OR to_id IN ({marks})",
        ids + ids,
    )
    src.execute(f"DELETE FROM tasks WHERE id IN ({marks})", ids)

    return ArchiveResult(
        tickets=len(task_rows),
        comments=len(comment_rows),
        relations=len(relation_rows),
        source_path="",
        archive_path="",
    )


def _insert_rows(
    conn: sqlite3.Connection,
    table: str,
    columns: Sequence[str],
    rows: Sequence[sqlite3.Row],
) -> None:
    if not rows:
        return
    marks = ",".join("?" * len(columns))
    conn.executemany(
        f"INSERT OR REPLACE INTO {table} ({', '.join(columns)}) VALUES ({marks})",
        [tuple(r[c] for c in columns) for r in rows],
    )


def archive_cold_tickets(
    db_path: Path,
    *,
    archive_path: Optional[Path] = None,
    older_than_days: int = DEFAULT_ARCHIVE_AGE_DAYS,
    now: Optional[datetime] = None,
    statuses: Sequence[str] = TERMINAL_STATUSES,
) -> ArchiveResult:
    """Move cold terminal tickets from ``db_path`` into the archive store.

    A ticket is cold when its ``status`` is terminal (done/canceled) and its
    ``updated_at`` is older than ``older_than_days``. Reversible via
    :func:`restore_archived_tickets`.
    """
    db_path = Path(db_path)
    archive_path = Path(archive_path) if archive_path else archive_db_path_for(db_path)
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=older_than_days)

    if not db_path.exists():
        return ArchiveResult(0, 0, 0, str(db_path), str(archive_path))

    _ensure_archive_schema(archive_path)

    src = sqlite3.connect(str(db_path), timeout=5.0)
    src.row_factory = sqlite3.Row
    dst = sqlite3.connect(str(archive_path), timeout=5.0)
    dst.row_factory = sqlite3.Row
    try:
        marks = ",".join("?" * len(statuses))
        candidates = src.execute(
            f"SELECT id, updated_at FROM tasks WHERE status IN ({marks})",
            tuple(statuses),
        ).fetchall()
        ids = [
            int(r["id"])
            for r in candidates
            if (_parse_iso(r["updated_at"]) or now) < cutoff
        ]
        if not ids:
            return ArchiveResult(0, 0, 0, str(db_path), str(archive_path))

        result = _copy_and_delete(src, dst, ids)
        dst.commit()
        src.commit()
    finally:
        src.close()
        dst.close()

    result.source_path = str(db_path)
    result.archive_path = str(archive_path)
    return result


def restore_archived_tickets(
    db_path: Path,
    task_ids: Sequence[Union[str, int]],
    *,
    archive_path: Optional[Path] = None,
) -> ArchiveResult:
    """Move archived tickets (by internal id) back into the hot store.

    Keys on the store's internal ``tasks.id`` — not ``ext_id`` — because real
    tickets can have NULL ``ext_id``. Same copy path as archive, reversed.
    """
    db_path = Path(db_path)
    archive_path = Path(archive_path) if archive_path else archive_db_path_for(db_path)

    if not archive_path.exists():
        return ArchiveResult(0, 0, 0, str(archive_path), str(db_path))
    if not task_ids:
        return ArchiveResult(0, 0, 0, str(archive_path), str(db_path))

    # Hot DB must exist with schema for the restore target.
    with SQLiteTracker(db_path=db_path)._connect():
        pass

    # Normalize to ints; skip unparseable so a bad id never aborts the batch.
    want: List[int] = []
    for raw in task_ids:
        try:
            want.append(int(str(raw).strip()))
        except (TypeError, ValueError):
            continue
    if not want:
        return ArchiveResult(0, 0, 0, str(archive_path), str(db_path))

    src = sqlite3.connect(str(archive_path), timeout=5.0)
    src.row_factory = sqlite3.Row
    dst = sqlite3.connect(str(db_path), timeout=5.0)
    dst.row_factory = sqlite3.Row
    try:
        marks = ",".join("?" * len(want))
        rows = src.execute(
            f"SELECT id FROM tasks WHERE id IN ({marks})",
            tuple(want),
        ).fetchall()
        ids = [int(r["id"]) for r in rows]
        if not ids:
            return ArchiveResult(0, 0, 0, str(archive_path), str(db_path))

        result = _copy_and_delete(src, dst, ids)
        dst.commit()
        src.commit()
    finally:
        src.close()
        dst.close()

    result.source_path = str(archive_path)
    result.archive_path = str(db_path)
    return result


def archive_counts(archive_path: Path) -> int:
    """How many tickets currently sit in the archive store (0 if none/missing)."""
    archive_path = Path(archive_path)
    if not archive_path.exists():
        return 0
    conn = sqlite3.connect(str(archive_path), timeout=2.0)
    try:
        row = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()
        return int(row[0]) if row else 0
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


__all__ = [
    "ArchiveResult",
    "TERMINAL_STATUSES",
    "DEFAULT_ARCHIVE_AGE_DAYS",
    "archive_db_path_for",
    "archive_cold_tickets",
    "restore_archived_tickets",
    "archive_counts",
]
