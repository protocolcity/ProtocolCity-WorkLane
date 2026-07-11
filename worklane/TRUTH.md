## WorkLane Truth

**Scope:** Standalone ticketing product surface (board/table + APIs), local runtime state, and tradeOS integration boundary.  
**Last verified:** 2026-07-10 (MCP 16-tool surface: wl-19 + wl-31 triage + wl-32 soft-lock + wl-33 pulse; HTTP surface re-checked against `task_server.py`; wl-13 host-neutral `wl` CLI + INSTALL.md added)

### What This Module Is

- A local-first ticketing product consumed by tradeOS as an external service.
- A standalone FastAPI service exposed on `TASK_PORT` (default `8799`).
- A shared ticket backend that can be consumed by multiple local cockpit surfaces.

### What This Module Is Not

- Not rendered inside tradeOS pages.
- Not dependent on tradeOS UI process uptime.
- Not a remote SaaS dependency.

### Canonical Entrypoints

- Service launcher (standalone): `python -m worklane.server` from the WL repo root (`~/Developer/worklane/`).
- Boot persistence (wl-14): `scripts/install-macos-service.sh install` installs `com.worklane.server` as a macOS LaunchAgent (RunAtLoad + KeepAlive-on-crash), running the standalone launcher above — no host repo involved. `uninstall` removes it.
- DB backup (wl-41): `scripts/backup_dbs.sh` runs `sqlite3 .backup` (safe hot-copy, server can stay running) against every discovered product store in `worklane/local/data/*.db` (skips the legacy `ops_tickets` store), writing dated copies to an offsite-synced dir (default `~/Library/Mobile Documents/com~apple~CloudDocs/wl-backups/<store>/`, override via `--dest`/`WL_BACKUP_DIR`) with day-count retention (default 14, `--retention-days`/`WL_BACKUP_RETENTION_DAYS`). `scripts/install-backup-service.sh install` schedules it daily (default 06:45) as `com.worklane.backup`; `uninstall` removes it. Known gap (wl-42): the LaunchAgent path currently fails against iCloud Drive under macOS TCC (no Full Disk Access) — works when run interactively; needs a one-time founder grant to run headless.
- Host-integrated launch: `./tradeos dev` auto-starts WL when `WORKLANE_START_CMD` is set.
- Direct module: `worklane/task_server.py` (main FastAPI app, imported by `server.py`).
- MCP stdio server (wl-19+): `python -m worklane.mcp --author <agent-id>` (or `worklane-mcp`). Tools (16): work lifecycle `wl_list`/`wl_ready`/`wl_show`/`wl_create`/`wl_claim`/`wl_comment`/`wl_close`/`wl_release`; triage `wl_label`/`wl_update`/`wl_cancel`/`wl_reopen`; soft-lock/pulse `wl_reserve`/`wl_park`/`wl_mine`/`wl_counts`. Author required at connect time (`--author` / `WL_AGENT_ID`); default product via `WL_PRODUCT`. No extra deps — JSON-RPC over stdio against product trackers.
- Host-neutral HTTP CLI (wl-13): `wl` console script (`worklane/cli/wl.py`, `python -m worklane.cli.wl`) — stdlib-only (`urllib`), talks only to the HTTP API (`WL_BASE_URL`, default `http://127.0.0.1:8799`), no `worklane` import needed by the caller. Subcommands: `list`/`show`/`comment`/`status`/`label`. Distinct from the legacy `worklane/cli/task.py` (`python -m worklane.cli.task`), which imports the tracker and reads/writes SQLite directly — kept for backward compatibility only.
- New-host onboarding: `INSTALL.md` (clone → install → start → bootstrap a product → pick an agent interface) + `HOST_PROFILE_TEMPLATE.md` (fill-in-the-blanks PROCESS.md §6-style profile + CLAUDE.md snippet).

### Runtime Contracts

