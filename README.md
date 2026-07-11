## WorkLane

WorkLane (WL) is a **local-first ticketing product**. It ships as a standalone service with:

- SQLite-backed ticket storage
- a built-in board/table UI
- JSON APIs for other cockpit surfaces

WL is independent. Host products (like tradeOS) consume WL as clients — they link to WL; they do not render WL's board internally.

## Agent / operator entry points

- **New host adopting WL:** read [INSTALL.md](INSTALL.md) — clone → install → start → bootstrap a product → pick an agent interface, then [HOST_PROFILE_TEMPLATE.md](HOST_PROFILE_TEMPLATE.md) to write your own PROCESS.md §6-style profile.
- **Agents working tickets:** read [PROCESS.md](PROCESS.md) — normative rulebook.
- **Agents changing WL code:** read [CLAUDE.md](CLAUDE.md) — boundary rules + code conventions.
- **Understanding what WL *is* right now:** read [TRUTH.md](TRUTH.md).
- **Design rationale:** read [workqueue-coordination-system-design.md](workqueue-coordination-system-design.md) (non-normative).

## Native startup

The canonical way to run WL, with no host repo involvement:

```bash
# Default port 8799 on 127.0.0.1
python -m worklane.server

# Override host/port
TASK_HOST=0.0.0.0 TASK_PORT=8799 python -m worklane.server
```

