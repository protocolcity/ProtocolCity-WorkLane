"""Structured task relations + ready/explain (wl-20).

First-class relation types (beads-inspired) live in ``task_relations``:

* ``blocks``          — from_id blocks to_id (to_id waits on from_id)
* ``parent-child``    — from_id is parent of to_id
* ``related``         — informational link (no cycle / ready impact)
* ``discovered-from`` — provenance (no cycle / ready impact)

Schema is additive (new table only). Cycle detection runs on ``blocks`` +
``parent-child`` edges at create time. The prose blocker parser
(:func:`worklane.trackers.sqlite._parse_blockers`) remains the
intake shim; :func:`plan_backfill` can materialize those declarations as
rows (dry-run by default — never apply against a live store without
founder review).
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

# Public relation vocabulary.
RELATION_TYPES: Tuple[str, ...] = (
    "blocks",
    "parent-child",
    "related",
    "discovered-from",
)
# Directed edges that participate in cycle detection.
_CYCLE_TYPES: Set[str] = {"blocks", "parent-child"}
# Directed edges that block readiness of the destination until the source is done.
_BLOCKING_TYPES: Set[str] = {"blocks"}

_DONE_STATUSES: Set[str] = {"done", "canceled"}

_PARENT_LABEL_RE = re.compile(
    r"^(?:parent|slice-of):#?(?P<id>\d+)$",
    re.IGNORECASE,
)
_EPIC_LABEL_RE = re.compile(
    r"^epic:#?(?P<id>\d+)$",
    re.IGNORECASE,
)


class RelationError(ValueError):
    """Invalid relation type, missing task, duplicate, or cycle."""


@dataclass
class Relation:
    """One directed edge in ``task_relations``."""

    id: str
    from_id: str
    to_id: str
    relation_type: str
    created_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "from_id": self.from_id,
            "to_id": self.to_id,
            "relation_type": self.relation_type,
            "created_at": self.created_at,
        }


@dataclass
class ReadyExplain:
    """Per-ticket readiness against a relation graph."""

    task_id: str
    ready: bool
    blocked_by: List[str] = field(default_factory=list)
    status: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "ready": self.ready,
            "blocked_by": list(self.blocked_by),
            "status": self.status,
        }


@dataclass
class BackfillPlanItem:
    """One proposed relation from prose / label markers."""

    from_id: str
    to_id: str
    relation_type: str
    source: str  # e.g. "depends-on", "label:parent", "label:epic"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "from_id": self.from_id,
            "to_id": self.to_id,
            "relation_type": self.relation_type,
            "source": self.source,
        }


@dataclass
class BackfillReport:
    """Result of planning or applying a relations backfill."""

    planned: List[BackfillPlanItem] = field(default_factory=list)
    applied: List[BackfillPlanItem] = field(default_factory=list)
    skipped_existing: List[BackfillPlanItem] = field(default_factory=list)
    skipped_missing: List[BackfillPlanItem] = field(default_factory=list)
    skipped_cycle: List[BackfillPlanItem] = field(default_factory=list)
    dry_run: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "planned_count": len(self.planned),
            "applied_count": len(self.applied),
            "skipped_existing_count": len(self.skipped_existing),
            "skipped_missing_count": len(self.skipped_missing),
            "skipped_cycle_count": len(self.skipped_cycle),
            "planned": [p.to_dict() for p in self.planned],
            "applied": [p.to_dict() for p in self.applied],
            "skipped_existing": [p.to_dict() for p in self.skipped_existing],
            "skipped_missing": [p.to_dict() for p in self.skipped_missing],
            "skipped_cycle": [p.to_dict() for p in self.skipped_cycle],
        }


def _norm_id(task_id: object) -> str:
    return str(task_id).strip()


def _validate_type(relation_type: str) -> str:
    rt = (relation_type or "").strip().lower()
    if rt not in RELATION_TYPES:
        raise RelationError(
            f"unknown relation_type {relation_type!r}; "
            f"expected one of {', '.join(RELATION_TYPES)}"
        )
    return rt


def ensure_relations_schema(conn: sqlite3.Connection) -> None:
    """Idempotent additive DDL for ``task_relations``."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS task_relations (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            from_id       INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            to_id         INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            relation_type TEXT    NOT NULL,
            created_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            UNIQUE(from_id, to_id, relation_type)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_task_relations_from "
        "ON task_relations(from_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_task_relations_to "
        "ON task_relations(to_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_task_relations_type "
        "ON task_relations(relation_type)"
    )


