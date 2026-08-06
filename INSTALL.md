# Installing WorkLane in a new host

This walks a new host ("I found WL on GitHub, I want work order tracking for my
own project") from a bare clone to agents filing and working tickets. It
assumes no existing host.

## Quick install (package / suite dependency)

If WorkLane is delivered as a wheel — Homebrew formula, `pip install`, or
as a suite dependency — no source checkout is required:

```bash
pip install protocolcity-worklane
worklane          # start the API server (http://127.0.0.1:8799)
worklane-mcp      # MCP server for agent clients
wl --help         # ticket CLI (canonical short; worklane is the long form)
```

No source checkout or separate host venv required. Runtime state (SQLite
stores, config) lives under `~/.worklane/`; override with
`WORKLANE_RUNTIME_DIR`. Skip to **§4** to bootstrap your first
project.

---

The sections below walk the source-checkout path — for hosts that want to
pin a specific commit, run the test suite, or contribute to the protocol.

## 1. Clone

```bash
git clone <this repo> worklane
cd worklane
```

WL is self-contained: no other repo, service, or database is required.

## 2. Install

**Source product venv hygiene (wl-398 / pc-1150):** install **only** this
checkout editable (`pip install -e .`). Do **not** also
`pip install -e ../ProtocolCity-WorkLane` (or any export tree) into the same
venv — both claim the top-level package name `worklane`, the export’s editable
finder wins, and `wl` dies with `ModuleNotFoundError: worklane.cli.wl` (export
still ships the old `worklane.cli.wl` module). Export trees are publish
artifacts, never a second live install target beside the product source.


```bash
python3 -m venv .venv        # required for launchd / ./ticketing on this host
source .venv/bin/activate
pip install -e .
```

Package deps are only FastAPI + uvicorn — **no host venv or separate service
is required**.

This installs the `worklane` package plus three console scripts:

| Script | Purpose |
| --- | --- |
| `worklane` | starts the FastAPI/uvicorn service |
| `worklane-mcp` | starts the stdio MCP server for agent clients |
| `wl` | ticket CLI (`wl list` / `show` / `comment` / `status` / `label`) — see below; `worklane` is the long-form alias (`tk` retired 2026-08-03) |

Requires Python 3.9+ (see `pyproject.toml`).

## 3. Start the service

```bash
python -m worklane.server
# or, after step 2: worklane
```

Default bind is `127.0.0.1:8799`. Override with `TASK_HOST` / `TASK_PORT`.
Runtime state (SQLite stores, logs, pid files) lives under
`worklane/local/` — hidden, gitignored, created on first run.

### Optional: seed a demo board (wl-45)

For a first-run board that already has tickets across backlog / in_progress /
in_review / done (so an agent can claim one immediately):

```bash
wl demo
# or seed then start in one shot:
worklane --demo
```

This writes **only** the isolated `demo` project store
(`worklane/local/data/demo.db`). It never touches any other project
store you have configured. Re-run is a no-op unless
you pass `--force` (demo store only). Inspect the seeded tickets: `wl list --product demo`.

To keep it running across reboots on macOS without any host repo:

```bash
scripts/install-macos-service.sh install
```

See [README.md#native-startup](README.md#native-startup) for details and
`--python`/`--dry-run` flags.

Verify it's up:

```bash
curl -s http://localhost:8799/api/admin/products   # expect {"ok":true,"products":[...]}
```

## 4. Bootstrap your project

WL is **one SQLite store per project**, auto-discovered — but the store has
to exist before you can file tickets against it; there is no
create-on-first-write. A project appears the moment either of these
happens:

- you bootstrap it explicitly via `POST /api/admin/products` (wl-12):

  ```bash
  curl -s -X POST http://localhost:8799/api/admin/products \
    -H 'Content-Type: application/json' \
    -d '{"slug":"myproject","display":"My Project","prefix":"mp"}'
  ```

  `slug` is required (lowercase, starts with a letter, `[a-z0-9_-]`, max 40
  chars); `display` and `prefix` are optional (`prefix` must be 2-8
  lowercase alphanumeric characters and not already used by another
  project). This creates `worklane/local/data/myproject.db` and
  registers any given metadata in
  `worklane/local/config/products.json`, or
- you drop a `<your-slug>.db` SQLite file directly into
  `worklane/local/data/`.

Filing a ticket with `"surface": "<your-slug>"` (via the API, CLI, or MCP
`wl_create`) against a slug that hasn't been bootstrapped either way rejects
with an "unknown ticket surface" / "unknown product" error — bootstrap
first, then file.

Once the store exists, it appears in the products registry automatically — no code changes. File your
first work order:

```bash
wl --help   # confirm the CLI is on PATH first

curl -s -X POST http://localhost:8799/api/admin/tasks \
  -H 'Content-Type: application/json' \
  -d '{"title":"First work order","description":"Bootstrapping myproject.","surface":"myproject","author":"you"}'
```

You can rename the display name / short id prefix later too, via
`PATCH /api/admin/products/<slug>` (wl-17) — see
`worklane/products.py` for the on-disk shape of the
`products.json` overlay.

## 5. Pick an interface for agents

Three ways to read/write tickets, in order of preference:

1. **MCP** (best for AI agents/editors): point an MCP-capable client at
   `python -m worklane.mcp --author <agent-id>` — see
   [README.md#mcp-server-agent-native-access](README.md#mcp-server-agent-native-access)
   for the full 16-tool catalog and a sample client config.
2. **The `wl` CLI** (best for shell scripts / non-MCP hosts): installed by
   step 2 above (`worklane` is the long-form alias).

   ```bash
   export WL_BASE_URL=http://localhost:8799   # default if unset
   export WL_AGENT_ID=you                     # signs comments (PROTOCOL.md §3.8)

   wl list --project myproject --status backlog
   wl show mp-1
   wl comment mp-1 "starting work" --author you
   wl status mp-1 in_progress
   wl label mp-1 --add area:backend
   ```

   This CLI only speaks HTTP (`urllib`, stdlib-only) — no
   `worklane` import required on the calling side, so it is safe
   to vendor into a host repo that doesn't want a Python dependency on WL.
3. **Direct HTTP** (for non-Python hosts, or a custom passthrough): the full route list lives in `worklane/task_server.py`. Every write requires a signed `author` field
   (PROTOCOL.md §3.8) — unsigned writes are rejected with a 400.

## 6. Write your host's agent docs

Every host that adopts WL is expected to write its own operating profile
— what agent identity to sign as, which working copy to use, what the
verification bar is before closing a work order. Don't skip this: it's what
keeps multiple agents from clobbering each other's claims.

Start from [HOST_PROFILE_TEMPLATE.md](HOST_PROFILE_TEMPLATE.md), which has
a fill-in-the-blanks PROTOCOL.md §6-style profile plus an AGENTS.md snippet.
See PROTOCOL.md §6 (Host Profiles).

## Read next

- [PROTOCOL.md](PROTOCOL.md) — the normative work order lifecycle/ownership
  rulebook every agent (yours included) follows.
- [README.md](README.md) — product overview, quickstart, MCP setup.
