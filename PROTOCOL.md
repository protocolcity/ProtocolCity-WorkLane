# WorkLane Process Guide

Operational rulebook for how agents and humans work tickets in WL. System design rationale lives in `workqueue-coordination-system-design.md`.

## 1) Core Model

Four statuses, each with one meaning:

- **`backlog`** — free pool. Fair game for any agent.
- **`in_review`** — soft-lock. Off the pool, held by someone (reading, parked, bundled). Other agents skip.
- **`in_progress`** — the live ticket. Exactly one per agent; code is actively being written here.
- **`done`** — complete and verified.

(`canceled` exists for intentionally dropped work.)

**The pool must stay alive.** If you pull something out of `backlog` and don't end up working it, put it right back.

## 2) Agent Flow

1. **Scan** `backlog` (priority + age).
2. **Reserve** the candidate → move it to `in_review`. Stops another agent from grabbing it mid-read.
3. **Bundle** — scan for related tickets (same surface, linked via `Depends on #NNN`, overlapping area). Park them to `in_review` too.
4. **Decide:**
   - Working it → promote one to `in_progress` and start. Siblings stay parked.
   - Not working it → release the whole group back to `backlog`.
5. **Rotate** — when the live ticket closes, promote the next sibling from `in_review` to `in_progress`. Repeat until the bundle is done.
6. **Close** via completion comment (`Completed:` / `Verification:` / `Links:` / `Follow-ups:`). Status auto-moves to `done`.
7. **Blocker** — post `Blocked:` + `Next step:`. Status auto-moves back to `backlog`.

Abstract commands — **MCP first** (see §6). CLI remains a legacy shell fallback:

| Step | MCP tool | Legacy CLI |
| --- | --- | --- |
| Scan ready | `wl_ready` / `wl_list` | `<host-cli> task list --status backlog` |
| Soft-lock reserve | `wl_reserve` | `<host-cli> task status NNN in_review` |
| Claim live | `wl_claim` (atomic reserve+promote + Owner marker) | status → in_progress + Owner comment |
| Park (bundle rotate) | `wl_park` | `<host-cli> task status NNN in_review` |
| Close | `wl_close` (structured §5 sections) | comment with Completed/Verification/Links |
| Release / block | `wl_release` | Blocked: + Next step: comment |
| Session pulse | `wl_mine` · `wl_counts` | list + counts |

### 2.1) Coordination model: workers

Multi-agent routing on WL is a negative-space default, not an explicit gate. This underpins the "Scan" step above and every per-agent profile in §6 — stated here once instead of left implicit across those profiles.

- **WorkForce scheduled hands drain labeled feeds only (BluePrint / roster law).**
  Each hand's `queue_url` filters `worker:<id>`. Unlabeled ready is **not**
  drained by cron — it shows as Map “No hand” / `needs:routing` until routed.
  **Create path (MCP / CLI / HTTP) — hard B (wl-274, 2026-07-28):** when the
  product has ≥1 hired **lane** hand, create **requires** exactly one
  `worker:*` seat (`worker:<persona>` or **`worker:you`** personal seat).
  Omit seat → create **rejected** with an error listing valid seats (never
  invent a hand). **Pre-hire** (no hired lanes): auto-stamp `needs:routing`
  so silence cannot hide unrouted work. Never two `worker:*` labels. Area
  tags are not routing. Grammar: ProtocolCity
  `docs/specs/WORK_ORDER_LABELS.md`.
- **Unlabeled historical note (pre-roster exclusivity).** Older PROCESS text
  treated unlabeled as “default worker pool scans full backlog.” That remains
  true only for **manual** `wl_ready` without a label filter — **not** for
  WorkForce launchd/schedule drains. New cities: always route.
- **`worker:*` labels are the exclusive hand feed.** A `worker:nala` or
  `worker:felix` label places the ticket on that hand's schedule. Do not put two
  `worker:*` labels on one ticket.
- **>24h no-activity demotion.** A `worker:*` label with no activity for more than 24 hours is treated as stale: the default-worker pool may pick the ticket up as if it were unlabeled. Narrower-scope agents don't need to strip the label themselves — the pool's own scan handles the fallback.
- **Why labels aren't mandatory.** Requiring a worker label on every ticket would turn the label into a routing gate: a fresh, unlabeled ticket would be invisible to every agent until someone triaged it, adding a failure mode where tickets belong to nobody and rot. The unlabeled-default guarantees every ticket always has an owner-of-last-resort. (Ratified as DECISION (recommendation-default) 2026-07-11, wl-53 — founder may veto.)

This is deliberately a fail-safe, not a strict routing table: per-agent scan filters (Nala's `--label worker:nala`, Felix's `--label worker:felix`) are narrowings of the default-worker pool's scan, never replacements for it.

One addendum (ratified 2026-07-11): a narrower-scope agent whose profile defines **objective, mechanically checkable self-service criteria** (§6.2 Grok/Felix) may, when its queue is empty, take an unlabeled ticket that passes every criterion — self-labeling it first so the triage decision is recorded on the ticket. Self-service never changes ownership defaults: unlabeled tickets still belong to the default-worker pool, and an agent without a self-service clause in its profile (e.g. Cursor/Nala, §6.1) has none.

## 3) Rules

1. **No orphan work** — any TODO, gap, fix, or refactor starts with a ticket.
1b. **Entry/host chat that implements is not exempt** (wl-273 / pc-589) —
    interactive sessions (Claude / Grok / Cursor / …) that **change the
    system** must create or claim a work order as they go. Pure advice is
    free; shipping code/docs/config without a ticket id is a process
    violation. Host-as-hand claims as **you** (or the citizen id). Coord
    still must not steal `worker:*` feeds when hands are armed.
