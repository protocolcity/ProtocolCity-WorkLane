# Changelog

## v0.1.6 — 2026-08-03

Headline for host cities on PyPI: **route-event wake nudge** so hands pick up
routed work in seconds instead of waiting for the next clock.

- **Route-event WorkForce wake** (wl-359): on create / label / release / reopen /
  gate-clear for a dispatchable `worker:<hand>` seat, fire-and-forget `POST`
  WorkForce `/api/wake` so scheduled hands run within seconds. Skips
  `worker:you` and gated tickets; debounces per hand; never fails the ticket
  mutation if wake is down. Pair with a WorkForce that exposes `/api/wake`
  (wf-149 sibling).
- **Hard-B create routing** (wl-274 / wl-275 / wl-322 / wl-315 / wl-320): when
  hands are hired, create requires exactly one `worker:*` seat; bare
  `worker:you` is rejected (starve guard); label mutations that strip the last
  seat re-stamp `needs:routing`; import paths stamp `needs:routing` rather than
  landing silently bare (wl-338 partial).
- **Cross-store seat guard** (wl-296): create/label-add warns (or hard-rejects
  with `WL_WORKER_PRODUCT_HARD_REJECT=1`) when a `worker:<id>` roster seat is
  for a different product store.
- **Umbrella epic discipline** (wl-297 / wl-347): ready feed excludes
  `umbrella`-labeled tickets; close-path enforces child-coverage when a
  structured Children inventory is present.
- **Store addressing** (wl-344): bare-id write paths require `project=` so
  multi-store boards cannot silently mis-address.
- **`tk` fully retired** (wl-342 / wl-326 / wl-325): public console scripts are
  `worklane` / `worklane-mcp` / `wl` only — do not re-add `tk`.
- **`wl` CLI binary retired** (wl-384): private installs no longer ship a silent
  `wl` console alias of the real CLI; optional deprecation shim prints the `wl`
  equivalent and exits nonzero. `wl --help` brands as `usage: wl`.
- **Done notifications** (wl-302): optional ntfy hook when a work order closes.
- **SSE payloads** (wl-348): events carry store + composite `task_id`; unscoped
  aggregates no longer drop identity.
- **Hot-path resilience** (wl-357 / wl-353 / wl-354 / pc-881): retry transient
  SQLite disk I/O on hot reads; slim attention seeds; SQL status counts for
  admin list; short scene/attention caches to stop Map thrash.
- **Env dual-accept** (wl-279): prefer `WL_*`; still read the pre-rename
  product-prefix env names during the dual-accept window.
- **Citizen process one-pager** + work-order vocabulary on surfaces (wl-314 /
  wl-294); For You stays scarce — bare `in_review` is not Map gold.

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
