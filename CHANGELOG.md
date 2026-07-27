# Changelog

## v0.1.5 — 2026-07-27

- **Desk board filter chips** for gate class: Ready · For You · Deferred (wl-265).
- **MCP/API deferred first-class** + `wl_list`/`wl_list` gate filter (wl-262).
- **Migration script** human+parked → `gate_type=deferred` (wl-264).
- **Identity registry catch-up** for the 12-persona rename slate (wl-267).

## v0.1.4 — 2026-07-27

- **gate_type=deferred** (wl-261): first-class park — never Ready, never For You gold.
- **Route on create**: auto-stamp `needs:routing` when no `worker:*` label.
- **PROCESS §3.9** three columns: Ready · For You · Deferred (wl-263).

## v0.1.3 — 2026-07-21

Ship surfaces/desk assets in the wheel (wl-230, first-user Homebrew report): package-data now includes desk.css/desk.js/desk-scene.css/desk-scene.js, fixing FileNotFoundError at import on pip/brew installs.

## v0.1.0 — 2026-07-10

Initial public export: the WorkLane protocol (PROTOCOL.md) + reference implementation — FastAPI server with live board, 16-tool MCP server, stdlib wl CLI, per-project SQLite stores. Extracted from production multi-agent use.

All notable public changes. Feedback: open a GitHub issue and include the version.
