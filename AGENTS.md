# WorkLane — Agent Instructions

WorkLane (WL) is a **standalone local-first ticketing product**. It is independent of any host repo. Any city project may connect to the Desk as optional infrastructure; WL makes no assumptions about the host.

This file is the repo's canonical law (ProtocolCity Charter §3 vendor-pointer rule): `CLAUDE.md` and `GROK.md` are thin `@AGENTS.md` forwards — one law, every vendor reads it. **The normative process rules live in [PROTOCOL.md](PROTOCOL.md) — nothing in this file overrides it.**

Read this file when:

- You've scoped into `worklane/` and need to know what the folder is.
- You're about to change WL's code, schema, API surface, or process rules.
- You're an agent picking up a ticket and want the entry point to the rulebook.

## Reading order

0. **[INSTALL.md](INSTALL.md)** — onboarding a *new* host (clone → install → start → bootstrap a project → pick an agent interface) plus **[HOST_PROFILE_TEMPLATE.md](HOST_PROFILE_TEMPLATE.md)** for writing that host's own PROTOCOL.md §6-style profile. Start here if WL isn't running yet in your host.
1. **[PROTOCOL.md](PROTOCOL.md)** — normative operations rulebook. Lifecycle, ownership markers, comment cadence, closeout contract (`Completed:` / `Verification:` / `Links:` / `Follow-ups:`), auto-transition guards, dependency freeze rules. **Start here for ticket work.**
2. **[README.md](README.md)** — product overview, install, launch, host-integration examples.

## Folder map

The law maps the room (pc-111): the ProtocolCity map renders WL's room from
these rows; entries missing here render unmapped.

| Path | What it is |
|---|---|
| `worklane/` | The package — server, board, trackers, MCP, archival; `local/` inside holds runtime state (SQLite stores, config) |
| `docs/` | The records — design docs, decisions, audits |
| `scripts/` | Export/release/backup/migration scripts (the WorkLane export seam lives here) |
| `github.public/` | Public-repo staging material for the WorkLane export |
| `ops/` | Operational glue — service installs, maintenance |
| `tests/` | The proving ground — pytest suite |
| `workers/` | Worker papers — the self-host lane's CONTRACT.md + prompt.md |
| `worklane.egg-info/` | Build metadata from the editable install (generated) |

## Boundary rules

- **WL does not render inside host product pages.** UI lives at WL's own port (default 8799).
- **WL does not depend on host product uptime.** It is a long-lived local service.
- **WL is not a SaaS dependency.** Everything is file-backed SQLite on the local machine.
- **Any city project may connect to WL as a client** — via the CLI or the HTTP API. Connecting is optional city infrastructure; WL never reaches back into the connecting project.

If a change would cross any of these lines, open an issue proposing it first — boundary changes need explicit design sign-off. Don't silently couple.

## Code conventions

WL ships Python and uses FastAPI. When writing WL code:

- Python 3.9+ is the version floor — use `Optional[X]`, `List[X]`, `Dict[K, V]` from `typing` in signatures and pydantic models; no `X | None` or built-in generics at annotation positions FastAPI evaluates at import time.
- Keep WL importable without the host. `worklane/*` must not `from core.*` or `from <host>.*`. If you need host-specific behavior, hide it behind a host profile flag (PROTOCOL.md §6) and keep the default path host-neutral.
- Follow the lifecycle contract in PROTOCOL.md §3 when emitting status changes, including the auto-transition guard semantics (`Owner:`, `Completed:`+`Verification:`, `Blocked:`+`Next step:`).
- This is a shared checkout — other agent lanes and the founder's own terminal may have unrelated uncommitted edits in the working copy at any time. Never `git add -A` / `git add .` / `git commit -a`; stage only the files your own ticket touched, by explicit path (PROTOCOL.md §5.1).

## Host-specific instructions

Each host that adopts WL owns its own AGENTS.md and repo docs (vendor files like CLAUDE.md are pointers to it). Host rules (server management, runtime tiers, supported platforms, etc.) live there, not here. This file is about WL the product — not about what a user of any particular host sees. When working inside a host repo, read that repo's own agent instructions first.

