# Host profile template

Copy this into your host repo (e.g. as a `PROTOCOL.md` section or standalone
`TICKETING.md`) and fill in the blanks. It mirrors the host-profile shape described in [PROTOCOL.md](PROTOCOL.md) §6 (Host Profiles). Setup steps live in
[INSTALL.md](INSTALL.md); this is the *process* doc that goes in your
host repo once WL is running.

Why this exists: WL enforces signed writes and status transitions, but it
has no opinion on *who* your agents are, what working copy they use, or
what "done" means for your codebase. Skipping this doc is how two agents
(or an agent and a human) end up clobbering the same ticket.

---

## `<Host Name>` Profile

Run from `<absolute path to your repo root>`. Commit subject convention:
`<your-prefix>-NNN: short description`.

**Ticket interface:** `<MCP | wl CLI | direct HTTP>` — see
[INSTALL.md §5](INSTALL.md#5-pick-an-interface-for-agents) for how each
works. Never write WL's SQLite stores directly from host code.

```bash
# fill in whichever interface you picked above, e.g.:
wl list --project <your-slug> --status backlog
```

**Agent identity:** every agent lane your host runs needs its own
canonical id (lowercase kebab-case, no spaces) — register it in a table
like PROTOCOL.md §5.2's, one row per lane:

| Agent id | Who |
| --- | --- |
| `<your-agent-id>` | `<human description, e.g. "hourly scheduled worker">` |

Every comment's `author` field and every `Owner:` marker must carry this
id (PROTOCOL.md §3.8/§5.2) — sign every write, no exceptions.

**Working copy:** `<absolute path>` — state whether this is the primary
checkout or a git worktree, and whether the lane is allowed to create its
own worktrees. (WL's own lanes default to "primary checkout, no
worktrees" — see PROTOCOL.md §6.1's rationale: a stray worktree stranded
five closed tickets' commits off `main` for ~5 hours.)

**Scan filter:** does this lane pull the full backlog, or only tickets
with a specific label (e.g. `lane:<your-agent-id>`)? If label-filtered,
say who applies the label.

**Take-list:** what kinds of tickets this lane should claim.

**Skip-list:** what it should never touch — list concrete labels/areas,
not vague categories. If a claimed ticket turns out to be on the
skip-list, the convention is: post `Blocked: scope larger than expected —
releasing` and return it to backlog.

**Verification bar:** what must pass before a `Completed:` close-out —
e.g. `<test command>`, lint, a manual check. Be concrete; "tests pass" is
only useful if the next agent knows which command to run.

**Deploy step (if any):** does landing a change require restarting a
service? Name the exact command and the health-check to confirm it came
back up.

**Claim / close-out / ghost-audit:** state that PROTOCOL.md §2 (agent flow)
and §5 (intake/closeout, including the §5.1 commit-or-abandon guard) apply
unchanged, with this lane's `Owner:` id. Ghost-audits are reciprocal — this
lane audits only its own `Owner:` markers, never another lane's.

**Runtime (if scheduled/automated):** how and when this lane fires (cron
expression, launchd label, manual trigger), and where its logs land.

---

## AGENTS.md snippet

Add a section like this to your host repo's `AGENTS.md` (or equivalent
agent-instructions file) so any agent that scopes into ticket work finds
the rulebook:

```markdown
## Ticketing

This repo tracks work in WorkLane (WL), a standalone local-first
ticketing service — not GitHub Issues, not a TODO file. Before touching
any ticket:

1. Read `<path-to-your-copy-of>/PROTOCOL.md` (or your host profile section
   above) — the lifecycle/ownership rulebook every agent follows.
2. Use `<MCP | the wl CLI | curl>` for every read/write — never open WL's
   SQLite files directly.
3. Sign every comment with `<your-agent-id>` (PROTOCOL.md §3.8).
4. Close tickets with the four-section contract: `Completed:` /
   `Verification:` / `Links:` / `Follow-ups:` — malformed close-outs are
   rejected by the API (or by `wl_close` if you're on MCP).

WL itself lives at `<path to your WL checkout, or "vendored at <path>">`.
Service runs on `<host>:<port>` (default `127.0.0.1:8799`).
```