def connect(db_path: Path) -> sqlite3.Connection:
    """Open a short-lived connection with FK + schema ensured."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    ensure_relations_schema(conn)
    return conn


def _row_to_relation(row: sqlite3.Row) -> Relation:
    return Relation(
        id=str(row["id"]),
        from_id=str(row["from_id"]),
        to_id=str(row["to_id"]),
        relation_type=str(row["relation_type"]),
        created_at=row["created_at"] or "",
    )


def _task_exists(conn: sqlite3.Connection, task_id: str) -> bool:
    try:
        pk = int(task_id)
    except (TypeError, ValueError):
        return False
    row = conn.execute("SELECT 1 FROM tasks WHERE id = ?", (pk,)).fetchone()
    return row is not None


def _load_cycle_edges(conn: sqlite3.Connection) -> Dict[str, Set[str]]:
    """Adjacency list for blocks + parent-child (from → to)."""
    adj: Dict[str, Set[str]] = {}
    rows = conn.execute(
        "SELECT from_id, to_id, relation_type FROM task_relations"
    ).fetchall()
    for row in rows:
        if str(row["relation_type"]) not in _CYCLE_TYPES:
            continue
        src = str(row["from_id"])
        dst = str(row["to_id"])
        adj.setdefault(src, set()).add(dst)
    return adj


def would_create_cycle(
    edges: Dict[str, Set[str]], from_id: str, to_id: str
) -> bool:
    """True if adding directed edge from_id → to_id closes a cycle.

    Pure graph helper: ``edges`` is adjacency (source → set of targets).
    """
    if from_id == to_id:
        return True
    # Can we already reach from_id starting at to_id? Then from→to loops.
    stack = [to_id]
    seen: Set[str] = set()
    while stack:
        node = stack.pop()
        if node == from_id:
            return True
        if node in seen:
            continue
        seen.add(node)
        for nxt in edges.get(node, ()):
            if nxt not in seen:
                stack.append(nxt)
    return False


def create_relation(
    db_path: Path,
    from_id: str,
    to_id: str,
    relation_type: str,
) -> Relation:
    """Insert a relation; reject unknown types, missing tasks, dups, cycles."""
    rt = _validate_type(relation_type)
    src = _norm_id(from_id)
    dst = _norm_id(to_id)
    if not src or not dst:
        raise RelationError("from_id and to_id are required")
    if src == dst and rt in _CYCLE_TYPES:
        raise RelationError(f"self-edge not allowed for relation_type {rt!r}")

    conn = connect(db_path)
    try:
        if not _task_exists(conn, src):
            raise RelationError(f"from_id {src!r} not found")
        if not _task_exists(conn, dst):
            raise RelationError(f"to_id {dst!r} not found")

        if rt in _CYCLE_TYPES:
            edges = _load_cycle_edges(conn)
            if would_create_cycle(edges, src, dst):
                raise RelationError(
                    f"cycle detected: adding {src} -[{rt}]-> {dst} "
                    f"would close a cycle"
                )

        now = datetime.now(timezone.utc).isoformat()
        try:
            with conn:
                cur = conn.execute(
                    """
                    INSERT INTO task_relations (from_id, to_id, relation_type, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (int(src), int(dst), rt, now),
                )
                rid = cur.lastrowid
        except sqlite3.IntegrityError as exc:
            raise RelationError(
                f"relation already exists: {src} -[{rt}]-> {dst}"
            ) from exc

        row = conn.execute(
            "SELECT * FROM task_relations WHERE id = ?", (rid,)
        ).fetchone()
        assert row is not None
        return _row_to_relation(row)
    finally:
        conn.close()


def list_relations(
    db_path: Path,
    *,
    task_id: Optional[str] = None,
    relation_type: Optional[str] = None,
) -> List[Relation]:
    """List relations; optionally filter by endpoint and/or type."""
    conn = connect(db_path)
    try:
        sql = "SELECT * FROM task_relations"
        clauses: List[str] = []
        params: List[object] = []
        if task_id is not None:
            tid = _norm_id(task_id)
            clauses.append("(from_id = ? OR to_id = ?)")
            params.extend([int(tid), int(tid)])
        if relation_type is not None:
            rt = _validate_type(relation_type)
            clauses.append("relation_type = ?")
            params.append(rt)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id ASC"
        rows = conn.execute(sql, params).fetchall()
        return [_row_to_relation(r) for r in rows]
    finally:
        conn.close()


