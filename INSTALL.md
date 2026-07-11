# Installing WorkLane in a new host

This walks a new host ("I found WL on GitHub, I want ticket tracking for my
own project") from a bare clone to agents filing and working tickets. It
does not assume tradeOS or any other existing host — see
[README.md](README.md#host-integration--tradeos-profile) for the tradeOS
profile specifically.

## 1. Clone

```bash
git clone <this repo> worklane
cd worklane
```

WL is self-contained: no other repo, service, or database is required.

## 2. Install

```bash
python3 -m venv .venv        # optional but recommended
source .venv/bin/activate
pip install -e .
```

This installs the `worklane` package plus three console scripts:

| Script | Purpose |
| --- | --- |
| `worklane` | starts the FastAPI/uvicorn service |
| `worklane-mcp` | starts the stdio MCP server for agent clients |
| `wl` | host-neutral HTTP CLI (`wl list` / `show` / `comment` / `status` / `label`) — see below |

Requires Python 3.9+ (see `pyproject.toml`).

## 3. Start the service

```bash
python -m worklane.server
# or, after step 2: worklane
```

Default bind is `127.0.0.1:8799`. Override with `TASK_HOST` / `TASK_PORT`.
Runtime state (SQLite stores, logs, pid files) lives under
`worklane/local/` — hidden, gitignored, created on first run.

To keep it running across reboots on macOS without any host repo:

```bash
scripts/install-macos-service.sh install
```

See [README.md#native-startup](README.md#native-startup) for details and
`--python`/`--dry-run` flags.

Verify it's up:

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8799/admin/cockpit   # expect 200
```

## 4. Bootstrap your product

WL is **one SQLite store per product**, auto-discovered — there is no
separate "create a product" step to run. A product appears the moment
either of these happens:

- a ticket is filed with `"surface": "<your-slug>"` (via the API, CLI, or
  MCP `wl_create`) — WL creates
  `worklane/local/data/<your-slug>.db` on first write, or
- you drop a `<your-slug>.db` SQLite file directly into
  `worklane/local/data/`.

Once the store exists, it gets its own Pool tab at
`/admin/tickets/<your-slug>` automatically — no code changes. File your
first ticket to bootstrap it:

```bash
wl comment --help   # confirm the CLI is on PATH first

curl -s -X POST http://localhost:8799/api/admin/tasks \
  -H 'Content-Type: application/json' \
  -d '{"title":"First ticket","description":"Bootstrapping myproduct.","surface":"myproduct","author":"you"}'
```

Give the product a friendlier display name / short id prefix (defaults to
the slug itself) via `/admin/settings`, or by adding an entry to
`worklane/local/config/products.json` — see
`worklane/products.py` for the shape.

## 5. Pick an interface for agents

Three ways to read/write tickets, in order of preference:

1. **MCP** (best for AI agents/editors): point an MCP-capable client at
   `python -m worklane.mcp --author <agent-id>` — see
   [README.md#mcp-server-agent-native-access](README.md#mcp-server-agent-native-access)
   for the full 16-tool catalog and a sample client config.
2. **The `wl` CLI** (best for shell scripts / non-MCP hosts): installed by
   step 2 above.

   ```bash
   export WL_BASE_URL=http://localhost:8799   # default if unset
   export WL_AGENT_ID=you                     # signs comments (PROCESS.md §3.8)

   wl list --product myproduct --status backlog
   wl show wl-1
   wl comment wl-1 "starting work" --author you
   wl status wl-1 in_progress
   wl label wl-1 --add area:backend
   ```

   This CLI only speaks HTTP (`urllib`, stdlib-only) — no
   `worklane` import required on the calling side, so it is safe
   to vendor into a host repo that doesn't want a Python dependency on WL.
3. **Direct HTTP** (for non-Python hosts, or a custom passthrough): see
   [worklane/TRUTH.md](worklane/TRUTH.md#http-surface)
   for the full route list. Every write requires a signed `author` field
   (PROCESS.md §3.8) — unsigned writes are rejected with a 400.

## 6. Write your host's agent docs

Every host that adopts WL is expected to write its own operating profile
— what agent identity to sign as, which working copy to use, what the
verification bar is before closing a ticket. Don't skip this: it's what
keeps multiple agents from clobbering each other's claims.

Start from [HOST_PROFILE_TEMPLATE.md](HOST_PROFILE_TEMPLATE.md), which has
a fill-in-the-blanks PROCESS.md §6-style profile plus a CLAUDE.md snippet.
Concrete worked examples: PROCESS.md §6 (tradeOS), §6.1 (Cursor), §6.2
(Grok), §8 (WL's own self-host lane).

## Read next

- [PROCESS.md](PROCESS.md) — the normative ticket lifecycle/ownership
  rulebook every agent (yours included) follows.
- [worklane/TRUTH.md](worklane/TRUTH.md) — canonical
  entrypoints, runtime layout, full HTTP surface.
- [README.md](README.md) — product overview and the tradeOS worked example
  of a host integration.
