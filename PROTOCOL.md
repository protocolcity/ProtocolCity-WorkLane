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

### 2.1) Coordination model: lanes

Multi-agent routing on WL is a negative-space default, not an explicit gate. This underpins the "Scan" step above and every per-agent profile in §6 — stated here once instead of left implicit across those profiles.

- **Unlabeled = default lane.** Every ticket belongs to the primary/most-capable agent's pool by default, and that pool scans the *full* backlog — not just labeled tickets. A ticket needs no label to be workable; the absence of a `lane:*` label is itself the routing signal.
- **`lane:*` labels delegate down, they don't gate.** A `lane:cursor` or `lane:grok` label (§6.1, §6.2) narrows a ticket to a specific narrower-scope agent — it signals who should take it first, not who is *allowed* to see it. The default-lane pool still owns the ticket if the narrower lane doesn't act on it.
- **>24h no-activity demotion.** A `lane:*` label with no activity for more than 24 hours is treated as stale: the default-lane pool may pick the ticket up as if it were unlabeled. Narrower-lane agents don't need to strip the label themselves — the pool's own scan handles the fallback.
- **Why labels aren't mandatory.** Requiring a lane label on every ticket would turn the label into a routing gate: a fresh, unlabeled ticket would be invisible to every agent until someone triaged it, adding a failure mode where tickets belong to nobody and rot. The unlabeled-default guarantees every ticket always has an owner-of-last-resort. (Ratified as DECISION (recommendation-default) 2026-07-11, wl-53 — founder may veto.)

This is deliberately a fail-safe, not a strict routing table: per-agent scan filters (Cursor's `--label lane:cursor`, Grok's `--label lane:grok`) are narrowings of the default-lane pool's scan, never replacements for it.

One addendum (ratified 2026-07-11): a narrower lane whose profile defines **objective, mechanically checkable self-service criteria** (§6.2 Grok) may, when its lane is empty, take an unlabeled ticket that passes every criterion — self-labeling it first so the triage decision is recorded on the ticket. Self-service never changes ownership defaults: unlabeled tickets still belong to the default-lane pool, and a lane without a self-service clause in its profile (e.g. Cursor, §6.1) has none.

## 3) Rules

1. **No orphan work** — any TODO, gap, fix, or refactor starts with a ticket.
2. **Single live owner** — `in_progress` is one ticket per agent at a time.
3. **Status is truth** — ticket status matches actual work state.
4. **Comment trail** — blockers, decisions, and completion evidence go in comments.
5. **Close with links** — the completion comment must include at least one navigable reference (PR URL, merge commit, doc path).
6. **Declare dependencies** — use `Depends on #NNN` in the description so the queue guard can freeze siblings.
7. **Recommendation-default decisions** (founder-ratified 2026-07-09) — when a ticket hits a decision point, the agent records its recommendation as the decision (`DECISION (recommendation-default): <choice> — <why>` comment) and keeps working; the founder reviews and can veto after the fact. `needs:founder-decision` is reserved for the escalation class only: real-money gates (LIVE flips, risk-limit widening, new broker/credential enablement, moving money, gate bypasses), reversals of ratified ADRs/product direction, and public-facing or expensive-to-reverse actions. Everything else — including strategy-intent on paper/bench plays and exposure-reducing enforcement — proceeds on the recommendation. Decisions must be logged in ticket comments so the veto window is real.
8. **Sign every comment** (2026-07-10) — pass the author flag (`--author "<agent-id>"` on the CLI, `author` on the API) on every comment you post, using your canonical agent id from §5.2. The `Owner:` line inside the body documents the claim; the author *field* is what the board byline, filters, and ghost-audits key on. The two must carry the same id. An unsigned (empty-author) comment is a process violation, not a default.

## 4) Transitions

Allowed moves:

- `backlog → in_review` (reserve)
- `in_review → in_progress` (promote to live)
- `in_review → backlog` (release back to pool)
- `in_progress → in_review` (park; rotate to a sibling)
- `in_progress → backlog` (abandon via `Blocked:` comment)
- `in_progress → done` (complete via `Completed:` comment)
- `in_review → done` (bundled completion, rare)
- `* → canceled` (with rationale)

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
does not satisfy it. `Follow-ups:` may be "none".

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
3. **`done` means reachable from `main`.** Before the `Completed:` comment,
   verify the closing commit is on `main` (`git merge-base --is-ancestor <sha>
   main`), not just committed on a branch. A merged-but-unpushed or
   committed-but-unmerged close-out is a `Blocked:`, not a `done`.
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
| `founder-terminal` | Founder-driven Claude terminal sessions |
| `cursor` | Cursor editor lane (§6.1) |
| `grok` | Grok CLI lane (§6.2) |
| `cowork` | Claude cowork sessions |
| `claude-worklane` | Claude CLI dispatch, WL's own tickets (§8; launchd :15; fka `wl-pool`, renamed 2026-07-11) |
| `doc-audit` | Monthly documentation-audit job (Claude CLI, unattended; added 2026-07-11) — files tickets and commits doc patches; never claims, reserves, or closes backlog tickets (a report job, not a dispatch lane) |
| `codex` | Codex CLI lane (§6.3; added 2026-07-11) — visuals/content production |

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
   in: `CLAUDE.md` (Claude Code), `AGENTS.md` (Codex CLI, Cursor — the
   cross-tool standard), `GROK.md` (Grok CLI; symlink to `AGENTS.md` is
   fine), `.cursor/rules/*.mdc` (Cursor, when repo rules are preferred over
   `AGENTS.md`). Written as a **thin router** — identity, pointer to the §6
   profile, that repo's safety rules — never duplicated normative content;
   PROTOCOL.md stays the single source. House pattern: a host's
   `.cursor/rules/<host>.mdc` (or thin `AGENTS.md` router at repo root).
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
scratch files belong outside the discovery dir (`~/Developer/wl-backups/` or
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
5. **CLAUDE.md pointer** — add a short "Ticketing" section (template in
   [HOST_PROFILE_TEMPLATE.md](HOST_PROFILE_TEMPLATE.md)) to the project's
   own `CLAUDE.md`/`AGENTS.md` naming the slug and reminding agents to pass
   `project=<slug>` explicitly on every WL call — required, not optional,
   once more than one project store exists (no single call may rely on
   `WL_PRODUCT`/`WL_DEFAULT_PRODUCT` defaulting to the right one). See the
   cross-project rule in `~/Developer/CLAUDE.md` for a worked multi-project
   example.
6. **UI wiring is automatic** — `discover_products()` re-scans
   `local/data/*.db` on every request (no restart), so Board/Table's
   segmented project nav, the Overview scope nav, and `wl_counts`/`wl_ready`
   pick up the new project the moment its store exists — zero further code
   or server changes for any N. Verified live at three concurrent project
   stores: all three surfaces render correctly with no code
   changes needed.


## 6) Host Profiles

Every host that adopts WorkLane writes its own profile — the per-agent
rules (identity, lanes, claim discipline, verification bar) for the agents
working its queues. Start from [HOST_PROFILE_TEMPLATE.md](HOST_PROFILE_TEMPLATE.md).