2. **Single live owner** — `in_progress` is one ticket per agent at a time.
3. **Status is truth** — ticket status matches actual work state.
4. **Comment trail** — blockers, decisions, and completion evidence go in comments.
5. **Close with links** — the completion comment must include at least one navigable reference (PR URL, merge commit, doc path).
6. **Declare dependencies** — use `Depends on #NNN` in the description so the queue guard can freeze siblings.
7. **Recommendation-default decisions** (founder-ratified 2026-07-09) — when a ticket hits a decision point, the agent records its recommendation as the decision (`DECISION (recommendation-default): <choice> — <why>` comment) and keeps working; the founder reviews and can veto after the fact. `needs:founder-decision` is reserved for the escalation class only: real-money gates (LIVE flips, risk-limit widening, new broker/credential enablement, moving money, gate bypasses), reversals of ratified ADRs/product direction, and public-facing or expensive-to-reverse actions. Everything else — including strategy-intent on paper/bench plays and exposure-reducing enforcement — proceeds on the recommendation. Decisions must be logged in ticket comments so the veto window is real.
7a. **File = decided** (BluePrint product law, 2026-07-28) — when **You** file a work order (ordinary backlog, not a `FOUNDER ·` publish/gate ticket), that **is** the decision. Hands and coord sessions **work it** — they do not re-ask for permission, re-open design debate, or park as “waiting for You to pick.” Route with `worker:<id>` so schedules drain. **For You** remains scarce (rule 9): roadblocks only. This is expected behavior in every WorkLane-backed city, not host-private air traffic.
8. **Sign every comment** (2026-07-10) — pass the author flag (`--author "<agent-id>"` on the CLI, `author` on the API) on every comment you post, using your canonical agent id from §5.2. The `Owner:` line inside the body documents the claim; the author *field* is what the board byline, filters, and ghost-audits key on. The two must carry the same id. An unsigned (empty-author) comment is a process violation, not a default.
9. **Gate classes — Ready · For You · Deferred** (engine wl-261, 2026-07-27; For You law wl-257, 2026-07-16) — Three attention/ready classes govern how a ticket surfaces to You:

   | Class | How to set | Attention / Map gold | Ready pool |
   |---|---|---|---|
   | **Ready** | no gate | — | ✓ claimable |
   | **For You** | `gate_type=human`, action-shaped note | ✓ In-tray / Map gold | ✗ blocked |
   | **Deferred** | `gate_type=deferred` | — | ✗ blocked |

   - **Park with `gate_type=deferred`.** To withhold a ticket from ready without painting You: `PATCH gate_type=deferred`. No special note prefix needed. Ready stays blocked; attention / Map gold skip entirely. This is the modern park encoding, superseding the legacy `gate_type=human` + parked `gate_note` workaround for *new* parks (wl-257 dual-read window preserved for existing parked-note gates; see wl-264 for bulk migration).
   - **`gate_type=human` is scarce — act-now only.** Reserve it for tickets where You must act *now* (decide / clear / approve). Every human gate needs a concrete `gate_note` naming what You must decide and what clears it. Legacy human+parked-note gates (`gate_note` starts with `deferred:` / `umbrella`, or includes `post-northstar` / `not claimable` / `withheld from ready` / `parked:`) still block ready without attention paint — preserved during the migration window; prefer `gate_type=deferred` for new parks.
   - **Umbrella epics:** `gate_type=deferred` + the `umbrella` label. No human gate unless You are needed today.
   - **Deferred gates thaw freely.** Any agent or founder may clear a deferred gate (`PATCH gate_type=null`) when the track reopens. Human gates still require founder-present to clear (§5.2.1).
   - **No bulk sweeps.** Agents must not set any gate type on more than **three** tickets in a single shift unless a ticket they hold explicitly authorizes a named bulk re-gate (ids listed). Mass “park the pool so I look unwedged” is an automatic reject.
   - **Do not re-gate the already gated.** Leave existing gates; comment if the note is wrong.
   - **Muted = snooze, not a gate.** You may snooze a product, kind, task, or all on Waiting on You to mute attention for a day without changing store gates. Snooze scopes: `product | kind | task | all`. Mechanical enforcement: **wl-205** (product/kind/all); **wl-251** adds per-ticket (`task`) scope. Snooze is a UI silence only — it does not block ready or change gate state.
   - **Migration (legacy human+parked → deferred):** `PATCH gate_type=deferred`, drop or repurpose the `gate_note`. Use wl-264's batch script to convert the full pool.
10. **No cancel without shipping · no mass cancel** (2026-07-26, founder — empty-BL thrash) —
    - **`wl_cancel` / cancel is rare.** Allowed only when work is **intentionally abandoned as product truth** (duplicate, wrong product forever, explicit founder “drop this”) — **not** as a way to make the board look empty.
    - **Never cancel to “clear the queue”** or “empty the backlog” before a ship/export. “Empty the BL” means **implement and close**, or **leave open** what still needs work — **not** mass-cancel deferred epics.
    - **Never cancel without the requested functionality shipping** (or a founder-explicit drop). If the slice is incomplete: leave `backlog` / `in_progress`, post `Blocked:` + `Next step:`, or close only the **completed child** and keep the parent open.
    - **Mass cancel forbidden** unless You **explicitly** order it (named ids or “cancel all of X”). One cancel needs a one-ticket rationale; bulk needs explicit founder language.
    - **Wrong cancel → reopen.** If an agent mass-canceled without that order, reopen and restore the board.
11. **Sticky residual work · board is shared memory** (2026-07-26, founder — invisible close-outs) —
    The work-order board is how You and agents coordinate. **Closing a ticket hides the work.** Residual work that still needs a return visit must remain **visible as open tickets**, not only as prose in a `Completed:` or `Follow-ups:` note.
    - **`Follow-ups: none` means none.** Not “tabled in my head,” not “hard-stops listed in the close comment,” not “re-file later.”
    - **If residual work exists at close:** either (a) **keep the parent open** and comment progress, or (b) **file child tickets first** (imperative titles, parent linked with `blocks:parent` / body “Parent: **id**”), list those ids under `Follow-ups:`, **then** close only the slice that actually shipped.
    - **Never close a parent epic** while known residual children are still unfinished **unless** those children are **already open on the board**. Invisible residuals are a process violation (same class as empty-BL mass cancel: board looks clean, work is gone).
    - **You table explicitly.** Only You park work permanently (cancel with founder order, or a child left open under a “tabled” label). Agents do not invent “tabled” as a close-out substitute.

## 4) Transitions

Allowed moves:

- `backlog → in_review` (reserve)
- `in_review → in_progress` (promote to live)
- `in_review → backlog` (release back to pool)
- `in_progress → in_review` (park; rotate to a sibling)
- `in_progress → backlog` (abandon via `Blocked:` comment)
- `in_progress → done` (complete via `Completed:` comment)
- `in_review → done` (bundled completion, rare)
- `* → canceled` (with rationale — **§3 rule 10**: rare; never mass-cancel / never cancel to empty the board without founder order)

Auto-transitions (lifecycle guard):

- `Completed:` + `Verification:` comment on `in_progress`/`in_review` → `done`.
- `Blocked:` + `Next step:` comment on `in_progress`/`in_review` → `backlog`.
- Dependency guard: a ticket whose declared blockers aren't all `done` stays in `in_review` when promoted; auto-thaws to `backlog` once blockers clear.
- The UI (header badges + the Overview's Attention panel) flags `in_progress`/`in_review` tickets as stalled after 90 min without updates.

## 5) Intake and Closeout

**Intake** — concise imperative title, clear problem, expected outcome (with the kind of link expected at close), `area:*` / `sys:*` labels, priority (`1` urgent → `4` low). If follow-up work surfaces mid-ticket, file a child immediately and link it in a comment on the parent.

Enforced at the API: creation requires a signed
`author` (§3.8 applies to every write, not just comments) and a non-empty
`description`. The server records the filer as a signed `Intake: filed by
<agent-id>` comment — tickets have no creator column; the comment trail is
the record. There is no UI create form: tickets enter via agents (API / CLI
/ MCP), by design.

**Ownership marker** (post when reserving or promoting; sign it per §3.8):

```text
Owner: <agent-id> (<model>)
Workdir: <absolute path of the working copy>
Branch: <branch — omit this line when working directly on main>
Start: <iso>
Plan:
- bullet
- bullet
```

Marker rules (settled 2026-07-10; before this date the field drifted per agent —
`Worktree:` vs `Workdir:` vs `Worktree/branch:`):

- The working-copy line is always **`Workdir:`**. If the working copy is a git
  worktree, its absolute path goes in `Workdir:` and its branch in `Branch:` —
  there is no separate `Worktree:` field.
- The `(<model>)` parenthetical is optional but encouraged for agents that can
  run on different models (e.g. `Owner: claude-worklane (claude-fable-5)`). The model
  goes **only** here — never in the comment author field.

**Completion comment:**

```text
Completed:
- <files/surfaces changed>

Verification:
- <commands/tests run + result>

Links:
- <PR, commit, or repo-relative path>

Follow-ups:
- <ticket refs or "none">
```

All four sections are mandatory, for **every** agent — narrative closers
included. A prose headline may follow `Completed:` on the same line, but
`Verification:` and `Links:` must still appear as their own literal sections:
the lifecycle guard (§4) and the close-with-links rule (§3.5) key on those
exact tokens. `Links:` needs at least one navigable reference (PR URL, commit
SHA, or repo-relative path); a merge SHA buried in prose ("Merged: abc1234 …")
does not satisfy it. `Follow-ups:` may be "none" **only when no residual
work remains** (§3 rule 11). If more work is known, list open ticket ids
(file them first if needed) — never prose-only “tabled / later / hard-stops.”

**Blocked comment:**

```text
Blocked: <cause>
Next step: <what's needed>
```

### 5.1) Close-out guard — commit-or-abandon (all agents)

Uncommitted work in an abandoned working copy is how finished fixes get destroyed or silently stranded off `main` while the board says done
(seen in production). Every close-out — success or blocked — passes this guard:

1. **Inspect before leaving.** Before removing a worktree, switching away from a
   working copy, or ending a session: `git status --porcelain` and
   `git log main..<branch> --oneline`. Never `worktree remove --force` (or
   discard edits) without looking first.
2. **Commit-or-abandon, explicitly.** WIP worth keeping → commit it
   (`WIP #NNN: <state — what's done, what's not>`), push the branch, and name it
   in the ticket comment so a future claim resumes from it. Dead ends → abandon
   deliberately and say so in the comment ("edits abandoned deliberately").
   Silence is not a disposition.
3. **`done` means reachable from `origin/main` (amended 2026-07-16, wl-212).**
   Before the `Completed:` comment, verify the closing commit is on `main`
   (`git merge-base --is-ancestor <sha> main`), not just committed on a
   branch — then **push**. On private/internal remotes (the host's own
   private repos — everything outside the public protocolcity org) the
   same slice that closes the ticket pushes `main` to origin; after the
   push, `git merge-base --is-ancestor <sha> origin/main` is the check. A
   merged-but-unpushed or committed-but-unmerged close-out is a `Blocked:`,
   not a `done`. Local-only `main` is how ~70 finished commits sat with no
   offsite copy while the board said done (2026-07-16 drift audit, wl-211).
   Two carve-outs: repos under the **protocolcity GitHub org** are never
   pushed by workers — stage the sync and file the `FOUNDER · publish` gate
   ticket instead (founder gate, ratified 2026-07-16); and a repo with **no
   remote** (e.g. a local-only store) closes on local `main` and says so in
   `Verification:`.
4. **Rescue stalled-but-alive claims, never discard.** A claim whose `Start:` is
   older than 3 hours with a live working copy: inspect per step 1; if work
   exists, commit + push it and comment the branch pointer before releasing to
   `backlog`. A claim under 3 hours is active — leave it alone. Each agent
   rescues only its own `Owner:` markers.
5. **Docs drift (added 2026-07-10).** If the change altered structural truth —
   entrypoints, HTTP surface, runtime layout, project/store model, process
   rules — the same close-out updates the truth docs (`PROTOCOL.md`, `README.md` as applicable) in the same commit, and the
   `Completed:` section names the doc updates (or states "docs: no drift").
   Stale truth files are orphan work with better manners; don't leave them.
6. **Stage explicitly — never blanket-add (added 2026-07-11).** This is a
   shared checkout: multiple agent lanes and the founder's own terminal all
   edit the same working copy, often concurrently. `git add -A` / `git add .`
   / `git commit -a` will sweep up someone else's uncommitted, unrelated edits
   into your ticket's commit — silently, with no ticket trail (2026-07-10
   evidence: commit c37006a absorbed a founder-terminal session's unrelated
   board.py/task_server.py fixes under an unrelated "wl-37" commit message —
   see wl-51). Stage only the files your ticket touched, by explicit path.
   The `git status --porcelain` check from step 1 is where you catch this: if
   it shows dirty files outside your ticket's scope, leave them unstaged and
   name them in your close-out comment rather than silently sweeping them in.