- Canonical runtime root: `worklane/local/` (visible + gitignored).
- **One SQLite store per product** (2026-07-10): every `<slug>.db` under
  `worklane/local/data/` is auto-discovered as an independent product
  surface (`worklane/products.py`). Known stores: `tradeos.db`
  (primary host), `worklane.db` (WL tracks itself).
- `ops_tickets.db` is a retired legacy store — ignored by discovery; `o-`
  task-id links still resolve for history.
- Legacy fallback paths are still readable for migration compatibility:
  - `worklane/.local/...` (pre-rename hidden layout)
  - `local/data/tradeos.db` (pre-cord-cut tradeOS-root layout)

### Process Contracts

- `worklane/PROCESS.md` is the normative operations guide for filing, ownership, status transitions, comment cadence, and close criteria (including required `Links:` on completion comments).
- `worklane/workqueue-coordination-system-design.md` is design context; it does not override runtime/process contracts.

### HTTP Surface

- UI:
  - `/admin/cockpit` — landing (root `/` redirects here); includes the live pulse strip (Pulse merged in 2026-07-10, `/admin/pulse` redirects here)
  - `/admin/tickets/{all|<product-slug>}?view=board|table` — the Pool; one surface per discovered product store, `ops` surface retired (redirects to `all`)
  - `/admin/tasks/{task_id}` — ticket detail
  - `/dev/dashboard` — dispatch/queue dev dashboard
- API:
  - `/api/admin/tasks` (GET/POST), `/api/admin/tasks/{task_id}` (GET/PATCH)
  - `/api/admin/tasks/{task_id}/labels`, `/api/admin/tasks/{task_id}/comments` (comments carry an `author` field — PROCESS.md §3.8)
  - `/api/admin/cockpit/summary`, `/api/ops/tickets-health`
  - `/api/dev/tasks`, `/api/dev/activity`, `/api/dev/board-summary`, `/api/dev/queue/*`

Legacy URL aliases (`/admin/tasks`, `/admin/work-queue`, `/admin/products/*`) redirect to the canonical tickets surfaces.

### tradeOS Integration Boundary

Per ADR-028 (supersedes ADR-023): the only permitted coupling is HTTP. No Python imports cross the boundary.

- **Target state:** tradeOS talks to WL exclusively via HTTP (`http://localhost:TASK_PORT/api/*`). No `from worklane.*` in tradeOS code.
- **Resolved (#420):** `core/web/routes/admin_tasks.py` and `core/web/routes/ops_tickets_feed.py` now proxy via `core.clients.wl` (HTTP). No `worklane.*` imports in those files.
- **Resolved (#421):** `./tradeos task …` is now an HTTP passthrough (`deploy/tools/task_http.py`). No Python module calls cross the boundary. ADR-028 §4 fully satisfied.
- tradeOS Dev menu links out to WL UI on `TASK_PORT`.
- tradeOS does not host WL board rendering.

### Dispatch/queue surface (in WL scope)

The work-queue dispatch logic (prioritized ready list, dependency guard, file-conflict batching, session close-out) is a WL concern — it operates on the ticket store and is consumed only by WL's server (`/dev/dashboard`, `/api/dev/queue/*`). It lives at `worklane/devqueue/` (moved from `core/devqueue/` under #348/#351; shim retired under #357).

**Ops accretion audit (2026-04-17, #417):** All items named in ADR-028 as potential creep — devqueue, dispatch logic, stale reaper, groomer, work-queue coordination — are classified as **WL-native ticketing concerns**. The stale reaper and groomer are not yet implemented as code (spec in ADR-019); when built, they belong in WL as ticket-store state management. One genuine ops-creep module was found and deleted: `trackers/ops_sync.py` (ADR-019-era HTTP mirror to a future Ops Cockpit; never wired, disabled by default, dead code).

### Operational Notes

- `./tradeos tickets-install` initializes runtime folders and performs DB migration/copy when needed.
- `./tradeos dev` auto-starts WL when `WORKLANE_START_CMD` is set.
- Direct startup: `python -m worklane.server` from the WL repo root.
