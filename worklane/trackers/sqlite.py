"""SQLiteTracker — ticket store under ``worklane/local/data/``.

Default path: ``<main-worktree>/worklane/local/data/tradeos.db``
(override with ``WORKLANE_DB``; legacy ``TRADEOS_TRACKER_DB``
still supported). Product / planning tickets live here. Builder-scoped
tickets use a separate file — see :func:`core.web.routes.admin_tasks.get_ops_ticket_tracker`.

SEO-171 introduced local task tracking; ADR-019/021 split product vs operations
surfaces. Storing tasks in SQLite removes the external-SaaS dependency from the
self-hosted story (ADR-011).

Schema (created lazily on first connect, idempotent via IF NOT EXISTS):

* ``tasks``         — one row per work item.
* ``task_comments`` — 1:N per-task comment stream.

The connection pattern mirrors ``core/web/utils/notifications.py``:
short-lived connections, WAL mode for cheap concurrent reads, and a
module-level ``_initialized_dbs`` guard so the first connection against
a given path runs schema setup once and subsequent opens are cheap.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterator, List, Optional

from worklane.trackers.protocol import ProjectTracker, Task, TaskComment, TaskStatus


def _tp_root() -> Path:
    """WorkLane package root (``worklane/``).

    Computed from this module's own location so WL stays independent of
    any host repo's path helpers.
    """
    return Path(__file__).resolve().parents[1]


def _main_worktree_root() -> Path:
    """Resolve the main git worktree root for the repo that hosts WL.

    WL can be vendored into any repo; it still needs to find the single
    "main" worktree so concurrent worktrees share one ticket DB. We walk
    up from WL's own location and follow the ``.git`` pointer file if we
    land inside a linked worktree.
    """
    repo = _tp_root().parent
    dot_git = repo / ".git"
    if dot_git.is_file():
        # Inside a worktree — .git is a pointer file
        text = dot_git.read_text().strip()
        if text.startswith("gitdir:"):
            gitdir = Path(text.split(":", 1)[1].strip())
            # gitdir points to <main>/.git/worktrees/<name>
            main_root = gitdir.parents[2]
            if (main_root / "worklane").exists():
                return main_root
    return repo


REPO_ROOT = _main_worktree_root()
_MAIN_ROOT = REPO_ROOT
# WorkLane runtime root (separate product boundary).
TICKETING_ROOT = _MAIN_ROOT / "worklane"
# Product ticket DB — gitignored runtime state under worklane/local/data.
DEFAULT_DB_PATH = TICKETING_ROOT / "local" / "data" / "tradeos.db"
# Pre-rename fallback: hidden `.local/` layout retained so installs mid-upgrade still read.
LEGACY_HIDDEN_DB_PATH = TICKETING_ROOT / ".local" / "data" / "tradeos.db"
# Pre-cord-cut fallback: DB under the consuming repo root (tradeOS-side).
LEGACY_DB_PATH = _MAIN_ROOT / "local" / "data" / "tradeos.db"

# Default ``product:*`` label applied on create when the row has none yet.
PRODUCT_LABEL_TRADEOS = "product:tradeos"
PRODUCT_LABEL_OPS = "product:ops"


_initialized_dbs: set[str] = set()
_DEPENDENCY_FREEZE_LABEL = "queue:frozen-dependency"
_COMPLETED_RE = re.compile(r"^\s*completed\s*:", re.IGNORECASE | re.MULTILINE)
_VERIFICATION_RE = re.compile(r"^\s*verification\s*:", re.IGNORECASE | re.MULTILINE)
_BLOCKED_RE = re.compile(r"^\s*blocked\s*:", re.IGNORECASE | re.MULTILINE)
_NEXT_STEP_RE = re.compile(r"next\s+step", re.IGNORECASE)
_OWNER_RE = re.compile(r"^\s*owner\s*:", re.IGNORECASE | re.MULTILINE)
_PLAN_RE = re.compile(r"^\s*plan\s*:", re.IGNORECASE | re.MULTILINE)
_START_RE = re.compile(r"^\s*start\s*:", re.IGNORECASE | re.MULTILINE)
_HEADING_RE = re.compile(
    r"^(?:#{1,6}\s+|\*\*)\s*(?P<title>[^*\n]+?)\s*(?:\*\*)?\s*$",
    re.MULTILINE,
)
_SEO_TICKET_RE = re.compile(r"\bSEO-(\d+)\b", re.IGNORECASE)
_LOCAL_TICKET_RE = re.compile(r"(?:^|[^A-Za-z0-9_])#(\d+)\b")
_BLOCKER_KEYWORDS = ("depend", "blocked by", "blockers", "requires")
# Inline blocker declarations (PROTOCOL.md: "use `Depends on #NNN`").
# Only refs immediately following the keyword count — a run of ticket
# refs separated by commas/"and"/etc., ending at the first non-ref token.
_REF_TOKEN = r"(?:#\d+|SEO-\d+)\b"
_BLOCKER_DECL_RE = re.compile(
    r"(?:\bdepends?\s+on\b|\bblocked\s+by\b|^[ \t]*blockers?\b)[ \t]*:?[ \t]*"
    rf"(?P<refs>{_REF_TOKEN}(?:[ \t]*(?:,|;|/|&|\+|\band\b)?[ \t]*{_REF_TOKEN})*)",
    re.IGNORECASE | re.MULTILINE,
)
# Parent-epic references are membership, not dependency — an epic can
# never close before its phase tickets, so counting them deadlocks.
_EPIC_REF_RE = re.compile(r"\bepic[ \t]*:[ \t]*#\d+", re.IGNORECASE)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _extract_ticket_refs(text: str) -> List[str]:
    refs: List[str] = []
    seen = set()
    for m in _SEO_TICKET_RE.finditer(text or ""):
        ref = f"SEO-{m.group(1)}"
        if ref not in seen:
            seen.add(ref)
            refs.append(ref)
    for m in _LOCAL_TICKET_RE.finditer(text or ""):
        ref = m.group(1)
        if ref not in seen:
            seen.add(ref)
            refs.append(ref)
    return refs


def _parse_blockers(description: str) -> List[str]:
    """Extract declared blocker refs from a ticket description.

    Two forms count as declarations:

    1. A heading whose title contains a blocker keyword (``## Dependencies``,
       ``**Blocked by**`` ...): every ref in the section body.
    2. Inline ``Depends on #N`` / ``Blocked by #N`` / line-leading
       ``Blockers: #N`` — only the refs immediately following the keyword.

    Prose mentions never count: "requires" in a sentence, ``Related:`` /
    context refs in the same paragraph, and parent ``epic:#N`` references
    must not freeze a ticket (false-positive history on #834).
    """
    text = description or ""
    if not text:
        return []
    text = _EPIC_REF_RE.sub("", text)
    seen = set()
    out: List[str] = []
    headings = list(_HEADING_RE.finditer(text))
    for i, match in enumerate(headings):
        title = (match.group("title") or "").strip().lower()
        if not any(k in title for k in _BLOCKER_KEYWORDS):
            continue
        body_start = match.end()
        body_end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
        for ref in _extract_ticket_refs(text[body_start:body_end]):
            if ref not in seen:
                seen.add(ref)
                out.append(ref)
    for match in _BLOCKER_DECL_RE.finditer(text):
        for ref in _extract_ticket_refs(match.group("refs")):
            if ref not in seen:
                seen.add(ref)
                out.append(ref)
    return out


def _task_count(path: Path) -> int:
    """Best-effort task row count for migration/fallback decisions."""
    if not path.exists():
        return -1
    try:
        conn = sqlite3.connect(str(path), timeout=2.0)
        try:
            row = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()
    except Exception:
        return -2


def _row_to_task(row: sqlite3.Row) -> Task:
    labels_raw = row["labels"] or "[]"
    try:
        labels = list(json.loads(labels_raw))
    except Exception:
        labels = []
    row_keys = row.keys()
    return Task(
        id=str(row["id"]),
        ext_id=row["ext_id"] or None,
        title=row["title"] or "",
        description=row["description"] or "",
        status=row["status"] or TaskStatus.BACKLOG,
        priority=int(row["priority"] or 3),
        labels=labels,
        created_at=row["created_at"] or "",
        updated_at=row["updated_at"] or "",
        gate_type=(row["gate_type"] or None) if "gate_type" in row_keys else None,
        gate_until=(row["gate_until"] or None) if "gate_until" in row_keys else None,
        gate_note=(row["gate_note"] or None) if "gate_note" in row_keys else None,
        intake=(row["intake"] or None) if "intake" in row_keys else None,
    )


def _row_to_comment(row: sqlite3.Row) -> TaskComment:
    return TaskComment(
        id=str(row["id"]),
        task_id=str(row["task_id"]),
        body=row["body"] or "",
        author=row["author"] or "",
        created_at=row["created_at"] or "",
    )


class SQLiteTracker(ProjectTracker):
    """Local-file project tracker for ops tickets.

    Pass ``db_path`` to override the DB location (tests do this). The
    default is :data:`DEFAULT_DB_PATH`
    (``worklane/local/data/tradeos.db`` under the main worktree root
    unless ``WORKLANE_DB`` or legacy ``TRADEOS_TRACKER_DB`` is set).
    """

    name = "sqlite"

    def __init__(
        self,
        db_path: Optional[Path] = None,
        *,
        product_default: str = PRODUCT_LABEL_TRADEOS,
    ) -> None:
        if db_path is None:
            env = (
                os.environ.get("WORKLANE_DB")
                or os.environ.get("TRADEOS_TRACKER_DB")
            )
            if env:
                db_path = Path(env)
            elif DEFAULT_DB_PATH.exists():
                # If the canonical DB is empty but a legacy store has tasks,
                # keep reading legacy until tickets-install migrates it.
                if _task_count(DEFAULT_DB_PATH) == 0:
                    if LEGACY_HIDDEN_DB_PATH.exists() and _task_count(LEGACY_HIDDEN_DB_PATH) > 0:
                        db_path = LEGACY_HIDDEN_DB_PATH
                    elif LEGACY_DB_PATH.exists() and _task_count(LEGACY_DB_PATH) > 0:
                        db_path = LEGACY_DB_PATH
                    else:
                        db_path = DEFAULT_DB_PATH
                else:
                    db_path = DEFAULT_DB_PATH
            elif LEGACY_HIDDEN_DB_PATH.exists():
                # Compatibility fallback: pre-rename hidden .local/ path.
                db_path = LEGACY_HIDDEN_DB_PATH
            elif LEGACY_DB_PATH.exists():
                # Compatibility fallback: keep pre-cord-cut installs readable
                # until tickets-install migrates into worklane/local.
                db_path = LEGACY_DB_PATH
            else:
                # Truly fresh install — nothing on disk anywhere. Route the
                # filename through the configured default product (wl-124)
                # instead of the tradeos.db literal, so a fresh WorkLane
                # install doesn't create a database named after tradeOS.
                # Existing hosts never reach this branch: DEFAULT_DB_PATH
                # already exists for them, handled above.
                from worklane.products import default_product_slug, wl_data_dir

                slug = default_product_slug() or "tradeos"
                db_path = (
                    DEFAULT_DB_PATH if slug == "tradeos" else wl_data_dir() / f"{slug}.db"
                )
        self._db_path = Path(db_path)
        self._product_default = product_default

    def _merge_default_product_label(self, labels: Optional[List[str]]) -> List[str]:
        lab = list(labels or [])
        if self._product_default and not any(
            (x or "").startswith("product:") for x in lab
        ):
            lab.append(self._product_default)
        return lab

    # ── connection management ────────────────────────────────────────

    def _ensure_dir(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self._ensure_dir()
        key = str(self._db_path.resolve())
        fresh = key not in _initialized_dbs
        conn = sqlite3.connect(str(self._db_path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            if fresh:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("PRAGMA foreign_keys=ON")
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS tasks (
                        id           INTEGER PRIMARY KEY AUTOINCREMENT,
                        ext_id       TEXT,
                        title        TEXT NOT NULL,
                        description  TEXT NOT NULL DEFAULT '',
                        status       TEXT NOT NULL DEFAULT 'backlog',
                        priority     INTEGER NOT NULL DEFAULT 3,
                        labels       TEXT NOT NULL DEFAULT '[]',
                        created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                        updated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                        gate_type    TEXT,
                        gate_until   TEXT,
                        gate_note    TEXT,
                        intake       TEXT
                    );
                    CREATE INDEX IF NOT EXISTS ix_tasks_status   ON tasks(status);
                    CREATE INDEX IF NOT EXISTS ix_tasks_ext_id   ON tasks(ext_id);
                    CREATE INDEX IF NOT EXISTS ix_tasks_updated  ON tasks(updated_at DESC);

                    CREATE TABLE IF NOT EXISTS task_comments (
                        id          INTEGER PRIMARY KEY AUTOINCREMENT,
                        task_id     INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                        body        TEXT NOT NULL,
                        author      TEXT NOT NULL DEFAULT '',
                        created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                    );
                    CREATE INDEX IF NOT EXISTS ix_task_comments_task ON task_comments(task_id);

                    -- wl-20: structured relations (additive; also ensured by relations.py)
                    CREATE TABLE IF NOT EXISTS task_relations (
                        id            INTEGER PRIMARY KEY AUTOINCREMENT,
                        from_id       INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                        to_id         INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                        relation_type TEXT    NOT NULL,
                        created_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                        UNIQUE(from_id, to_id, relation_type)
                    );
                    CREATE INDEX IF NOT EXISTS ix_task_relations_from
                        ON task_relations(from_id);
                    CREATE INDEX IF NOT EXISTS ix_task_relations_to
                        ON task_relations(to_id);
                    CREATE INDEX IF NOT EXISTS ix_task_relations_type
                        ON task_relations(relation_type);

                    -- wl-101: append-only change feed. The row id IS the
                    -- cursor — durable across restarts because it's the
                    -- table's own autoincrement, not an in-memory counter.
                    CREATE TABLE IF NOT EXISTS task_events (
                        id          INTEGER PRIMARY KEY AUTOINCREMENT,
                        task_id     INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                        event_type  TEXT    NOT NULL,
                        status      TEXT,
                        labels      TEXT,
                        actor       TEXT,
                        created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                    );
                    CREATE INDEX IF NOT EXISTS ix_task_events_task ON task_events(task_id);
                    """
                )
                # wl-21: gate_* columns didn't exist before this ticket, so
                # CREATE TABLE IF NOT EXISTS above is a no-op against a DB
                # created pre-wl-21 — retrofit via ALTER TABLE (no ADD
                # COLUMN precedent existed in this file until now).
                existing_cols = {
                    r["name"] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()
                }
                for col in ("gate_type", "gate_until", "gate_note", "intake"):
                    if col not in existing_cols:
                        conn.execute(f"ALTER TABLE tasks ADD COLUMN {col} TEXT")
                # Scene-feed attribution: the actor column didn't exist when
                # task_events shipped (wl-101), so CREATE TABLE IF NOT EXISTS
                # above is a no-op against a DB created before it — retrofit
                # via ALTER TABLE (same pattern as the tasks gate_* columns).
                event_cols = {
                    r["name"]
                    for r in conn.execute("PRAGMA table_info(task_events)").fetchall()
                }
                if event_cols and "actor" not in event_cols:
                    conn.execute("ALTER TABLE task_events ADD COLUMN actor TEXT")
                _initialized_dbs.add(key)
            yield conn
        finally:
            conn.close()

    # ── ProjectTracker API ───────────────────────────────────────────

    def list_tasks(
        self,
        *,
        status: Optional[str] = None,
        label: Optional[str] = None,
        priority: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> List[Task]:
        sql = "SELECT * FROM tasks"
        clauses: List[str] = []
        params: List[object] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if label:
            # Simple substring match on the JSON blob — a full inverted
            # index is overkill at task counts we expect (O(1k) max).
            clauses.append("labels LIKE ?")
            params.append(f'%"{label}"%')
        if priority is not None:
            clauses.append("priority = ?")
            params.append(int(priority))
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY updated_at DESC"
        if limit is not None and limit > 0:
            sql += " LIMIT ?"
            params.append(int(limit))

        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_task(r) for r in rows]

    def get_task(self, task_id: str) -> Optional[Task]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE id = ? OR ext_id = ? LIMIT 1",
                (self._maybe_int(task_id), str(task_id)),
            ).fetchone()
        return _row_to_task(row) if row else None

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
        intake: Optional[str] = None,
    ) -> Task:
        now = _now_iso()
        merged = self._merge_default_product_label(labels)
        labels_json = json.dumps(merged)
        with self._connect() as conn:
            with conn:
                cur = conn.execute(
                    """
                    INSERT INTO tasks
                        (ext_id, title, description, status, priority, labels,
                         created_at, updated_at, intake)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (ext_id, title, description, status, int(priority),
                     labels_json, now, now, intake),
                )
                task_pk = cur.lastrowid
                self._insert_event(
                    conn, task_pk, "created", status=status, labels=merged,
                    actor=actor, now=now,
                )
            row = conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_pk,)
            ).fetchone()
        return _row_to_task(row)

    def update_status(
        self, task_id: str, status: str, actor: str = ""
    ) -> Optional[Task]:
        if status not in TaskStatus.ALL:
            raise ValueError(
                f"unknown status {status!r}; expected one of {TaskStatus.ALL}"
            )
        now = _now_iso()
        with self._connect() as conn:
            cur_row = conn.execute(
                "SELECT * FROM tasks WHERE id = ? OR ext_id = ? LIMIT 1",
                (self._maybe_int(task_id), str(task_id)),
            ).fetchone()
            if not cur_row:
                return None
            cur_task = _row_to_task(cur_row)
            target_status = status

            # Dependency guard: blocked tickets cannot be claimed directly.
            # Keep them in the frozen pool (in_review) until blockers clear.
            if status == TaskStatus.IN_PROGRESS:
                unresolved = self._unresolved_blockers(conn, cur_task)
                if unresolved:
                    target_status = TaskStatus.IN_REVIEW
                    labels = list(cur_task.labels or [])
                    if _DEPENDENCY_FREEZE_LABEL not in labels:
                        labels.append(_DEPENDENCY_FREEZE_LABEL)
                    with conn:
                        conn.execute(
                            """
                            UPDATE tasks
                               SET status = ?, labels = ?, updated_at = ?
                             WHERE id = ?
                            """,
                            (
                                target_status,
                                json.dumps(labels),
                                now,
                                int(cur_row["id"]),
                            ),
                        )
                        self._insert_comment(
                            conn,
                            int(cur_row["id"]),
                            (
                                "Dependency guard froze this ticket in in_review. "
                                "Attempted in_progress while blockers are unresolved: "
                                + ", ".join(unresolved)
                            ),
                            "dependency-guard",
                            now,
                        )
                        if target_status != cur_task.status:
                            self._insert_event(
                                conn, int(cur_row["id"]), "status_change",
                                status=target_status,
                                actor=actor or "dependency-guard", now=now,
                            )
                        self._thaw_dependency_frozen(conn, now)
                    return self.get_task(task_id)

            with conn:
                conn.execute(
                    """
                    UPDATE tasks SET status = ?, updated_at = ?
                     WHERE id = ? OR ext_id = ?
                    """,
                    (target_status, now, self._maybe_int(task_id), str(task_id)),
                )
                if target_status != cur_task.status:
                    self._insert_event(
                        conn, int(cur_row["id"]), "status_change",
                        status=target_status, actor=actor, now=now,
                    )
                # Entering in_progress freezes backlog dependents.
                if (
                    target_status == TaskStatus.IN_PROGRESS
                    and cur_task.status != TaskStatus.IN_PROGRESS
                ):
                    fresh = conn.execute(
                        "SELECT * FROM tasks WHERE id = ? LIMIT 1",
                        (int(cur_row["id"]),),
                    ).fetchone()
                    if fresh:
                        self._freeze_dependents_for_anchor(conn, _row_to_task(fresh), now)
                self._thaw_dependency_frozen(conn, now)
        return self.get_task(task_id)

    def add_comment(self, task_id: str, body: str, author: str = "") -> TaskComment:
        now = _now_iso()
        with self._connect() as conn:
            resolved = conn.execute(
                "SELECT id, status FROM tasks WHERE id = ? OR ext_id = ? LIMIT 1",
                (self._maybe_int(task_id), str(task_id)),
            ).fetchone()
            if not resolved:
                raise KeyError(f"task {task_id!r} not found")
            task_pk = int(resolved["id"])
            current_status = (resolved["status"] or "").strip()
            with conn:
                cur = conn.execute(
                    """
                    INSERT INTO task_comments (task_id, body, author, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (task_pk, body, author, now),
                )
                comment_pk = cur.lastrowid
                conn.execute(
                    "UPDATE tasks SET updated_at = ? WHERE id = ?",
                    (now, task_pk),
                )
                self._apply_comment_lifecycle(
                    conn, task_pk, current_status, body, now, actor=author
                )
                self._thaw_dependency_frozen(conn, now)
            row = conn.execute(
                "SELECT * FROM task_comments WHERE id = ?", (comment_pk,)
            ).fetchone()
        return _row_to_comment(row)

    # ── extras (SQLiteTracker-specific, not on the Protocol) ─────────

    def upsert_by_ext_id(
        self,
        *,
        ext_id: str,
        title: str,
        description: str = "",
        status: str = TaskStatus.BACKLOG,
        priority: int = 3,
        labels: Optional[List[str]] = None,
    ) -> Task:
        """Insert a task, or update in place if ``ext_id`` already exists.

        Used by the Linear import script so rerunning the migration with
        a fresh export is idempotent. Not part of the ProjectTracker
        protocol because other adapters have their own identity models.
        """
        now = _now_iso()
        labels_json = json.dumps(list(labels or []))
        with self._connect() as conn:
            with conn:
                existing = conn.execute(
                    "SELECT id FROM tasks WHERE ext_id = ? LIMIT 1",
                    (ext_id,),
                ).fetchone()
                if existing is None:
                    cur = conn.execute(
                        """
                        INSERT INTO tasks
                            (ext_id, title, description, status, priority,
                             labels, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (ext_id, title, description, status, int(priority),
                         labels_json, now, now),
                    )
                    task_pk = cur.lastrowid
                else:
                    task_pk = int(existing["id"])
                    conn.execute(
                        """
                        UPDATE tasks
                           SET title = ?, description = ?, status = ?,
                               priority = ?, labels = ?, updated_at = ?
                         WHERE id = ?
                        """,
                        (title, description, status, int(priority),
                         labels_json, now, task_pk),
                    )
            row = conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_pk,)
            ).fetchone()
        return _row_to_task(row)

    def list_comments(self, task_id: str) -> List[TaskComment]:
        with self._connect() as conn:
            resolved = conn.execute(
                "SELECT id FROM tasks WHERE id = ? OR ext_id = ? LIMIT 1",
                (self._maybe_int(task_id), str(task_id)),
            ).fetchone()
            if not resolved:
                return []
            rows = conn.execute(
                """
                SELECT * FROM task_comments
                 WHERE task_id = ?
                 ORDER BY created_at ASC
                """,
                (int(resolved["id"]),),
            ).fetchall()
        return [_row_to_comment(r) for r in rows]

    def list_events(self, *, since: int = 0, limit: int = 100) -> List[dict]:
        """Ordered change events with ``id > since`` — the poll-cursor feed (wl-101).

        ``since`` is the last event id a consumer already saw (0 for the
        full backlog). Event ids are the table's own autoincrement, so a
        cursor is just an integer — durable across server restarts with
        no separate cursor-store to keep in sync.
        """
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT e.id, e.task_id, e.event_type, e.status, e.labels,
                       e.created_at, t.ext_id, t.title
                  FROM task_events e
                  JOIN tasks t ON t.id = e.task_id
                 WHERE e.id > ?
                 ORDER BY e.id ASC
                 LIMIT ?
                """,
                (int(since), int(limit)),
            ).fetchall()
        events: List[dict] = []
        for r in rows:
            try:
                labels = json.loads(r["labels"]) if r["labels"] else None
            except Exception:
                labels = None
            events.append({
                "id": r["id"],
                "task_id": r["ext_id"] or str(r["task_id"]),
                "task_title": r["title"],
                "event_type": r["event_type"],
                "status": r["status"],
                "labels": labels,
                "created_at": r["created_at"],
            })
        return events

    def generation_token(self) -> dict:
        """Cheap freshness token for suite pulse bus (wl-217 / LIVE-B1).

        Advances when task_events, task_comments, or tasks.updated_at move.
        Tokens only — never task bodies.
        """
        with self._connect() as conn:
            max_ev = int(
                conn.execute(
                    "SELECT COALESCE(MAX(id), 0) FROM task_events"
                ).fetchone()[0]
                or 0
            )
            max_c = int(
                conn.execute(
                    "SELECT COALESCE(MAX(id), 0) FROM task_comments"
                ).fetchone()[0]
                or 0
            )
            max_u = (
                conn.execute(
                    "SELECT COALESCE(MAX(updated_at), '') FROM tasks"
                ).fetchone()[0]
                or ""
            )
        token = "e%d-c%d-%s" % (max_ev, max_c, max_u)
        return {
            "token": token,
            "events": max_ev,
            "comments": max_c,
            "updated_at": max_u,
        }

    # ── helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _maybe_int(value: object) -> object:
        """Accept either an integer primary key or an ``ext_id`` string."""
        try:
            return int(str(value))
        except (TypeError, ValueError):
            return -1

    def _insert_comment(
        self,
        conn: sqlite3.Connection,
        task_pk: int,
        body: str,
        author: str,
        now: str,
    ) -> None:
        conn.execute(
            """
            INSERT INTO task_comments (task_id, body, author, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (task_pk, body, author, now),
        )

    def _insert_event(
        self,
        conn: sqlite3.Connection,
        task_pk: int,
        event_type: str,
        *,
        status: Optional[str] = None,
        labels: Optional[List[str]] = None,
        actor: Optional[str] = None,
        now: Optional[str] = None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO task_events (task_id, event_type, status, labels, actor, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                task_pk,
                event_type,
                status,
                json.dumps(labels) if labels is not None else None,
                (actor or "").strip() or None,
                now or _now_iso(),
            ),
        )

    def _apply_comment_lifecycle(
        self,
        conn: sqlite3.Connection,
        task_pk: int,
        current_status: str,
        body: str,
        now: str,
        actor: str = "",
    ) -> None:
        text = body or ""
        if current_status == TaskStatus.BACKLOG:
            # Ownership marker fallback: if an agent posts Owner/Start/Plan
            # without explicit status claim, reserve the ticket in in_review
            # so it leaves the free pool. Agents promote to in_progress
            # explicitly when they start coding (see PROTOCOL.md §2).
            if _OWNER_RE.search(text) and (_PLAN_RE.search(text) or _START_RE.search(text)):
                conn.execute(
                    "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                    (TaskStatus.IN_REVIEW, now, task_pk),
                )
                self._insert_event(
                    conn, task_pk, "status_change", status=TaskStatus.IN_REVIEW,
                    actor=actor, now=now,
                )
                fresh = conn.execute(
                    "SELECT * FROM tasks WHERE id = ? LIMIT 1",
                    (task_pk,),
                ).fetchone()
                if fresh:
                    self._freeze_dependents_for_anchor(conn, _row_to_task(fresh), now)
            return

        if current_status not in (TaskStatus.IN_PROGRESS, TaskStatus.IN_REVIEW):
            return
        has_completed = bool(_COMPLETED_RE.search(text))
        has_verification = bool(_VERIFICATION_RE.search(text))
        has_blocked = bool(_BLOCKED_RE.search(text))
        has_next_step = bool(_NEXT_STEP_RE.search(text))

        if has_completed and has_verification:
            conn.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                (TaskStatus.DONE, now, task_pk),
            )
            self._insert_event(
                conn, task_pk, "status_change", status=TaskStatus.DONE,
                actor=actor, now=now,
            )
            return
        if has_blocked and has_next_step:
            conn.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                (TaskStatus.BACKLOG, now, task_pk),
            )
            self._insert_event(
                conn, task_pk, "status_change", status=TaskStatus.BACKLOG,
                actor=actor, now=now,
            )

    def _unresolved_blockers(
        self, conn: sqlite3.Connection, task: Task
    ) -> List[str]:
        blockers = _parse_blockers(task.description or "")
        unresolved: List[str] = []
        for ref in blockers:
            row = conn.execute(
                "SELECT status FROM tasks WHERE id = ? OR ext_id = ? LIMIT 1",
                (self._maybe_int(ref), str(ref)),
            ).fetchone()
            if row is None or (row["status"] or "") not in (
                TaskStatus.DONE,
                TaskStatus.CANCELED,
            ):
                unresolved.append(ref)
        return unresolved

    def _freeze_dependents_for_anchor(
        self, conn: sqlite3.Connection, anchor: Task, now: str
    ) -> None:
        refs = {str(anchor.id)}
        if anchor.ext_id:
            refs.add(str(anchor.ext_id))
        if not refs:
            return
        anchor_label = anchor.ext_id or f"#{anchor.id}"
        rows = conn.execute(
            "SELECT * FROM tasks WHERE status = ? ORDER BY updated_at DESC",
            (TaskStatus.BACKLOG,),
        ).fetchall()
        for row in rows:
            t = _row_to_task(row)
            blockers = set(_parse_blockers(t.description or ""))
            if not blockers.intersection(refs):
                continue
            labels = list(t.labels or [])
            if _DEPENDENCY_FREEZE_LABEL not in labels:
                labels.append(_DEPENDENCY_FREEZE_LABEL)
            conn.execute(
                """
                UPDATE tasks
                   SET status = ?, labels = ?, updated_at = ?
                 WHERE id = ?
                """,
                (
                    TaskStatus.IN_REVIEW,
                    json.dumps(labels),
                    now,
                    int(row["id"]),
                ),
            )
            self._insert_comment(
                conn,
                int(row["id"]),
                (
                    f"Dependency guard froze ticket in in_review because {anchor_label} "
                    "is in_progress."
                ),
                "dependency-guard",
                now,
            )
            if t.status != TaskStatus.IN_REVIEW:
                self._insert_event(
                    conn, int(row["id"]), "status_change",
                    status=TaskStatus.IN_REVIEW, actor="dependency-guard", now=now,
                )

    def update_task(
        self,
        task_id: str,
        *,
        title: Optional[str] = None,
        description: Optional[str] = None,
        priority: Optional[int] = None,
        gate_type: Optional[str] = None,
        gate_until: Optional[str] = None,
        gate_note: Optional[str] = None,
        actor: str = "",
    ) -> Optional[Task]:
        """Edit title, description, priority, and/or gate; append an audit comment.

        Only fields explicitly supplied (non-None) are changed. ``gate_type``
        is the gate control: ``None`` leaves the gate untouched, ``""``
        clears it (gate_until/gate_note are cleared too), ``"human"``/
        ``"timer"`` sets it (wl-21). A timer gate requires ``gate_until``.
        Returns the updated Task, or None if ``task_id`` does not exist.
        """
        if gate_type is not None and gate_type not in ("", "human", "timer", "deferred"):
            raise ValueError(
                f"gate_type must be '' (clear), 'human', 'timer', or 'deferred', got {gate_type!r}"
            )
        if gate_type == "timer" and not gate_until:
            raise ValueError("gate_until is required when gate_type is 'timer'")
        if gate_type is None and (gate_until is not None or gate_note is not None):
            raise ValueError("gate_type is required when setting gate_until or gate_note")

        now = _now_iso()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE id = ? OR ext_id = ? LIMIT 1",
                (self._maybe_int(task_id), str(task_id)),
            ).fetchone()
            if not row:
                return None
            task_pk = int(row["id"])

            changes: List[str] = []
            if title is not None and title != (row["title"] or ""):
                changes.append(f"  title: {(row['title'] or '')!r} → {title!r}")
            if description is not None and description != (row["description"] or ""):
                old_snip = (row["description"] or "")[:80].replace("\n", " ")
                new_snip = description[:80].replace("\n", " ")
                changes.append(f"  description: {old_snip!r} → {new_snip!r}")
            if priority is not None and int(priority) != int(row["priority"] or 3):
                changes.append(f"  priority: {row['priority']} → {priority}")
            if gate_type is not None:
                if gate_type == "":
                    changes.append(f"  gate: {row['gate_type']!r} → cleared")
                else:
                    changes.append(
                        f"  gate: {row['gate_type']!r} → {gate_type!r} "
                        f"until {gate_until!r} ({(gate_note or '')!r})"
                    )

            if not changes:
                return _row_to_task(row)

            sets: List[str] = []
            params: List[object] = []
            if title is not None:
                sets.append("title = ?")
                params.append(title)
            if description is not None:
                sets.append("description = ?")
                params.append(description)
            if priority is not None:
                sets.append("priority = ?")
                params.append(int(priority))
            if gate_type is not None:
                sets.append("gate_type = ?")
                params.append(gate_type or None)
                sets.append("gate_until = ?")
                params.append(gate_until if gate_type else None)
                sets.append("gate_note = ?")
                params.append(gate_note if gate_type else None)
            sets.append("updated_at = ?")
            params.append(now)
            params.append(task_pk)

            with conn:
                conn.execute(
                    f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?",
                    params,
                )
                audit = "Updated fields:\n" + "\n".join(changes)
                if actor:
                    audit += f"\nActor: {actor}"
                self._insert_comment(conn, task_pk, audit, actor or "cli-update", now)

        return self.get_task(task_id)

    def count_human_gate_sets_since(self, author: str, since_iso: str) -> int:
        """Count how many times `author` set gate_type=human since `since_iso`.

        Scans task_comments for audit lines written by update_task when
        gate_type='human' is applied.  Used by the hard-stop guard in the API.
        """
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS n
                FROM task_comments
                WHERE author = ?
                  AND body LIKE '%→ ''human''%'
                  AND created_at >= ?
                """,
                (author, since_iso),
            ).fetchone()
        return int(row["n"]) if row else 0

    def human_gate_stats_since(self, since_iso: str) -> List[Dict[str, object]]:
        """Return per-author counts of human-gate sets since `since_iso`.

        Each entry: {"author": str, "count": int}.  Used for metrics display.
        """
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT author, COUNT(*) AS n
                FROM task_comments
                WHERE body LIKE '%→ ''human''%'
                  AND created_at >= ?
                GROUP BY author
                ORDER BY n DESC
                """,
                (since_iso,),
            ).fetchall()
        return [{"author": r["author"], "count": int(r["n"])} for r in rows]

    def update_labels(
        self,
        task_id: str,
        *,
        add: Optional[List[str]] = None,
        remove: Optional[List[str]] = None,
        actor: str = "",
    ) -> Optional[Task]:
        """Add and/or remove labels; append an audit comment.

        Returns the updated Task, or None if ``task_id`` does not exist.
        """
        now = _now_iso()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE id = ? OR ext_id = ? LIMIT 1",
                (self._maybe_int(task_id), str(task_id)),
            ).fetchone()
            if not row:
                return None
            task_pk = int(row["id"])

            try:
                labels: List[str] = list(json.loads(row["labels"] or "[]"))
            except Exception:
                labels = []

            added: List[str] = []
            removed: List[str] = []
            for lb in (add or []):
                if lb not in labels:
                    labels.append(lb)
                    added.append(lb)
            for lb in (remove or []):
                if lb in labels:
                    labels.remove(lb)
                    removed.append(lb)

            if not added and not removed:
                return _row_to_task(row)

            parts: List[str] = []
            if added:
                parts.append("added: " + ", ".join(added))
            if removed:
                parts.append("removed: " + ", ".join(removed))
            audit = "Updated labels — " + "; ".join(parts)
            if actor:
                audit += f"\nActor: {actor}"

            with conn:
                conn.execute(
                    "UPDATE tasks SET labels = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(labels), now, task_pk),
                )
                self._insert_comment(conn, task_pk, audit, actor or "cli-label", now)
                self._insert_event(
                    conn, task_pk, "labels_changed", labels=labels, now=now
                )

        return self.get_task(task_id)

    def _thaw_dependency_frozen(self, conn: sqlite3.Connection, now: str) -> None:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE status = ? ORDER BY updated_at DESC",
            (TaskStatus.IN_REVIEW,),
        ).fetchall()
        for row in rows:
            t = _row_to_task(row)
            labels = list(t.labels or [])
            if _DEPENDENCY_FREEZE_LABEL not in labels:
                continue
            unresolved = self._unresolved_blockers(conn, t)
            if unresolved:
                continue
            new_labels = [lb for lb in labels if lb != _DEPENDENCY_FREEZE_LABEL]
            conn.execute(
                """
                UPDATE tasks
                   SET status = ?, labels = ?, updated_at = ?
                 WHERE id = ?
                """,
                (TaskStatus.BACKLOG, json.dumps(new_labels), now, int(row["id"])),
            )
            self._insert_comment(
                conn,
                int(row["id"]),
                "Dependency guard released ticket back to backlog (all blockers resolved).",
                "dependency-guard",
                now,
            )
            if t.status != TaskStatus.BACKLOG:
                self._insert_event(
                    conn, int(row["id"]), "status_change",
                    status=TaskStatus.BACKLOG, actor="dependency-guard", now=now,
                )


__all__ = [
    "DEFAULT_DB_PATH",
    "LEGACY_TP_DB_PATH",
    "LEGACY_DB_PATH",
    "PRODUCT_LABEL_OPS",
    "PRODUCT_LABEL_TRADEOS",
    "SQLiteTracker",
]