### 5.2) Identity and attribution (all agents)

Every ticket write carries the same identity in two places, and they must agree:

1. **The comment author field** — `--author "<agent-id>"` (CLI) / `author`
   (API), on every comment (§3.8). This is what the board byline and card
   attribution render, and what audits filter on.
2. **The `Owner:` line** in ownership markers — same agent id, optionally with
   the model in parentheses (§5).

Canonical agent ids (lowercase kebab-case, no spaces, no brackets):

| Agent id | Who |
| --- | --- |
| `morgan` | **RETIRED 2026-07-27** — succeeded by `sincere` (itself succeeded by `garfield` 2026-07-28). Was: Morgan · Lead Developer (succeeded the original host-dispatch lane 2026-07-14). History retained — comments signed by `morgan` remain valid record. |
| `sincere` | **RETIRED 2026-07-28** — succeeded by `garfield`. Was: Sincere · Lead Developer (succeeded `morgan` 2026-07-27). History retained — comments signed by `sincere` remain valid record. |\n| `garfield` | Garfield · Lead Developer. Succeeds `sincere` (retired 2026-07-28, history retained — comments signed by the old id remain valid record; earlier `morgan` / the original host-dispatch lane also valid history). Host-repo generalist desk. Papers at `the host product/workers/garfield-lane/`. |
| `founder-terminal` | **You present** — founder-driven interactive sessions on any host (Claude Desktop/Code, Grok Build/TUI, Cursor chat, Codex interactive, etc.). The AI product is the **hand**, not the actor. See §5.2.1. |
| `cursor` | **RETIRED 2026-07-14** — succeeded by `ellis` (itself succeeded by `beatriz` 2026-07-27 → `nala` 2026-07-28). Was: Cursor editor lane (§6.1). History retained. **Do not** use as MCP author for founder-present Cursor chat (that is `founder-terminal`). |
| `ellis` | **RETIRED 2026-07-27** — succeeded by `beatriz` (itself succeeded by `nala` 2026-07-28). Was: Ellis · Technical Writer (succeeded `cursor` 2026-07-14). History retained — comments signed by `ellis` remain valid record. |
| `beatriz` | **RETIRED 2026-07-28** — succeeded by `nala`. Was: Beatriz · Technical Writer (succeeded `ellis` 2026-07-27). History retained — comments signed by `beatriz` remain valid record. |\n| `nala` | Nala · Technical Writer. Succeeds `beatriz` (retired 2026-07-28, history retained — comments signed by the old id remain valid record; earlier `ellis` / `cursor` also valid history). Lane-labeled / `worker:nala` tickets; contract at host repo `workers/nala-lane/`. |
| `grok` | **RETIRED 2026-07-14** — succeeded by `kai` (itself succeeded by `kc` 2026-07-27 → `felix` 2026-07-28). Was: Grok CLI lane (§6.2). History retained. **Do not** use as MCP author for founder-present Grok sessions (that is `founder-terminal`). |
| `kai` | **RETIRED 2026-07-27** — succeeded by `kc` (itself succeeded by `felix` 2026-07-28). Was: Kai · Software Engineer (succeeded `grok` 2026-07-14). History retained — comments signed by `kai` remain valid record. |
| `kc` | **RETIRED 2026-07-28** — succeeded by `felix`. Was: Kc · Software Engineer (succeeded `kai` 2026-07-27). History retained — comments signed by `kc` remain valid record. |\n| `felix` | Felix · Software Engineer. Succeeds `kc` (retired 2026-07-28, history retained — comments signed by the old id remain valid record; earlier `kai` / `grok` also valid history). Lane-labeled / `worker:felix` tickets; contract at host repo `workers/felix-lane/`. |
| `cowork` | Claude cowork sessions |
| `claude-worklane` | **RETIRED 2026-07-14** — succeeded by `tess`. Was: Claude CLI dispatch, WL's own tickets (§8; fka `wl-pool`, renamed 2026-07-11). History retained. |
| `tess` | **RETIRED 2026-07-27** — succeeded by `tierra` (itself succeeded by `lili` 2026-07-28). Was: Tess · Desk Engineer, WL self-host lane (§8; succeeded `claude-worklane` 2026-07-14). History retained. |
| `tierra` | **RETIRED 2026-07-28** — succeeded by `lili`. Was: Tierra · Desk Engineer (succeeded `tess` 2026-07-27). History retained — comments signed by `tierra` remain valid record. |
| `lili` | Lili · Desk Engineer. Succeeds `tierra` (retired 2026-07-28, history retained — comments signed by the old id remain valid record; earlier `tess` / `claude-worklane` also valid history). WL self-host lane; contract at `workers/lili/CONTRACT.md`. |
| `doc-audit` | Monthly documentation-audit job (Claude CLI, unattended; added 2026-07-11) — files tickets and commits doc patches; never claims, reserves, or closes backlog tickets (a report job, not a dispatch lane). Patrol — function-named; not retired. |
| `backlog-snapshot` | Night-auditor patrol job — read-only backlog report; never claims. Function-named; not retired. |
| `visual-sweep` | Night-inspector patrol job — automated visual sweep; never claims. Function-named; not retired. |
| `codex` | **RETIRED 2026-07-14** — succeeded by `carl` (itself succeeded by `kayda` 2026-07-27 → `cheshire` 2026-07-28). Was: Codex CLI lane (§6.3; added 2026-07-11). History retained. |
| `carl` | **RETIRED 2026-07-27** for the host product Web Designer seat (succeeded by `kayda` → `cheshire` 2026-07-28; earlier `codex` 2026-07-14 — history retained). Re-hired 2026-07-27 as Carl · Software Engineer at ProtocolCity (renamed pc-532, succeeds `drew`). Papers at `ProtocolCity/workers/carl/`. Do not use `carl` for the host product. |
| `kayda` | **RETIRED 2026-07-28** — succeeded by `cheshire`. Was: Kayda · Web Designer (succeeded `carl` 2026-07-27). History retained — comments signed by `kayda` remain valid record. |\n| `cheshire` | Cheshire · Web Designer. Succeeds `kayda` (retired 2026-07-28, history retained — comments signed by the old id remain valid record; earlier `carl` / `codex` also valid history). Visuals/content production; contract at host repo `workers/cheshire-lane/`. |
| `riley` | **RETIRED 2026-07-27** — succeeded by `trinity`. Was: Riley · City Hall Desk, ProtocolCity docs/planning (succeeded `claude-protocolcity` 2026-07-14). History retained — comments signed by `riley` remain valid record. |
| `trinity` | Trinity · ProtocolCity. Succeeds `riley` (retired 2026-07-27, history retained — comments signed by the old id remain valid record; earlier `claude-protocolcity` also valid history). ProtocolCity docs/planning desk. Papers at `ProtocolCity/workers/trinity/`. |
| `drew` | **RETIRED 2026-07-27** — succeeded by `carl` (ProtocolCity Software Engineer, pc-532). Was: Drew · Software Engineer, ProtocolCity code lane (new hire 2026-07-21). History retained — comments signed by `drew` remain valid record. |
| `bryce` | Bryce · Suite Engineer. ProtocolCity suite/citylens/CLI packaging lane (renamed pc-533 2026-07-27; succeeded the ProtocolCity `codex` CLI slot — distinct from the the host product `codex` seat retired 2026-07-14). Papers at `ProtocolCity/workers/bryce/`. |
| `claude-socials` | **RETIRED 2026-07-14** — succeeded by `iris`. Was: Claude CLI lane, socials drafting desk (added 2026-07-14). History retained. |
| `iris` | **RETIRED 2026-07-27** — succeeded by `kenzie`, then `mittens` (so-17). Was: Iris · Content Writer (succeeded `claude-socials` 2026-07-14). History retained — comments signed by `iris` remain valid record. |
| `kenzie` | **RETIRED 2026-07-28** — succeeded by `mittens` (so-17 / pc-564 cat slate). Was: Kenzie · Content Writer (succeeded `iris` 2026-07-27, so-16). History retained — comments signed by `kenzie` remain valid record; earlier `iris` / `claude-socials` also valid history. |
| `mittens` | Mittens · Content Writer. Succeeds `kenzie` (retired 2026-07-28, history retained — comments signed by the old id remain valid record; earlier `iris` / `claude-socials` also valid history). Socials drafts-only (posting is founder-only). Papers at `socials/workers/mittens/`. Feed `worker:mittens`. |
| `claude-orchestrator` | **RETIRED 2026-07-14** — succeeded by `otto` (then `melanie` 2026-07-27, then `salem` 2026-07-28). Was: Claude CLI lane, orchestrator/WorkForce backlog (added 2026-07-14, oc-12). History retained. |
| `otto` | **RETIRED 2026-07-27** — succeeded by `melanie` (itself succeeded by `salem` 2026-07-28). Was: Otto · Systems Engineer (succeeded `claude-orchestrator` 2026-07-14). History retained — comments signed by `otto` remain valid record. |
| `melanie` | **RETIRED 2026-07-28** — succeeded by `salem`. Was: Melanie · Systems Engineer (succeeded `otto` 2026-07-27). History retained — comments signed by `melanie` remain valid record. |
| `salem` | Salem · Systems Engineer. Succeeds `melanie` (retired 2026-07-28, history retained — comments signed by the old id remain valid record; earlier `otto` / `claude-orchestrator` also valid history). WorkForce engine/board/schedule; never touches `local/` state, the daemon service, or live dispatches; papers at `workforce/workers/salem/`. |
| `neo` | **RETIRED 2026-07-27** — succeeded by `aniya` (itself succeeded by `maru` 2026-07-28). Was: Neo · Market Analyst (new hire 2026-07-14, no predecessor). History retained — comments signed by `neo` remain valid record. |
| `aniya` | **RETIRED 2026-07-28** — succeeded by `maru`. Was: Aniya · Market Analyst (succeeded `neo` 2026-07-27). History retained — comments signed by `aniya` remain valid record. |\n| `maru` | Maru · Market Analyst. Succeeds `aniya` (retired 2026-07-28, history retained — comments signed by the old id remain valid record; earlier `neo` also valid history). the host product specialist lane. Papers at `the host product/workers/maru-lane/`. |
| `wren` | Wren (new hire 2026-07-14 — no predecessor). Specialist / future desk; papers when armed. |
| `city-steward` | City Steward — cross-store stewardship patrol (job, not a claiming lane; new hire 2026-07-14 — no predecessor). Papers at orchestrator/workers/city-steward/. |
| `founder-brief` | Founder Brief — daily city reporting job (job, not a claiming lane; new hire 2026-07-14 — no predecessor). Papers at orchestrator/workers/founder-brief/. |
| `correspondent` | Correspondent — city-wide reporting job (job, not a claiming lane; hired pc-32, armed pc-502 2026-07-26). Signs briefs only; never claims backlog. Canonical papers: `.protocolcity/ops/workers/correspondent/` (pc-461). |
| `github-desk` | GitHub Desk · Public Issues — BP suite public issue **intake + close** job (job, not a claiming lane; hired 2026-07-27). Exclusive owner of `gh issue comment/close` on protocolcity org issue boards for BluePrint/WorkLane/WorkForce/homebrew-tap. Never implements suite code; never pushes public org git. Canonical papers: `.protocolcity/ops/workers/github-desk/`. |
| `ship-desk` | Ship Desk · Releases — BP suite **daily release** job (job, not a claiming lane; hired 2026-07-27). Runs `scripts/stage_daily_ship.sh` with launch-ramp default `SHIP_AUTO=1` (PyPI + homebrew-tap + local `blueprint update`). Opt out `SHIP_AUTO=0` for stage-only. Canonical papers: `.protocolcity/ops/workers/ship-desk/`. |
| `reed` | **RETIRED 2026-07-27** — succeeded by `zach` (conn-7), then `jiji` (conn-8). Was: Reed · Connector Desk (new hire 2026-07-27, no predecessor). History retained — comments signed by `reed` remain valid record. |
| `zach` | **RETIRED 2026-07-28** — succeeded by `jiji` (conn-8 / pc-564 cat slate). Was: Zach · Connector Desk (succeeded `reed` 2026-07-27, conn-7). History retained — comments signed by `zach` remain valid record. |
| `jiji` | Jiji · Connector Desk. Succeeds `zach` (retired 2026-07-28, history retained — comments signed by the old id remain valid record; earlier `reed` also valid history). Connector product generalist — design, law, bootstrap. Papers at `connector/workers/jiji/`. Feed `worker:jiji`. |

