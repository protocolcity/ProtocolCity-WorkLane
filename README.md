# WorkLane

**A local-first work queue for multi-agent teams — the coordination layer that
keeps your AI agents from stepping on each other.**

You have Claude Code working your backlog. Then you add Cursor. Then a
scheduled agent that runs every hour. Suddenly two agents are editing the same
file, a third closes a ticket nobody verified, and you can't reconstruct who
did what. WorkLane is the fix: a shared ticket queue with a **claim protocol**
(reserve before you touch anything), **lanes** (route work by agent
capability), and a **closeout contract** (no "done" without stating what
shipped and how it was verified) — all on your machine, in SQLite, with no
cloud dependency.

WorkLane is a **protocol plus a reference implementation**. The protocol
([PROTOCOL.md](PROTOCOL.md)) defines the lifecycle, ownership markers, and
closeout format any compliant agent follows. The implementation ships
everything you need to run it: a FastAPI server with a live board UI, an MCP
server so agents get native tools, a stdlib-only CLI, and per-product SQLite
stores.

It isn't a demo. It was extracted from a working system where a scheduled
Claude Code pool, founder-driven terminals, Cursor, and Grok have worked a
shared backlog daily for months.

## Quickstart

```bash
git clone https://github.com/protocolcity/worklane && cd worklane
pip install -e .

worklane                     # server + board UI on http://127.0.0.1:8799
```

Open http://localhost:8799/admin/cockpit — the cockpit shows live pulse,
in-flight work, and throughput across every product store.

File your first ticket:

```bash
curl -X POST localhost:8799/api/admin/tasks \
  -H "Content-Type: application/json" \
  -d '{"surface": "worklane", "author": "founder",
       "title": "Try WorkLane",
       "description": "Problem: my agents collide. Outcome: they stop."}'
```

Every write is signed (`author` is required — the protocol has no anonymous
actions), and every ticket needs a real problem statement by construction.

## Give your agents tools (MCP)

Agents coordinate through the stdio MCP server — 16 tools, no extra
dependencies:

```bash
claude mcp add worklane -- python -m worklane.mcp --author claude
```

or in any MCP host config (Claude Code / Claude Desktop / Cursor / …):

```json
{
  "mcpServers": {
    "worklane": {
      "command": "python",
      "args": ["-m", "worklane.mcp", "--author", "cursor"]
    }
  }
}
```

| Group | Tools |
| --- | --- |
| Work lifecycle | `wl_list` · `wl_ready` · `wl_show` · `wl_create` · `wl_claim` · `wl_comment` · `wl_close` · `wl_release` |
| Triage | `wl_label` · `wl_update` · `wl_cancel` · `wl_reopen` |
| Soft-lock / pulse | `wl_reserve` · `wl_park` · `wl_mine` · `wl_counts` |

`wl_close` takes structured closeout sections (`completed`, `verification`,
`links`, `follow_ups`) — a malformed closeout is impossible by construction.

There's also `wl`, a stdlib-only CLI for agents and scripts that shouldn't
import anything:

```bash
export WL_AGENT_ID=my-agent
wl list --product worklane --status backlog
wl show wl-1
wl status wl-1 in_progress
wl comment wl-1 "Owner: my-agent — claiming" --author my-agent
```

## The protocol in 60 seconds

1. **Everything is a ticket** in a per-product SQLite store. Products are just
   `<slug>.db` files — drop in a new one and it gets its own board tab, its
   own ticket id space (`wl-12`, `myapp-3`), zero code.
2. **Claim before work.** An agent moves a ticket to `in_review` and posts an
   `Owner:` comment before touching a file. Two agents can never silently work
   the same ticket.
3. **Lanes route by capability.** Label tickets `lane:<agent>` and each agent
   scans only its lane — small mechanical fixes to a lightweight agent,
   architectural work to a stronger one, judgment calls to a human.
4. **Closeouts are contracts.** Done requires `Completed:` + `Verification:`
   (with evidence), or the auto-transition guards bounce it back.
5. **Every action is signed.** Comments and writes carry an agent identity;
   the trail is the audit log.

The full normative rulebook is [PROTOCOL.md](PROTOCOL.md). To onboard your own
project and write per-agent profiles, start at [INSTALL.md](INSTALL.md) and
[HOST_PROFILE_TEMPLATE.md](HOST_PROFILE_TEMPLATE.md).

## What it is / what it isn't

- **Local-first, file-backed.** One machine, SQLite, no accounts, no cloud.
  Backup = copy the `.db` files.
- **Host-neutral.** Your product consumes WorkLane over HTTP, MCP, or the CLI.
  WorkLane never reaches into your codebase.
- **Not a SaaS, not a Jira replacement for humans.** It's the coordination
  substrate for the agents doing your work — the humans get the board UI and
  the final say.

## Layout & configuration

Runtime state lives under `worklane/local/` (created on first run):
`data/<slug>.db` per product, `logs/`, `run/`. Useful knobs:

- `TASK_HOST` / `TASK_PORT` — bind address for the server (default
  `127.0.0.1:8799`)
- `WL_AGENT_ID` — default author identity for CLI/MCP writes
- `WL_PRODUCT` — default product store when a tool call omits `product`
- `WORKLANE_RUNTIME_DIR` — relocate the runtime root

macOS users can install a login service (auto-start + crash restart):
`scripts/install-macos-service.sh install`.

## License

Apache-2.0 — see [LICENSE](LICENSE). © 2026 ProtocolCity.
