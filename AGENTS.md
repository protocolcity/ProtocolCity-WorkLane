# WorkLane — Project instructions (L1 CORE)

**Product brand:** **WorkLane** (queue / work orders for BluePrint cities).  
**Wire / package:** Python package folder `worklane/` (renamed from
`worklane/` — wl-280, 2026-08-05; untracked local symlink shim +
dual-window console-script aliases retire after one release).
**Citizen and Map glass say WorkLane**. Store slug `worklane`
(legacy `worklane` still aliases) · prefix **`wl-`** (legacy `wl-`
forever).

Standalone **local-first** work-order engine. Independent of any host repo.
Any workspace project may connect to the Desk as optional infrastructure;
WorkLane makes no assumptions about the host.

This file is the repo's canonical law (ProtocolCity Charter §3 vendor-pointer
rule): `CLAUDE.md` and `GROK.md` are thin `@AGENTS.md` forwards — one law,
every vendor. **Normative process:** [PROTOCOL.md](PROTOCOL.md) — nothing here
overrides it. **City loop (short):** workspace CORE + ProtocolCity
`docs/specs/ALWAYS_WORK_PROTOCOL.md` (author You · seat hand · gold only on
true blocker).

Read this file when:

- You've scoped into `worklane/` and need to know what the folder is.
- You're about to change WorkLane code, schema, API surface, or process rules.
- You're an agent picking up a `wl-*` / `wl-*` ticket and want the entry point.

## Reading order

0. **[INSTALL.md](INSTALL.md)** — onboarding a *new* host (clone → install → start → bootstrap a project → pick an agent interface) plus **[HOST_PROFILE_TEMPLATE.md](HOST_PROFILE_TEMPLATE.md)** for writing that host's own PROTOCOL.md §6-style profile. Start here if Desk isn't running yet.
1. **[PROTOCOL.md](PROTOCOL.md)** — normative operations rulebook. Lifecycle, ownership markers, comment cadence, closeout contract (`Completed:` / `Verification:` / `Links:` / `Follow-ups:`), auto-transition guards, dependency freeze rules. **Start here for ticket engine work.**
2. **[README.md](README.md)** — product overview, install, launch, host-integration examples.

## Folder map

The law maps the room (pc-111): the ProtocolCity map renders WL's room from
these rows; entries missing here render unmapped.

| Path | What it is |
|---|---|
| `ARCHITECTURE.md` | Project architecture paper (L1) — layers, SoT, invariants; anchors the package paper |
| `worklane/` | **Package path** — server, board, trackers, MCP, archival; `local/` runtime state |
| `docs/` | The records — design docs, decisions, audits |
| `scripts/` | Export/release/backup/migration scripts (the WorkLane export seam lives here) |
| `github.public/` | Public-repo staging material for the WorkLane export |
| `ops/` | Operational glue — service installs, maintenance |
| `tests/` | The proving ground — pytest suite |
| `workers/` | Worker papers — the self-host lane's CONTRACT.md + prompt.md |
| `worklane.egg-info/` | Build metadata from the editable install (generated) |

## Boundary rules

- **WorkLane does not render inside host product pages.** REST API at port 8799 (API-only); glass is BluePrint suite `:8801`.
- **Does not depend on host product uptime.** Long-lived local service.
- **Not a SaaS dependency.** File-backed SQLite on the local machine.
- **Any workspace project may connect as a client** — CLI or HTTP. Optional infrastructure; WorkLane never reaches back into the connecting project.

If a change would cross any of these lines, open an issue proposing it first — boundary changes need explicit design sign-off. Don't silently couple.

## Code conventions

WorkLane ships Python + FastAPI (package import path `worklane`).

- Match the host Python floor (3.9+ minimum): `Optional[X]`, `List[X]` — no PEP 604 union syntax.
- Keep the package importable without the host. `worklane/*` must not
  `from core.*` or `from <host>.*`.
- Follow PROTOCOL.md §3 lifecycle (`Owner:`, `Completed:`+`Verification:`,
  `Blocked:`+`Next step:`).
- Shared checkout — never `git add -A` / `git commit -a`; stage only your paths
  (PROTOCOL.md §5.1).

## Host-specific instructions

Each host owns its own workspace `AGENTS.md`. Host rules live there. This file
is about WorkLane the product. When working inside another project, read that
project’s L1 `AGENTS.md` too.