#### 5.2.1 Founder-present sessions (identity law, 2026-07-17)

**If You are in the loop, the ticket author is `founder-terminal`.** The chat
product (Grok, Claude, Cursor, …) is the **hand**, not the person. Signing a
founder decision, human-gate clear, or `FOUNDER ·` close-out as `grok` /
`felix` / `trinity` / `cursor` makes the board look like a worker self-approved
a human gate.

| Situation | Author field (`--author` / `WL_AGENT_ID`) | Optional body line |
| --- | --- | --- |
| You + any AI: decisions, gates, registry law, publish packets | `founder-terminal` | `Hand: grok-build` / `Hand: claude-desktop` / … |
| Autonomous worker shift (roster / launchd) | persona id (`lili`, `kc`, `trinity`, …) | model in `Owner:` per §5 |
| Patrol / report job | job id (`city-steward`, `founder-brief`, …) | — |

**MCP connect-time identity** is the stamp on every write. Founder-present
host configs (interactive Grok/Claude/Cursor) must set
`WL_AGENT_ID=founder-terminal` or `--author founder-terminal` — never a
retired vendor id (`grok`, `cursor`, `codex`) and never a worker persona
unless that persona is actually running unattended.

**Human gates:** only founder-present authors clear `gate_type: human` /
`FOUNDER ·` tickets in practice. Workers may stage evidence and file the
gate; they do not impersonate You on the byline.

