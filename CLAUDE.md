# WorkLane — Agent Instructions

WorkLane (WL) is a **standalone local-first ticketing product**. It is independent of any host repo. tradeOS is currently one client that consumes it; the protocol makes no assumptions about the host.

Read this file when:

- You've scoped into `worklane/` and need to know what the folder is.
- You're about to change WL's code, schema, API surface, or process rules.
- You're an agent picking up a ticket and want the entry point to the rulebook.

## Reading order

0. **[INSTALL.md](INSTALL.md)** — onboarding a *new* host (clone → install → start → bootstrap a product → pick an agent interface) plus **[HOST_PROFILE_TEMPLATE.md](HOST_PROFILE_TEMPLATE.md)** for writing that host's own PROCESS.md §6-style profile. Start here if WL isn't running yet in your host.
1. **[PROCESS.md](PROCESS.md)** — normative operations rulebook. Lifecycle, ownership markers, comment cadence, closeout contract (`Completed:` / `Verification:` / `Links:` / `Follow-ups:`), auto-transition guards, dependency freeze rules. **Start here for ticket work.**
2. **[TRUTH.md](worklane/TRUTH.md)** — what WL currently *is*: canonical entrypoints, runtime paths, HTTP surface, the boundary with host products.
3. **[README.md](README.md)** — product overview, install, launch, host-integration examples.
4. **[workqueue-coordination-system-design.md](worklane/workqueue-coordination-system-design.md)** — design background, non-normative. Read only if you need the *why* behind a decision.

## Boundary rules

- **WL does not render inside host product pages.** UI lives at WL's own port (default 8799).
- **WL does not depend on host product uptime.** It is a long-lived local service.
- **WL is not a SaaS dependency.** Everything is file-backed SQLite on the local machine.
- **Host products consume WL as a client** — via the CLI, the HTTP API, or an in-tree tracker adapter when the host ships one. WL itself never reaches back into the host.

If a change would cross any of these lines, it needs either an ADR in the host repo's `docs/decisions/` (explaining the coupling) or a design change in `workqueue-coordination-system-design.md`. Don't silently couple.

## Code conventions

WL ships Python and uses FastAPI. When writing WL code:

- Match the host repo's Python version floor. For the tradeOS profile that means Python 3.9+ — use `Optional[X]`, `List[X]`, `Dict[K, V]` from `typing` in signatures and pydantic models; no `X | None` or built-in generics at annotation positions FastAPI evaluates at import time.
- Keep WL importable without the host. `worklane/*` must not `from core.*` or `from <host>.*`. If you need host-specific behavior, hide it behind a profile flag in PROCESS.md §6 (tradeOS Profile) and keep the default path host-neutral.
- Follow the lifecycle contract in PROCESS.md §3 when emitting status changes, including the auto-transition guard semantics (`Owner:`, `Completed:`+`Verification:`, `Blocked:`+`Next step:`).

## Host-specific instructions

Each host that adopts WL owns its own CLAUDE.md and repo docs. Host rules (server management, runtime tiers, supported platforms, etc.) live there, not here. This file is about WL the product — not about what a user of any particular host sees. When working inside tradeOS, read the tradeOS repo root `CLAUDE.md` and follow its pointers.