def get_relation(db_path: Path, relation_id: str) -> Optional[Relation]:
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM task_relations WHERE id = ?",
            (int(relation_id),),
        ).fetchone()
        return _row_to_relation(row) if row else None
    except (TypeError, ValueError):
        return None
    finally:
        conn.close()


def delete_relation(db_path: Path, relation_id: str) -> bool:
    """Delete by primary key. Returns True if a row was removed."""
    conn = connect(db_path)
    try:
        with conn:
            cur = conn.execute(
                "DELETE FROM task_relations WHERE id = ?",
                (int(relation_id),),
            )
        return cur.rowcount > 0
    except (TypeError, ValueError) as exc:
        raise RelationError(f"invalid relation id {relation_id!r}") from exc
    finally:
        conn.close()


def delete_relation_edge(
    db_path: Path,
    from_id: str,
    to_id: str,
    relation_type: str,
) -> bool:
    """Delete by (from, to, type). Returns True if a row was removed."""
    rt = _validate_type(relation_type)
    conn = connect(db_path)
    try:
        with conn:
            cur = conn.execute(
                """
                DELETE FROM task_relations
                 WHERE from_id = ? AND to_id = ? AND relation_type = ?
                """,
                (int(_norm_id(from_id)), int(_norm_id(to_id)), rt),
            )
        return cur.rowcount > 0
    finally:
        conn.close()


def explain_ready(
    task_ids: Sequence[str],
    status_by_id: Dict[str, str],
    relations: Sequence[Relation],
    *,
    ready_statuses: Optional[Set[str]] = None,
) -> Dict[str, ReadyExplain]:
    """Pure ready/explain over a task set + relations.

    A ticket is ``ready`` when its status is in ``ready_statuses`` (default
    ``{backlog}``) and every ``blocks`` predecessor is done or canceled.
    Unknown blockers (referenced but missing from ``status_by_id``) count
    as still blocking.
    """
    wanted = ready_statuses if ready_statuses is not None else {"backlog"}
    # to_id → list of from_ids that block it
    blockers_of: Dict[str, List[str]] = {}
    for rel in relations:
        if rel.relation_type not in _BLOCKING_TYPES:
            continue
        blockers_of.setdefault(rel.to_id, []).append(rel.from_id)

    out: Dict[str, ReadyExplain] = {}
    for tid in task_ids:
        tid_s = _norm_id(tid)
        status = status_by_id.get(tid_s, "")
        unresolved: List[str] = []
        seen: Set[str] = set()
        for bid in blockers_of.get(tid_s, []):
            if bid in seen:
                continue
            seen.add(bid)
            bstatus = status_by_id.get(bid)
            if bstatus is None or bstatus not in _DONE_STATUSES:
                unresolved.append(bid)
        is_ready = status in wanted and len(unresolved) == 0
        out[tid_s] = ReadyExplain(
            task_id=tid_s,
            ready=is_ready,
            blocked_by=unresolved,
            status=status,
        )
    return out


def ready_task_ids(
    task_ids: Sequence[str],
    status_by_id: Dict[str, str],
    relations: Sequence[Relation],
    *,
    ready_statuses: Optional[Set[str]] = None,
) -> List[str]:
    """Ids from ``task_ids`` that :func:`explain_ready` marks ready."""
    explained = explain_ready(
        task_ids, status_by_id, relations, ready_statuses=ready_statuses
    )
    return [tid for tid in task_ids if explained[_norm_id(tid)].ready]


# ── backfill from prose / labels ─────────────────────────────────────


def _label_parent_edges(task_id: str, labels: Sequence[str]) -> List[BackfillPlanItem]:
    items: List[BackfillPlanItem] = []
    for lab in labels or []:
        text = (lab or "").strip()
        m = _PARENT_LABEL_RE.match(text)
        if m:
            parent = m.group("id")
            items.append(
                BackfillPlanItem(
                    from_id=parent,
                    to_id=task_id,
                    relation_type="parent-child",
                    source=f"label:{text.split(':', 1)[0].lower()}",
                )
            )
            continue
        m = _EPIC_LABEL_RE.match(text)
        if m:
            parent = m.group("id")
            items.append(
                BackfillPlanItem(
                    from_id=parent,
                    to_id=task_id,
                    relation_type="parent-child",
                    source="label:epic",
                )
            )
    return items