**History:** do not rewrite past bylines. Optional clarifying comment as
`founder-terminal` if a mis-sign confuses an audit.

Related: city `AGENTS.md` (human entry points); pc-213 (persona → git);
pc-198 (You on surfaces); connector (tabled multi-citizen ids).

#### SUCCESSION (2026-07-14, wl-169 / STAFFING.md)

Persona ids **succeed** retired vendor-store ids. Succession is not an alias:
ghost-audits and historical comments stay attributable to the id that signed
them. Routing labels migrate `lane:<old-id>` → `worker:<persona>` via
`scripts/migrate_worker_labels.py` in the cutover window (coordinator-run).

| Persona id | Display | Succeeds |
| --- | --- | --- |
| `garfield` | Garfield · Lead Developer | `sincere` (retired 2026-07-28; earlier `morgan` 2026-07-27 / the original host-dispatch lane 2026-07-14 — history retained) |\n| `sincere` | **RETIRED 2026-07-28** → `garfield` | `morgan` (retired 2026-07-27; earlier the original host-dispatch lane 2026-07-14 — history retained) |
| `felix` | Felix · Software Engineer | `kc` (retired 2026-07-28; earlier `kai` 2026-07-27 / `grok` 2026-07-14 — history retained) |\n| `kc` | **RETIRED 2026-07-28** → `felix` | `kai` (retired 2026-07-27; earlier `grok` 2026-07-14 — history retained) |
| `nala` | Nala · Technical Writer | `beatriz` (retired 2026-07-28; earlier `ellis` 2026-07-27 / `cursor` 2026-07-14 — history retained) |\n| `beatriz` | **RETIRED 2026-07-28** → `nala` | `ellis` (retired 2026-07-27; earlier `cursor` 2026-07-14 — history retained) |
| `cheshire` | Cheshire · Web Designer (the host product) | `kayda` (retired 2026-07-28; earlier `carl` the host product seat 2026-07-27 / `codex` 2026-07-14 — history retained) |\n| `kayda` | **RETIRED 2026-07-28** → `cheshire` | `carl` the host product seat (retired 2026-07-27; earlier `codex` 2026-07-14 — history retained) |
| `trinity` | Trinity · ProtocolCity | `riley` (retired 2026-07-27; earlier `claude-protocolcity` 2026-07-14 — history retained) |
| `carl` | Carl · Software Engineer (ProtocolCity) | `drew` (retired 2026-07-27, pc-532 — history retained). Note: the host product `carl` seat separately RETIRED → `kayda` → `cheshire`. |
| `mittens` | Mittens · Content Writer | `kenzie` (retired 2026-07-28, so-17; earlier `iris` 2026-07-27 / `claude-socials` 2026-07-14 — history retained) |
| `maru` | Maru · Market Analyst | `aniya` (retired 2026-07-28; earlier `neo` 2026-07-14 — history retained) |\n| `aniya` | **RETIRED 2026-07-28** → `maru` | `neo` (retired 2026-07-27, no predecessor before neo — history retained) |
| `jiji` | Jiji · Connector Desk | `zach` (retired 2026-07-28, history retained; earlier `reed` also valid history) |
| `bryce` | Bryce · Suite Engineer (ProtocolCity) | ProtocolCity `codex` CLI slot (renamed pc-533 2026-07-27; distinct from the host product `codex` → `carl` → `kayda` → `cheshire` chain) |
| `lili` | Lili · Desk Engineer | `tierra` (retired 2026-07-28; earlier `tess` 2026-07-27 / `claude-worklane` 2026-07-14 — history retained) |
| `salem` | Salem · Systems Engineer | `melanie` (retired 2026-07-28; earlier `otto` 2026-07-27 / `claude-orchestrator` 2026-07-14 — history retained) |
| `tierra` | **RETIRED 2026-07-28** → `lili` | `tess` (retired 2026-07-27) |
| `melanie` | **RETIRED 2026-07-28** → `salem` | `otto` (retired 2026-07-27) |
| `morgan` | **RETIRED 2026-07-27** → `sincere` → `garfield` | the original host-dispatch lane (retired 2026-07-14) |
| `kai` | **RETIRED 2026-07-27** → `kc` → `felix` | `grok` (retired 2026-07-14) |
| `ellis` | **RETIRED 2026-07-27** → `beatriz` → `nala` | `cursor` (retired 2026-07-14) |
| `carl` (the host product) | **RETIRED 2026-07-27** → `kayda` → `cheshire` (the host product Web Designer); slug re-hired for ProtocolCity (see above) | `codex` (retired 2026-07-14) |
| `riley` | **RETIRED 2026-07-27** → `trinity` | `claude-protocolcity` (retired 2026-07-14) |
| `drew` | **RETIRED 2026-07-27** → `carl` (ProtocolCity) | — (new hire 2026-07-21) |
| `iris` | **RETIRED 2026-07-27** → `kenzie` → `mittens` | `claude-socials` (retired 2026-07-14) |
| `kenzie` | **RETIRED 2026-07-28** → `mittens` | `iris` (retired 2026-07-27) |
| `neo` | **RETIRED 2026-07-27** → `aniya` → `maru` | — (new hire 2026-07-14) |
| `reed` | **RETIRED 2026-07-27** → `zach` → `jiji` | — (new hire 2026-07-27) |
| `zach` | **RETIRED 2026-07-28** → `jiji` | `reed` (retired 2026-07-27) |
| `otto` | **RETIRED 2026-07-27** → `melanie` → `salem` | `claude-orchestrator` (retired 2026-07-14) |
| `wren` | Wren | — (new hire 2026-07-14, no predecessor; specialist / future desk) |
| `city-steward` | City Steward | — (new hire, no predecessor; patrol job) |
| `founder-brief` | Founder Brief | — (new hire, no predecessor; report job) |

