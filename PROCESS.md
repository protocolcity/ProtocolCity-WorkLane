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
- Work Queue flags `in_progress`/`in_review` tickets as stalled after 90 min without updates.

## 5) Intake and Closeout

**Intake** — concise imperative title, clear problem, expected outcome (with the kind of link expected at close), `area:*` / `sys:*` labels, priority (`1` urgent → `4` low). If follow-up work surfaces mid-ticket, file a child immediately and link it in a comment on the parent.

Enforced at the API since 2026-07-10 (wl-26): creation requires a signed
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
  run on different models (e.g. `Owner: work-pool (claude-fable-5)`). The model
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

Uncommitted work in an abandoned working copy is how finished fixes get destroyed
(tradeOS #816/#836) or silently stranded off `main` while the board says done
(tradeOS #857 evidence: a batch worktree held commits for five closed tickets,
unmerged for ~5 hours). Every close-out — success or blocked — passes this guard:

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
   entrypoints, HTTP surface, runtime layout, product/store model, process
   rules — the same close-out updates the truth docs (`TRUTH.md`,
   `PROCESS.md`, `README.md` as applicable) in the same commit, and the
   `Completed:` section names the doc updates (or states "docs: no drift").
   Stale truth files are orphan work with better manners; don't leave them.

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
| `work-pool` | Claude Code hourly work-pool dispatch |
| `founder-terminal` | Founder-driven Claude terminal sessions |
| `cursor` | Cursor editor lane (§6.1) |
| `grok` | Grok CLI lane (§6.2) |
| `cowork` | Claude cowork sessions |
| `wl-pool` | Claude Code hourly lane working WL's own tickets (§8) |

Reserved system authors — written by automation only, never by an agent:
`cli-label`, `cli-update`, `dependency-guard` (WL internals), and
`tradeos-app` (tickets filed by the tradeOS app itself — ntfy auto-filers,
in-app proxies — when the originating payload carries no agent id).

Rules:

- **One id per lane, forever.** Historical variants (`tradeOS Profile`,
  `[COWORK]`, `claude`, `work-pool-test`, bare human names, anything with
  trailing whitespace) are deprecated — do not write them; they exist only in
  pre-2026-07-10 history.
- **New lane, new row.** A new agent lane registers its id in this table (same
  commit that adds its §6 profile) before posting its first comment.
- **Ghost-audits key on these ids** — each agent audits only markers bearing
  its own id (§6.1/§6.2 reciprocity rule).

## 7) API and Surface

WL exposes API/UI so other cockpits can read ticket state. Board/table and API reflect the same DB truth. External aggregators should be read-first unless write is explicitly enabled. tradeOS is a WL client; shipped/product startup must not depend on WL availability.

**One product, one store** (2026-07-10): every product tracked by WL has its
own SQLite file (`worklane/local/data/<slug>.db`) and its own Pool
surface tab; "All" is a merged read view. Products are independent — an agent
working one product's tickets never writes another product's store. Composite
ids (`t-…` tradeOS, `wl-…` WorkLane) address tickets across stores;
WL's own development work is tracked in `worklane.db` under the same
rules as any other product.


## 6) Host Profiles

Every host that adopts WorkLane writes its own profile — the per-agent
rules (identity, lanes, claim discipline, verification bar) for the agents
working its queues. Start from [HOST_PROFILE_TEMPLATE.md](HOST_PROFILE_TEMPLATE.md).
