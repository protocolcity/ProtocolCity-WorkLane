# Dev Queue (`core/devqueue/`)

**Scope:** Semi-autonomous work-queue dispatch and shutdown protocol for `./tradeos founder`.
**Last verified:** 2026-04-14 — Mode.DEV renamed to Mode.FOUNDER (ADR-020).

## What this module does

The dev work queue is a thin layer over `ProjectTracker` that turns the
local task store (default: `SQLiteTracker`, see `core/trackers/`) into a
prioritized, dependency-aware ready list the dev dashboard can dispatch
in batches. Runtime-tier gating removed (#360); the work queue is accessible
to all users when TRADEOS_TICKETS_BOARD is enabled.

## Files

| File | Purpose |
|------|---------|
| `queue.py` | `WorkQueue`, `parse_blockers`, `find_orphans`, `build_dispatch_prompt` |
| `conflicts.py` | `extract_file_refs`, `group_by_file_conflict` (union-find batching) |
| `shutdown.py` | `run_shutdown` close-out protocol + `ShutdownReport` |
| `__init__.py` | Public surface — what `core.web.routes.dev` imports |

## Lifecycle

```
./tradeos founder
   │
   ▼
GET /founder/dashboard
   │ ── WorkQueue(tracker)
   │ ── orphans()                 ← in_progress carryover banner
   │ ── ready()                   ← prioritized + dep-filtered
   │ ── group_by_file_conflict()  ← batched into terminals
   │
   ▼
Developer copies dispatch prompt → opens new Claude Code → "work SEO-XXX"
   │
   ▼
POST /api/dev/queue/shutdown?apply=1
   │ ── for each in_progress task:
   │       git log --grep=SEO-NNN
   │       tracker.add_comment(closeout)
   │       commits → in_review, no commits → leave in_progress
```

## Design notes

- **No atexit hook.** `./tradeos founder --watch` runs uvicorn with `--reload`, which
  restarts the process on every file change. An atexit shutdown trigger
  would spam closeout comments. The dashboard exposes a manual button +
  the `/api/dev/queue/shutdown` endpoint instead.
- **Blockers parsed from description text**, not a relations graph — the
  SQLite tracker stores `labels` and `description`. The
  parser keys off Markdown headings whose title contains "depends",
  "blocked by", "blockers", or "requires".
- **File conflict detection is lenient.** Path-shaped tokens are matched
  against a small allowlist of source extensions (`.py`, `.md`, `.yaml`,
  `.json`, `.html`, etc.) so prose like "AAPL/SPY pair" doesn't trigger
  a false positive.
- **Unknown blockers count as still blocking.** If a ticket references
  `SEO-9999` and we can't find it in the local tracker, the queue keeps
  the dependent task hidden — safer than dispatching work whose
  prerequisites we can't verify.
- **Shutdown defaults to dry run.** `run_shutdown(tracker)` returns a
  `ShutdownReport` describing the proposed comments and transitions.
  Pass `apply=True` to commit them.

## API surface (dev mode only)

| Endpoint | Returns |
|----------|---------|
| `GET /api/dev/queue/ready` | Prioritized batches with dispatch prompts |
| `GET /api/dev/queue/orphans` | In-progress carryover from prior session |
| `POST /api/dev/queue/shutdown?apply=0\|1` | Close-out report (dry-run unless `apply=1`) |

## Tests

`tests/test_devqueue.py` — 19 unit tests covering blocker parsing,
priority sort, dependency filter, file-conflict union-find, shutdown
dry-run + applied transitions, and git-log fallbacks.

## Open follow-ups

- CLI entry point (`python -m worklane.devqueue shutdown`) for scripted use.
- Close-out comments land in `worklane/local/data/tradeos.db`. Linear is retired;
  legacy `SEO-XXX` ext_ids preserved for historical reference only.
- Fully-autonomous `./tradeos dev --auto` mode that dispatches without
  the approval step. Listed in the SEO-180 spec but deferred until the
  test suite gives enough confidence.