Patrols unchanged (function-named; not in the succession map): `doc-audit`,
`backlog-snapshot`, `visual-sweep`.

Fire schedules are WORKER-noun data, not desk rules — this table stays identity
+ who. Cadence truth (cron expressions, next-fire) lives in WorkForce's
roster, rendered live on its board (`:8797`); look there, not here.

Reserved system authors — written by automation only, never by an agent:
`cli-label`, `cli-update`, `dependency-guard` (WL internals), and
`<host>-app` (tickets filed by the host application itself when the originating payload carries no agent id).

Rules:

- **One id per lane, forever.** Historical variants (renamed profiles, bare human names, anything with trailing whitespace), bare human names, anything with
  trailing whitespace) are deprecated — do not write them; they exist only in
  pre-2026-07-10 history.
- **New lane, new row.** A new agent lane registers its id in this table (same
  commit that adds its §6 profile) before posting its first comment. The full
  onboarding bar is §5.3 — the row and profile are necessary, not sufficient.
- **Ghost-audits key on these ids** — each agent audits only markers bearing
  its own id (§6.1/§6.2 reciprocity rule).

### 5.3) New lane onboarding checklist (added 2026-07-11, wl-72)

Registering a lane is more than the §5.2 row. Evidence for the rule: the
codex lane was registered and dispatching headless before any
`AGENTS.md` existed anywhere — an interactive Codex session in the host repo
would have auto-loaded nothing, in a repo whose CLAUDE.md carries real-money
safety rules. All four items land **before the lane's first ticket**:

1. **Identity + profile** — §5.2 id row and the lane's §6 profile, same
   commit (the existing "new lane, new row" rule).
2. **Entry file in every workdir** — the tool's *native auto-loaded
   instruction file* must exist at the root of every repo the lane operates
   in, per the **vendor-pointer rule** (ProtocolCity Charter §3, adopted
   2026-07-13): `AGENTS.md` is the single canonical instruction file (auto-loaded
   by Codex CLI and Cursor — the cross-tool standard) and carries identity
   lookup, the pointer to the §6 profile, and that repo's safety rules —
   never duplicated normative content; PROTOCOL.md stays the single source.
   Vendor-specific files are pointers, never content: `CLAUDE.md`
   containing only `@AGENTS.md` (Claude Code), `GROK.md` as a symlink to
   `AGENTS.md` (Grok CLI), `.cursor/rules/*.mdc` as a thin pointer when
   Cursor repo rules are preferred over `AGENTS.md`.
3. **Packaged run form** — a dispatched (headless) lane ships its prompt in
   the host repo (`ops/tasks/<lane>/prompt.md`) and the prompt states that
   PROTOCOL.md wins on conflict (house pattern: host
   `ops/tasks/<lane>/prompt.md`).
4. **Docs-surface candidate** — if the tool auto-loads a filename WL's docs
   nav doesn't know yet, extend `_AGENT_DOCS` in
   `worklane/task_server.py` (wl-71 mechanism: tabs appear only for
   files that exist, so registering a candidate is free).

Interactive use counts as a workdir: if a founder can open the tool by hand
in a checkout, item 2 applies to that checkout — "the launchd prompt covers
it" is not a pass.

### 5.4) Pointer-wiring verification doctrine (added 2026-07-13, wl-108)

Verify pointer/click wiring at the **hit-testing level**: resolve
`document.elementFromPoint(x, y)` at the element's real visual on-screen
location, then dispatch the click on *that* resolved element — never by
calling the handler directly, and never by dispatching on the element you
intended to be the target. Applies to any close-out that claims a UI click,
hover, or selection path works.

Two real bugs shipped "green" past close-out verification and were dead in a
real browser for days, because direct-call tests are blind to both:

- **A guard sits between target and listener.** A modal wrapper's
  `stopPropagation()` silently ate a `document`-level delegated listener;
  calling the handler directly skips the guard entirely.
- **Painted element ≠ hit-tested element.** Overlapping/stacked geometry (a
  dashed ring segment painted as a full circle) means the browser resolves a
  click to a different element than the one the test dispatched on.

### 5.5) Concurrent-edit safety — soft-lock + no-broadcast (added 2026-07-14, wl-159)

Founder-ratified 2026-07-14 (a same-file concurrent-edit near-miss). ROOT-only
checkouts share one working tree: cursor, grok, codex, **founder-terminal**
sessions (the only ROOT-checkout lane with no per-lane CONTRACT.md), and any
Claude CLI dispatch lane when working main-direct rather than in an isolated
worktree. Two sessions can touch the same file before either commits; the
pre-commit dirty-file guard (§5.1.6) catches *other* files, not same-file
concurrent edits.

