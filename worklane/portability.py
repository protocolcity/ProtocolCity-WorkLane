"""JSONL export/import for product ticket stores (wl-22).

Pure functions over the SQLite store — no server / HTTP dependency.
Export is read-only. Import only CREATES rows (never updates or deletes).

Line shape (stable key order, one ticket per line)::

    {"id":"1","ext_id":null,"title":"...","description":"...",
     "status":"backlog","priority":3,"labels":[...],
     "created_at":"...","updated_at":"...",
     "comments":[{"id":"1","body":"...","author":"...","created_at":"..."}]}
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from worklane.trackers.sqlite import SQLiteTracker, _now_iso

# Stable field order for export lines (do not sort_keys).
_TASK_KEYS: Sequence[str] = (
    "id",
    "ext_id",
    "title",
    "description",
    "status",
    "priority",
    "labels",
    "created_at",
    "updated_at",
    "comments",
)
_COMMENT_KEYS: Sequence[str] = ("id", "body", "author", "created_at")

_REQUIRED_TASK_FIELDS = ("id", "title")


@dataclass
class ImportReport:
    """Result of :func:`import_jsonl`.

    ``created`` maps each successfully imported source id to the new
    autoincrement id. ``collisions`` lists source ids skipped because an
    existing row already owns the same ``ext_id``. ``errors`` is reserved
    for callers that collect soft failures; the default import path raises
    on malformed lines instead.
    """

    created: List[Tuple[str, str]] = field(default_factory=list)  # (old_id, new_id)
    collisions: List[str] = field(default_factory=list)  # old ids skipped
    errors: List[Tuple[int, str]] = field(default_factory=list)  # (line_no, msg)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "created": [{"old_id": o, "new_id": n} for o, n in self.created],
            "collisions": list(self.collisions),
            "errors": [{"line": ln, "message": msg} for ln, msg in self.errors],
        }


class PortabilityError(ValueError):
    """Malformed JSONL line or invalid product for export/import."""


def _tracker_for_product(slug: str) -> SQLiteTracker:
    """Bind a SQLiteTracker to ``<data>/<slug>.db``.

    Known products use registry paths; unknown slugs resolve to a path under
    the runtime data dir so import can create a new product store.
    """
    from worklane.products import get_product, wl_data_dir

    clean = (slug or "").strip().lower()
    if not clean:
        raise PortabilityError("product slug is required")
    spec = get_product(clean)
    if spec is not None:
        return SQLiteTracker(
            db_path=spec.db_path,
            product_default=f"product:{clean}",
        )
    return SQLiteTracker(
        db_path=wl_data_dir() / f"{clean}.db",
        product_default=f"product:{clean}",
    )


def _ordered_comment(row: sqlite3.Row) -> Dict[str, Any]:
    raw = {
        "id": str(row["id"]),
        "body": row["body"] or "",
        "author": row["author"] or "",
        "created_at": row["created_at"] or "",
    }
    return {k: raw[k] for k in _COMMENT_KEYS}


def _ordered_task(task_row: sqlite3.Row, comments: List[Dict[str, Any]]) -> Dict[str, Any]:
    labels_raw = task_row["labels"] or "[]"
    try:
        labels = list(json.loads(labels_raw))
    except Exception:
        labels = []
    raw: Dict[str, Any] = {
        "id": str(task_row["id"]),
        "ext_id": task_row["ext_id"] if task_row["ext_id"] is not None else None,
        "title": task_row["title"] or "",
        "description": task_row["description"] or "",
        "status": task_row["status"] or "backlog",
        "priority": int(task_row["priority"] or 3),
        "labels": labels,
        "created_at": task_row["created_at"] or "",
        "updated_at": task_row["updated_at"] or "",
        "comments": comments,
    }
    return {k: raw[k] for k in _TASK_KEYS}


def _dumps_line(obj: Dict[str, Any]) -> str:
    # Compact, stable separators; key order comes from the dict construction.
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def export_product(
    slug: str,
    *,
    tracker: Optional[SQLiteTracker] = None,
) -> Iterator[str]:
    """Yield one JSONL line per ticket in ``slug`` (stable id order).

    Read-only. Pass ``tracker`` in tests to point at a fixture DB without
    mutating the product registry.
    """
    tr = tracker if tracker is not None else _tracker_for_product(slug)
    with tr._connect() as conn:
        task_rows = conn.execute(
            "SELECT * FROM tasks ORDER BY id ASC"
        ).fetchall()
        for trow in task_rows:
            crow = conn.execute(
                """
                SELECT * FROM task_comments
                 WHERE task_id = ?
                 ORDER BY created_at ASC, id ASC
                """,
                (int(trow["id"]),),
            ).fetchall()
            comments = [_ordered_comment(c) for c in crow]
            yield _dumps_line(_ordered_task(trow, comments))


def _parse_line(line: str, line_no: int) -> Dict[str, Any]:
    text = line.strip()
    if not text:
        raise PortabilityError(f"line {line_no}: empty line")
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PortabilityError(f"line {line_no}: malformed JSON ({exc})") from exc
    if not isinstance(obj, dict):
        raise PortabilityError(f"line {line_no}: expected a JSON object")
    for key in _REQUIRED_TASK_FIELDS:
        if key not in obj:
            raise PortabilityError(f"line {line_no}: missing required field {key!r}")
    if not str(obj.get("title") or "").strip():
        raise PortabilityError(f"line {line_no}: title must be non-empty")
    comments = obj.get("comments", [])
    if comments is None:
        comments = []
    if not isinstance(comments, list):
        raise PortabilityError(f"line {line_no}: comments must be a list")
    for i, c in enumerate(comments):
        if not isinstance(c, dict):
            raise PortabilityError(f"line {line_no}: comments[{i}] must be an object")
        if "body" not in c:
            raise PortabilityError(f"line {line_no}: comments[{i}] missing body")
    return obj


def _ext_id_exists(conn: sqlite3.Connection, ext_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM tasks WHERE ext_id = ? LIMIT 1",
        (ext_id,),
    ).fetchone()
    return row is not None


def _insert_task_raw(
    conn: sqlite3.Connection,
    obj: Dict[str, Any],
) -> str:
    """Insert one task + comments without lifecycle side effects.

    Preserves status, labels, timestamps, and ext_id from the export payload.
    Does **not** merge product-default labels (export is authoritative).
    """
    now = _now_iso()
    ext_id = obj.get("ext_id")
    if ext_id is not None:
        ext_id = str(ext_id) if ext_id != "" else None
    title = str(obj["title"])
    description = str(obj.get("description") or "")
    status = str(obj.get("status") or "backlog")
    try:
        priority = int(obj.get("priority") if obj.get("priority") is not None else 3)
    except (TypeError, ValueError):
        priority = 3
    labels = obj.get("labels") or []
    if not isinstance(labels, list):
        labels = []
    labels_json = json.dumps(list(labels))
    created_at = str(obj.get("created_at") or now)
    updated_at = str(obj.get("updated_at") or now)

    cur = conn.execute(
        """
        INSERT INTO tasks
            (ext_id, title, description, status, priority, labels,
             created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ext_id,
            title,
            description,
            status,
            priority,
            labels_json,
            created_at,
            updated_at,
        ),
    )
    new_pk = int(cur.lastrowid)
    for c in obj.get("comments") or []:
        conn.execute(
            """
            INSERT INTO task_comments (task_id, body, author, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                new_pk,
                str(c.get("body") or ""),
                str(c.get("author") or ""),
                str(c.get("created_at") or now),
            ),
        )
    return str(new_pk)


_INACTIVE_STATUSES = frozenset({"done", "canceled"})


def _apply_import_routing(obj: Dict[str, Any]) -> Dict[str, Any]:
    """Stamp needs:routing on live imported tickets that arrive without a seat.

    Done/canceled tickets are not in any queue — no stamp needed.
    Import never rejects (pre-hire soft path only): malformed routing is
    preserved as-is so archival restores always succeed.
    """
    status = str(obj.get("status") or "backlog").lower()
    if status in _INACTIVE_STATUSES:
        return obj
    from worklane.routing_labels import ensure_create_labels

    raw_labels = obj.get("labels") or []
    if not isinstance(raw_labels, list):
        raw_labels = []
    routed, _, err = ensure_create_labels(raw_labels, hard_when_hands=False)
    if err or routed == raw_labels:
        return obj
    mutated = dict(obj)
    mutated["labels"] = routed
    return mutated


def import_jsonl(
    lines: Iterable[str],
    product: str,
    *,
    tracker: Optional[SQLiteTracker] = None,
) -> ImportReport:
    """Create tickets from JSONL lines into ``product``; never update/delete.

    Collisions: if a line carries a non-empty ``ext_id`` that already exists
    in the destination store, the line is skipped and listed in
    ``report.collisions``. Malformed lines raise :class:`PortabilityError`.

    Routing law (wl-338): live imported tickets without a ``worker:*`` seat
    get ``needs:routing`` stamped so they appear in the unrouted feed rather
    than silently draining no-one's queue.
    """
    tr = tracker if tracker is not None else _tracker_for_product(product)
    report = ImportReport()

    # Materialize so we can fail fast on parse before writing anything when
    # the whole stream is bad; still process line-by-line for collisions.
    parsed: List[Tuple[int, Dict[str, Any]]] = []
    for line_no, line in enumerate(lines, start=1):
        if not str(line).strip():
            continue
        parsed.append((line_no, _parse_line(str(line), line_no)))

    with tr._connect() as conn:
        with conn:
            for line_no, obj in parsed:
                old_id = str(obj["id"])
                ext_id = obj.get("ext_id")
                if ext_id is not None and str(ext_id) != "":
                    if _ext_id_exists(conn, str(ext_id)):
                        report.collisions.append(old_id)
                        continue
                new_id = _insert_task_raw(conn, _apply_import_routing(obj))
                report.created.append((old_id, new_id))

    return report


def export_to_path(slug: str, out_path: Path, *, tracker: Optional[SQLiteTracker] = None) -> int:
    """Write export JSONL to ``out_path``; return line count."""
    count = 0
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        for line in export_product(slug, tracker=tracker):
            fh.write(line + "\n")
            count += 1
    return count
