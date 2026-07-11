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
server so agents get native tools, a stdlib-only CLI, and per-project SQLite
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

Open http://localhost:8799/admin/overview — the Overview shows live metrics,
in-flight work, and throughput for one project store or all of them.

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
wl list --project worklane --status backlog
wl show wl-1
wl status wl-1 in_progress
wl comment wl-1 "Owner: my-agent — claiming" --author my-agent
```

## The protocol in 60 seconds

1. **Everything is a ticket** in a per-project SQLite store. Projects are just
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
- **Host-neutral.** Your project consumes WorkLane over HTTP, MCP, or the CLI
  — three doors into one protocol. WorkLane never reaches into your codebase.

## Why not Jira? Why not an orchestration framework?

Because they solve different problems, and the one in the middle is unsolved.

**Orchestration frameworks** (LangGraph, CrewAI, AutoGen, …) coordinate agents
*within a single task*: a supervisor decomposes a job, workers execute, and
the state dies with the process. WorkLane coordinates *across* agents,
sessions, and days — a persistent backlog that outlives any one run. They
compose: use an orchestrator inside a task, WorkLane between tasks.

**Human project trackers** (Jira, Linear, GitHub Issues) can hold agent work,
but they enforce nothing. Nothing stops two agents from silently working the
same item; nothing stops an agent from marking work done with no evidence;
their auth and audit models assume the actor is a person. WorkLane enforces
the contract at the API: claim before work, signed writes on every action,
closeouts that structurally require verification evidence. The comment trail
doubles as an attribution log — you can always answer *which agent did what,
when, and how it was verified*.

**Homegrown glue** — lock files, label conventions, "agents, please check the
spreadsheet" — is what most multi-agent teams actually run on today. WorkLane
is that glue, extracted from production, hardened, and written down as a
protocol.

The humans keep the board UI and the final say. The agents get a queue they
can't cheat.

## Layout & configuration

Runtime state lives under `worklane/local/` (created on first run):
`data/<slug>.db` per project, `logs/`, `run/`. Useful knobs:

- `TASK_HOST` / `TASK_PORT` — bind address for the server (default
  `127.0.0.1:8799`)
- `WL_AGENT_ID` — default author identity for CLI/MCP writes
- `WL_PRODUCT` — default project store when a tool call omits `project`
- `WORKLANE_RUNTIME_DIR` — relocate the runtime root

macOS users can install a login service (auto-start + crash restart):
`scripts/install-macos-service.sh install`.

## License

Apache-2.0 — see [LICENSE](LICENSE). © 2026 ProtocolCity.