Two disciplines, **mandatory for every ROOT-checkout lane including
founder-terminal**:

1. **Soft-lock before editing under an umbrella/grind ticket.** `wl_reserve`
   (or the legacy `status … in_review` + Owner comment) the umbrella before you
   start editing its files. Immediately before committing, re-check
   `git rev-parse HEAD` against your dispatch-start HEAD; if it moved, a
   concurrent writer landed — re-read the file and redo your edit rather than
   committing over a stale read. If the moved HEAD does not overlap your
   paths, you may proceed after confirming via `git diff <start>..HEAD --
   <your-paths>` that your files are untouched.
2. **Never broadcast exact coordinates.** Close-out and progress comments must
   not publish the precise next-target `file:line`. That synchronizes two
   independent grinders onto the same edit (the 2026-07-14 near-miss this
   section codifies). Next-target notes are allowed only as ticket-level
   pointers explicitly marked "reserve before working".

Worktree-isolated runs (e.g. a Claude CLI dispatch lane's Phase worktrees) are
already structurally isolated from ROOT uncommitted files — this rule still
applies whenever those lanes work main-direct. Per-lane CONTRACT.md files
(e.g. a host repo's `ops/tasks/*/CONTRACT.md`) may restate this for per-run
loading; **this section is the one-owner canonical source** those
restatements point at.

## 6) Host Profiles

Every host that adopts WorkLane writes its own profile — the per-agent
rules (identity, lanes, claim discipline, verification bar) for the agents
working its queues. Start from [HOST_PROFILE_TEMPLATE.md](HOST_PROFILE_TEMPLATE.md).
## 7) API and Surface

WL exposes API/UI so other cockpits can read ticket state. Board/table and API reflect the same DB truth. External aggregators should be read-first unless write is explicitly enabled. Host products are WL clients; a host must never depend on WL availability to start.

**One project, one store** (2026-07-10; canonical term since wl-64): every
project tracked by WL has its own SQLite file
(`worklane/local/data/<slug>.db`) and its own scope tab in the Board/Table views;
"All" is a merged read view. Projects are independent — an agent working one
project's tickets never writes another project's store. Composite ids
(`wl-…` WorkLane, `<slug>-…` your product) address tickets across stores;
WL's own development work is tracked in `worklane.db` under the same
rules as any other project. (`product` remains a silent back-compat alias on
API/MCP/CLI param names — e.g. `?product=` query params not yet migrated to
`?project=` — see wl-64/wl-46.)

**`local/data/` holds live stores only**: sqlite backups and dry-run
scratch files belong outside the discovery dir (`~/OneSeo/wl-backups/` or
similar — the scheduled `com.worklane.backup` job already writes
there). A stray backup left in `local/data/` used to surface as a phantom
project tab; discovery now skips `<slug>.db` stems matching a backup/scratch
glob (`*.pre-*`, `*.backup*`, `*bak*`, `zzz*`) unless the slug is explicitly
registered in `local/config/products.json`.

### 7.1) Onboarding a project (added 2026-07-13, wl-102)

This is the *store* onboarding path — dropping a new `<slug>.db` into WL so
a project's work is trackable. It is narrower than
[HOST_PROFILE_TEMPLATE.md](HOST_PROFILE_TEMPLATE.md): a project needs none
of this to be a real ticket surface, and doesn't need agent lanes to exist
yet (a docs/planning-stage project like ProtocolCity has tickets with no
lane running against it). Only do the full host-profile writeup (§5.3, four
items) once the project gets its own dispatched agent lane.

1. **Create the store** — `POST /api/admin/products` with a JSON body
   `{"slug": "<slug>", "display": "<Display Name>", "prefix": "<pfx>"}`
   (`display`/`prefix` optional). This is the only sanctioned door in — WL
   deliberately does not auto-create a store from a typo'd `surface=` on
   `/api/admin/tasks`. It materializes `local/data/<slug>.db` and,
   if `display`/`prefix` were given, writes them into the
   `local/config/products.json` overlay (`register_product_meta`).
2. **Slug naming rule** — `^[a-z][a-z0-9_-]{0,39}$`; `all`, `ops`, `op` are
   reserved. Avoid the backup/scratch globs above (`*.pre-*`, `*bak*`,
   `zzz*`, …) — a slug that matches one is still honored if registered in
   the config overlay, but plain lowercase words never collide.
3. **Prefix** — optional; `^[a-z][a-z0-9]{0,7}$`, must be unique across
   `discover_products()` (`o` is reserved for the retired ops store). Omit
   it and the slug itself becomes the composite-id prefix (`protocolcity-3`,
   not `pc-3`) — set one explicitly if you want a short id like `pc-`.
4. **Label taxonomy** — every ticket filed into the new store is
   auto-labeled `product:<slug>` by the tracker (no agent action needed).
   Don't add `lane:*` labels until an actual agent lane is registered
   against the project (§5.3) — an unlabeled backlog is the correct state
   for a project with no dispatched lane yet; `area:*`/`sys:*` stay
   project-specific conventions the first agent working that backlog
   establishes.
5. **AGENTS.md section** — add a short "Ticketing" section (template in
   [HOST_PROFILE_TEMPLATE.md](HOST_PROFILE_TEMPLATE.md)) to the project's
   own `AGENTS.md` (the canonical instruction file — vendor files like `CLAUDE.md`
   point to it) naming the slug and reminding agents to pass
   `project=<slug>` explicitly on every WL call — required, not optional,
   once more than one project store exists (no single call may rely on
   `WL_PRODUCT`/`WL_DEFAULT_PRODUCT` defaulting to the right one). See the
   cross-project rule in `~/OneSeo/AGENTS.md` for a worked multi-project
   example.
6. **UI wiring is automatic** — `discover_products()` re-scans
   `local/data/*.db` on every request (no restart), so Board/Table's
   segmented project nav, the Overview scope nav, and `wl_counts`/`wl_ready`
   pick up the new project the moment its store exists — zero further code
   or server changes for any N. Verified live at three concurrent project
   stores: all three surfaces render correctly with no code
   changes needed.