def plan_backfill_from_tasks(
    tasks: Iterable[Any],
) -> List[BackfillPlanItem]:
    """Parse tasks (``id``, ``description``, ``labels``) into proposed rows.

    * ``Depends on #N`` / ``Blocked by #N`` (via ``_parse_blockers``) →
      ``blocks`` edge N → task
    * labels ``parent:N`` / ``slice-of:N`` / numeric ``epic:N`` →
      ``parent-child`` edge N → task

    Non-numeric epic labels (e.g. ``epic:wl-18``) are membership tags, not
    task ids — skipped intentionally.
    """
    from worklane.trackers.sqlite import _parse_blockers

    planned: List[BackfillPlanItem] = []
    seen: Set[Tuple[str, str, str]] = set()

    for task in tasks:
        if isinstance(task, dict):
            tid = _norm_id(task.get("id"))
            desc = task.get("description") or ""
            labels = list(task.get("labels") or [])
        else:
            tid = _norm_id(getattr(task, "id", ""))
            desc = getattr(task, "description", "") or ""
            labels = list(getattr(task, "labels", None) or [])
        if not tid:
            continue

        for ref in _parse_blockers(str(desc or "")):
            # Only local numeric refs become structured rows; SEO-N stays prose.
            if not str(ref).isdigit():
                continue
            key = (str(ref), tid, "blocks")
            if key in seen:
                continue
            seen.add(key)
            planned.append(
                BackfillPlanItem(
                    from_id=str(ref),
                    to_id=tid,
                    relation_type="blocks",
                    source="depends-on",
                )
            )

        for item in _label_parent_edges(tid, labels):
            key = (item.from_id, item.to_id, item.relation_type)
            if key in seen:
                continue
            seen.add(key)
            planned.append(item)

    return planned


def plan_backfill(db_path: Path) -> BackfillReport:
    """Scan tasks in ``db_path`` and return a dry-run report (no writes)."""
    from worklane.trackers.sqlite import SQLiteTracker

    tracker = SQLiteTracker(db_path=Path(db_path), product_default="")
    tasks = tracker.list_tasks(limit=None)
    planned = plan_backfill_from_tasks(tasks)
    return BackfillReport(planned=planned, dry_run=True)


def apply_backfill(db_path: Path, *, dry_run: bool = True) -> BackfillReport:
    """Materialize planned relations. Default ``dry_run=True`` writes nothing.

    Missing endpoints and cycle-creating edges are skipped and reported.
    Existing edges are skipped (idempotent).
    """
    path = Path(db_path)
    report = plan_backfill(path)
    report.dry_run = dry_run
    existing = {
        (r.from_id, r.to_id, r.relation_type) for r in list_relations(path)
    }
    if dry_run:
        # Classify already-present edges so the report is actionable without writes.
        still: List[BackfillPlanItem] = []
        for item in report.planned:
            key = (item.from_id, item.to_id, item.relation_type)
            if key in existing:
                report.skipped_existing.append(item)
            else:
                still.append(item)
        report.planned = still
        return report

    for item in report.planned:
        key = (item.from_id, item.to_id, item.relation_type)
        if key in existing:
            report.skipped_existing.append(item)
            continue
        try:
            create_relation(
                path, item.from_id, item.to_id, item.relation_type
            )
        except RelationError as exc:
            msg = str(exc).lower()
            if "not found" in msg:
                report.skipped_missing.append(item)
            elif "cycle" in msg or "self-edge" in msg:
                report.skipped_cycle.append(item)
            elif "already exists" in msg:
                report.skipped_existing.append(item)
            else:
                report.skipped_missing.append(item)
            continue
        existing.add(key)
        report.applied.append(item)
    return report


def load_status_map(db_path: Path) -> Dict[str, str]:
    """task_id → status for ready/explain over a store."""
    conn = connect(db_path)
    try:
        rows = conn.execute("SELECT id, status FROM tasks").fetchall()
        return {str(r["id"]): (r["status"] or "") for r in rows}
    finally:
        conn.close()


__all__ = [
    "RELATION_TYPES",
    "BackfillPlanItem",
    "BackfillReport",
    "ReadyExplain",
    "Relation",
    "RelationError",
    "apply_backfill",
    "connect",
    "create_relation",
    "delete_relation",
    "delete_relation_edge",
    "ensure_relations_schema",
    "explain_ready",
    "get_relation",
    "list_relations",
    "load_status_map",
    "plan_backfill",
    "plan_backfill_from_tasks",
    "ready_task_ids",
    "would_create_cycle",
]
