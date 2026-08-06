"""Demo product store — seeded sandbox for a first-run board (wl-45).

``wl demo`` (exported as ``wl demo`` / ``worklane demo``) bootstraps an
isolated ``demo`` product store with a handful of realistic tickets across
backlog / in_progress / in_review / done so a fresh clone's board renders
alive and a new agent can immediately claim/work/close one.

Guard rails
-----------
* Writes **only** to the demo product slug's SQLite file (default
  ``demo.db`` under the runtime data dir).
* Refuses protected real-product slugs (``tradeos``, ``worklane``,
  ``worklane``, ``ops`` / ``ops_tickets``).
* Never reads or writes live host product DBs.
* Idempotent by default: an already-seeded store is left untouched unless
  ``force=True`` (which deletes **only** that demo DB + WAL/SHM sidecars).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from worklane.trackers.sqlite import SQLiteTracker

DEFAULT_DEMO_SLUG = "demo"
DEFAULT_DEMO_DISPLAY = "Demo"
DEFAULT_DEMO_PREFIX = "d"

# Slugs that must never be the target of demo seed / wipe.
PROTECTED_SLUGS = frozenset(
    {
        "tradeos",
        "worklane",
        "worklane",
        "ops",
        "ops_tickets",
    }
)


@dataclass(frozen=True)
class SeedTicket:
    """One fixture ticket for the demo store."""

    title: str
    description: str
    status: str
    priority: int
    labels: Tuple[str, ...]
    comments: Tuple[Tuple[str, str], ...]  # (author, body)


def seed_catalog() -> List[SeedTicket]:
    """Canonical demo seed set — board columns all non-empty; one claimable.

    Order is insertion order (ids 1..N on a fresh store). The first backlog
    ticket is the one a new agent should claim first.
    """
    return [
        SeedTicket(
            title="Claim me: try the agent claim protocol",
            description=(
                "Problem: a fresh WorkLane board has no tickets to practice on.\n"
                "Expected outcome: an agent claims this ticket, posts an Owner "
                "marker, does a trivial change, and closes with Completed / "
                "Verification / Links.\n\n"
                "This ticket is intentionally unblocked and in backlog so a "
                "new agent can run the lifecycle end-to-end on first open."
            ),
            status="backlog",
            priority=2,
            labels=("lane:demo", "size:S", "area:onboarding"),
            comments=(
                (
                    "demo-seed",
                    "Seed note: ready to claim. Use wl_claim / `wl status` + "
                    "Owner comment, then close with the §5 sections.",
                ),
            ),
        ),
        SeedTicket(
            title="Add a second product store for your host app",
            description=(
                "Problem: demo only shows one product tab.\n"
                "Expected outcome: file a ticket with surface=<your-slug> "
                "(or drop <slug>.db into the data dir) and see a new Pool tab."
            ),
            status="backlog",
            priority=3,
            labels=("lane:demo", "area:install"),
            comments=(),
        ),
        SeedTicket(
            title="Wire MCP tools in your editor",
            description=(
                "Problem: agents still talk to the board only via curl.\n"
                "Expected outcome: MCP client points at "
                "`python -m worklane.mcp --author <you>` and can "
                "list / claim / close tickets."
            ),
            status="in_progress",
            priority=2,
            labels=("lane:demo", "area:mcp"),
            comments=(
                (
                    "demo-agent",
                    "Owner: demo-agent\n"
                    "Workdir: /tmp/worklane-demo\n"
                    "Start: 2026-07-01T12:00:00Z\n"
                    "Plan:\n"
                    "- Point editor MCP at the stdio server\n"
                    "- Verify wl_list returns the demo product",
                ),
            ),
        ),
        SeedTicket(
            title="Review demo board copy before public launch",
            description=(
                "Problem: seed ticket titles may read too internal.\n"
                "Expected outcome: titles/descriptions read as host-neutral "
                "WorkLane copy (no host product names)."
            ),
            status="in_review",
            priority=3,
            labels=("lane:demo", "area:docs"),
            comments=(
                (
                    "demo-reviewer",
                    "Owner: demo-reviewer\n"
                    "Workdir: /tmp/worklane-demo\n"
                    "Start: 2026-07-02T09:00:00Z\n"
                    "Plan:\n"
                    "- Soft-locked for copy pass; not yet in_progress",
                ),
            ),
        ),
        SeedTicket(
            title="Ship the first-run quickstart smoke",
            description=(
                "Problem: install docs did not prove board + claim path.\n"
                "Expected outcome: INSTALL / quickstart covers demo seed and "
                "one claim/close loop."
            ),
            status="done",
            priority=3,
            labels=("lane:demo", "area:docs"),
            comments=(
                (
                    "demo-agent",
                    "Completed: Added demo seed + quickstart note so a fresh "
                    "clone's board is non-empty.\n"
                    "Verification: pytest tests/test_demo.py green; opened "
                    "board tab for product demo and saw 4 columns populated.\n"
                    "Links: d0e1f2a (local seed fixture)\n"
                    "Follow-ups: none",
                ),
            ),
        ),
    ]


class DemoError(RuntimeError):
    """User-facing demo bootstrap failure."""


def _normalize_slug(slug: str) -> str:
    s = (slug or "").strip().lower()
    if not s:
        raise DemoError("demo product slug must be non-empty")
    if not s.replace("-", "").replace("_", "").isalnum():
        raise DemoError(
            f"invalid demo product slug {slug!r}: use letters, digits, "
            f"hyphen, underscore only"
        )
    return s


def assert_slug_allowed(slug: str) -> str:
    """Return normalized slug or raise if it targets a protected product."""
    s = _normalize_slug(slug)
    if s in PROTECTED_SLUGS:
        raise DemoError(
            f"refusing to seed protected product {s!r} — demo mode only "
            f"writes the isolated demo store (default slug "
            f"{DEFAULT_DEMO_SLUG!r})"
        )
    return s


def demo_db_path(slug: str = DEFAULT_DEMO_SLUG, data_dir: Optional[Path] = None) -> Path:
    """Path of the demo product SQLite file under the runtime data dir."""
    s = assert_slug_allowed(slug)
    if data_dir is None:
        from worklane.products import wl_data_dir

        data_dir = wl_data_dir()
    return Path(data_dir) / f"{s}.db"


def _sidecar_paths(db_path: Path) -> List[Path]:
    return [db_path, Path(str(db_path) + "-wal"), Path(str(db_path) + "-shm")]


def _wipe_demo_db(db_path: Path) -> None:
    """Delete only the given demo DB and its WAL/SHM sidecars."""
    # Drop SQLiteTracker init cache for this path so a re-seed re-runs schema.
    from worklane.trackers import sqlite as sqlite_mod

    try:
        key = str(db_path.resolve())
    except OSError:
        key = str(db_path)
    sqlite_mod._initialized_dbs.discard(key)

    for p in _sidecar_paths(db_path):
        try:
            if p.exists() or p.is_symlink():
                p.unlink()
        except OSError as exc:
            raise DemoError(f"cannot remove {p}: {exc}") from exc


def seed_tracker(tracker: SQLiteTracker, catalog: Optional[Sequence[SeedTicket]] = None) -> List[str]:
    """Insert seed tickets into an already-bound tracker. Returns created ids."""
    created: List[str] = []
    for item in catalog if catalog is not None else seed_catalog():
        task = tracker.create_task(
            title=item.title,
            description=item.description,
            status=item.status,
            priority=item.priority,
            labels=list(item.labels),
        )
        for author, body in item.comments:
            tracker.add_comment(str(task.id), body, author=author)
        created.append(str(task.id))
    return created


def bootstrap_demo(
    *,
    slug: str = DEFAULT_DEMO_SLUG,
    force: bool = False,
    db_path: Optional[Path] = None,
    data_dir: Optional[Path] = None,
    register_meta: bool = True,
    catalog: Optional[Sequence[SeedTicket]] = None,
) -> Dict[str, Any]:
    """Create/seed the isolated demo product store.

    Returns a report dict: ``slug``, ``db_path``, ``created`` (list of ids),
    ``skipped`` (bool), ``task_count``, ``by_status``.
    """
    s = assert_slug_allowed(slug)
    path = Path(db_path) if db_path is not None else demo_db_path(s, data_dir=data_dir)

    # Extra safety: even with an explicit db_path, refuse protected basenames.
    stem = path.stem.strip().lower()
    if stem in PROTECTED_SLUGS:
        raise DemoError(
            f"refusing demo write to protected store file {path.name!r}"
        )

    catalog = list(catalog) if catalog is not None else seed_catalog()
    expected_statuses = {t.status for t in catalog}

    if force and (path.exists() or any(p.exists() for p in _sidecar_paths(path))):
        _wipe_demo_db(path)

    tracker = SQLiteTracker(db_path=path, product_default=f"product:{s}")
    existing = tracker.list_tasks()
    if existing and not force:
        by_status: Dict[str, int] = {}
        for t in existing:
            by_status[t.status] = by_status.get(t.status, 0) + 1
        return {
            "slug": s,
            "db_path": str(path),
            "created": [],
            "skipped": True,
            "task_count": len(existing),
            "by_status": by_status,
            "message": (
                f"demo store already has {len(existing)} ticket(s); "
                f"pass force=True / --force to wipe and re-seed"
            ),
        }

    # force path already wiped; empty existing proceeds to seed
    if existing and force:
        # Should not happen after wipe; re-wipe if tracker recreated file empty
        # but somehow still has rows (paranoia).
        _wipe_demo_db(path)
        tracker = SQLiteTracker(db_path=path, product_default=f"product:{s}")

    created_ids = seed_tracker(tracker, catalog)
    tasks = tracker.list_tasks()
    by_status = {}
    for t in tasks:
        by_status[t.status] = by_status.get(t.status, 0) + 1

    if register_meta:
        from worklane.products import register_product_meta

        register_product_meta(
            s, display=DEFAULT_DEMO_DISPLAY, prefix=DEFAULT_DEMO_PREFIX
        )

    return {
        "slug": s,
        "db_path": str(path),
        "created": created_ids,
        "skipped": False,
        "task_count": len(tasks),
        "by_status": by_status,
        "expected_statuses": sorted(expected_statuses),
        "message": f"seeded {len(created_ids)} demo ticket(s) into {path}",
    }