The service exposes the UI and API described under [HTTP surface](#http-surface) below.

### Auto-start at login (macOS, host-neutral)

No host repo required — WL can start itself on every login and restart if it crashes:

```bash
scripts/install-macos-service.sh install                 # installs com.worklane.server
scripts/install-macos-service.sh install --python /path/to/python --port 8799
scripts/install-macos-service.sh uninstall
```

This writes `~/Library/LaunchAgents/com.worklane.server.plist` running
`python -m worklane.server`, with logs at
`worklane/local/logs/server.{log,err.log}`. Pass `--python` when the
interpreter with `worklane` installed isn't `python3` on launchd's
minimal `PATH` (e.g. a host venv). `--dry-run` prints the plist without
writing or loading anything.

## Runtime Layout (Hidden by Default)

WL runtime state is hidden and untracked by default under:

- `worklane/local/data/`
  - **One SQLite store per product: `<slug>.db`.** WL discovers every
    `*.db` file here at request time and exposes each as its own Pool
    surface tab (`/admin/tickets/<slug>`), plus a merged read-only "All"
    view. Products stay independent by construction — separate files,
    separate ticket id spaces (composite ids `t-…`, `wl-…`, …).
  - `tradeos.db` — tradeOS product store (always registered)
  - `worklane.db` — WL's own tickets (WL tracks itself)
  - `ops_tickets.db` — legacy retired store, ignored by discovery
  - Display names / short id prefixes for known slugs live in
    `worklane/products.py` (`_KNOWN_PRODUCT_META`); unknown slugs
    get sensible defaults, so a new product needs zero code
- `worklane/local/config/ticketing.env` — local install/runtime flags (for example `TRADEOS_TICKETS_BOARD`)
- `worklane/local/run/` — PID/state files
- `worklane/local/logs/` — WL server log output

Legacy fallback paths are still read for compatibility:

- `worklane/.local/...` (pre-rename hidden layout)
- `local/data/tradeos.db` (pre-cord-cut tradeOS-root layout)

## HTTP Surface

- **UI:**
  - `GET /` — redirects to the Cockpit
  - `GET /admin/cockpit` — landing page: live pulse (metrics strip, in-flight, throughput, activity ticker) + status/priority charts, across all product stores ("Ticketing" brand link in the header)
  - `GET /admin/pulse` — redirects to the Cockpit (Pulse merged into the landing page 2026-07-10: live metrics strip + in-flight cards + throughput on top, breakdown charts below)
  - `GET /admin/tickets/{surface}?view=board|table` — the Pool. `surface` is a first-class path segment: `all` (merged view across every product store) or any discovered product slug (`tradeos`, `worklane`, …). `ops` redirects to `all` (surface retired from the UI)
  - `GET /admin/tasks/{task_id}` — ticket detail: description, labels, comment trail, comment form
  - `GET /dev/dashboard` — dispatch/queue dev dashboard (ready queue, orphans, shutdown)
- **API (admin):**
  - `GET /api/admin/tasks` · `POST /api/admin/tasks`
  - `GET /api/admin/tasks/{task_id}` · `PATCH /api/admin/tasks/{task_id}`
  - `PATCH /api/admin/tasks/{task_id}/labels`
  - `POST /api/admin/tasks/{task_id}/comments` — include `author` (see PROCESS.md §3.8/§5.2)
  - `GET /api/admin/cockpit/summary`
- **API (dev/ops):**
  - `GET /api/dev/tasks` · `GET /api/dev/activity` · `GET /api/dev/board-summary`
  - `GET /api/dev/queue/ready` · `POST /api/dev/queue/dispatch` · `GET /api/dev/queue/orphans` · `POST /api/dev/queue/shutdown`
  - `GET /api/ops/tickets-health`

Legacy route aliases remain for compatibility:

- `/admin/tasks` redirects to Tickets home
- `/admin/work-queue` (+ optional `?product=`) redirects to `/admin/tickets/{all|tradeos}`
- old `/admin/products/*` routes redirect to Tickets home
- `/admin/tickets/ops` redirects to `/admin/tickets/all`

## `wl` CLI (host-neutral HTTP client)

For hosts that don't want an MCP client or a Python dependency on WL, the
`wl` console script (installed by `pip install -e .`, wl-13) is a
stdlib-only HTTP client — no `worklane` import required by the
caller:

```bash
export WL_BASE_URL=http://localhost:8799   # default if unset
export WL_AGENT_ID=<your-agent-id>         # signs comments, PROCESS.md §3.8

wl list --product <slug> --status backlog
wl show <task-id>
wl comment <task-id> "..." --author <your-agent-id>
wl status <task-id> in_progress
wl label <task-id> --add area:backend --remove area:frontend
```

See [INSTALL.md](INSTALL.md) for the full onboarding walkthrough. This is
distinct from `worklane/cli/task.py` (`python -m
worklane.cli.task`), which is the original tradeOS-era CLI that
imports the tracker and reads/writes SQLite directly — it requires the
`worklane` package on the caller's side and is kept only for
backward compatibility.

## MCP server (agent-native access)

Agents can coordinate through WL without CLI wrappers via the stdio MCP server
(`worklane.mcp`, shipped with wl-19):

```bash
# Author identity is required at connect time (PROCESS.md §3.8)
python -m worklane.mcp --author grok
# or: WL_AGENT_ID=grok python -m worklane.mcp
# console script: worklane-mcp --author cursor
```

**Tools (16):**

| Group | Tools |
| --- | --- |
| Work lifecycle | `wl_list` · `wl_ready` · `wl_show` · `wl_create` · `wl_claim` · `wl_comment` · `wl_close` · `wl_release` |
| Triage | `wl_label` · `wl_update` · `wl_cancel` · `wl_reopen` |
| Soft-lock / pulse | `wl_reserve` · `wl_park` · `wl_mine` · `wl_counts` |

Each accepts a `product` param (`tradeos`, `worklane`, or `all` for
list/ready/mine/counts). Composite ids (`t-…`, `wl-…`) work everywhere.

`wl_close` takes structured §5 sections (`completed`, `verification`, `links`,
`follow_ups`) so malformed close-outs are impossible by construction.
`wl_reserve` / `wl_park` cover PROCESS §2 soft-lock without a host CLI.

Example client config (Claude Desktop / Cursor / any MCP host):

```json
{
  "mcpServers": {
    "worklane": {
      "command": "python",
      "args": ["-m", "worklane.mcp", "--author", "cursor"],
      "env": {
        "WL_PRODUCT": "tradeos",
        "WORKLANE_RUNTIME_DIR": "/absolute/path/to/worklane/local"
      }
    }
  }
}
```

No extra Python dependencies — the server speaks JSON-RPC 2.0 over stdio
directly against the product trackers (lifecycle auto-transitions included).

## Environment Overrides

- `WORKLANE_DB` (preferred): override product ticket DB path
- `TRADEOS_TRACKER_DB` (legacy): backward-compatible product DB override
- `OPS_TICKETS_DB`: override ops ticket DB path
- `TASK_HOST` / `TASK_PORT`: WL host/port for UI/API service
- `WORKLANE_RUNTIME_DIR`: override runtime root (default `worklane/local`)
- `WORKLANE_START_CMD`: optional external command used by a host launcher to start WL
- `WL_AGENT_ID`: default author for MCP/CLI signed writes
- `WL_PRODUCT`: default product store for MCP tools when `product` is omitted

---

## Host integration — tradeOS profile

tradeOS is currently the primary host. The following conveniences live in the tradeOS repo; they are **not** part of WL itself:

### First-time install (tradeOS)

From the tradeOS repo root:

```bash
./tradeos tickets-install
```

This command:

1. creates WL runtime folders under `worklane/local/`
2. initializes DB files
3. migrates/copies legacy DB content when needed
4. writes `ticketing.env`

### Host launchers (tradeOS wrappers)

```bash
./ticketing start          # background start
./ticketing status
./ticketing restart
./ticketing stop

# alias
./tickets status

# foreground (single-process / direct logs)
./ticketing serve
```

### tradeOS integration controls

The supported trading app (`./tradeos` / `./tradeos founder`) does **not** enable WL in the shell. When WL is installed, `./tradeos dev` (maintainer path) opens WL board links and can optionally start WL.

To force tradeOS to use an external WL command:

```bash
export WORKLANE_START_CMD="<your ticketing protocol start command>"
```

tradeOS also exposes WL passthrough controls:

```bash
./tradeos wl-status
./tradeos wl-start
./tradeos wl-stop
./tradeos wl-restart
```

### tradeOS integration contract

- tradeOS's `worklane.trackers.*` adapters read/write WL SQLite stores directly (short-term; see ADR-023 for the move to HTTP-only consumption).
- tradeOS Dev menu links to WL UI via `TRADEOS_TP_UI_BASE` (defaults to `http://localhost:${TASK_PORT}`).
- WL can serve board/table even while tradeOS restarts.

Other future hosts adopt WL by defining their own profile — see PROCESS.md §6 (host profiles) for the pattern. On the storage side, adoption is just a new `<slug>.db` in the data dir: WL discovers it and gives the product its own Pool tab automatically.
