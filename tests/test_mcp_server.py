"""MCP server + handlers tests (wl-19, wl-31, wl-32, wl-33).

Covers connect-time author identity, work-lifecycle + triage tools,
soft-lock reserve/park, ownership pulse (wl_mine/wl_counts),
structured close-out enforcement, and a round-trip JSON-RPC session
over in-memory stdio.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from worklane.mcp.handlers import (
    TPHandlers,
    ToolError,
    build_tool_definitions,
    dispatch_tool,
)
from worklane.mcp.server import MCPServer
from worklane.trackers.sqlite import SQLiteTracker


def _make_env(tmp: Path) -> None:
    (tmp / "data").mkdir(parents=True, exist_ok=True)
    os.environ["WORKLANE_RUNTIME_DIR"] = str(tmp)
    os.environ["WORKLANE_DB"] = str(tmp / "data" / "tradeos.db")
    os.environ.pop("TRADEOS_TRACKER_DB", None)
    os.environ["TRADEOS_TICKETS_SOURCE"] = "sqlite"


class HandlersTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._env_before = {
            k: os.environ.get(k)
            for k in (
                "WORKLANE_RUNTIME_DIR",
                "WORKLANE_DB",
                "TRADEOS_TRACKER_DB",
                "TRADEOS_TICKETS_SOURCE",
                "WL_PRODUCT",
            )
        }
        _make_env(self.root)
        # Seed a second product store so composite-id paths are exercised.
        # The DB file only appears on disk after the first write.
        SQLiteTracker(
            db_path=self.root / "data" / "worklane.db"
        ).create_task(
            title="seed",
            description="bootstrap worklane.db for discovery",
        )
        self.h = TPHandlers(author="grok", default_product="tradeos")

    def tearDown(self) -> None:
        for k, v in self._env_before.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    def test_author_required(self) -> None:
        with self.assertRaises(ValueError):
            TPHandlers(author="")

    def test_tool_catalog_has_sixteen(self) -> None:
        names = {t["name"] for t in build_tool_definitions()}
        self.assertEqual(
            names,
            {
                "wl_list",
                "wl_ready",
                "wl_show",
                "wl_create",
                "wl_claim",
                "wl_comment",
                "wl_close",
                "wl_release",
                "wl_label",
                "wl_update",
                "wl_cancel",
                "wl_reopen",
                "wl_reserve",
                "wl_park",
                "wl_mine",
                "wl_counts",
            },
        )
        # Existing core tools keep their names/required keys (byte-stable contracts).
        by_name = {t["name"]: t for t in build_tool_definitions()}
        self.assertEqual(
            set(by_name["wl_create"]["inputSchema"]["required"]),
            {"title", "description"},
        )
        self.assertEqual(
            set(by_name["wl_close"]["inputSchema"]["required"]),
            {"task_id", "completed", "verification", "links"},
        )
        self.assertEqual(
            set(by_name["wl_claim"]["inputSchema"]["required"]),
            {"task_id"},
        )
        self.assertEqual(
            set(by_name["wl_reserve"]["inputSchema"]["required"]),
            {"task_id"},
        )
        self.assertEqual(
            set(by_name["wl_park"]["inputSchema"]["required"]),
            {"task_id"},
        )

    def test_tool_catalog_exposes_project_alongside_product(self) -> None:
        # wl-64: every tool that took 'product' now also declares 'project'
        # as the canonical name; 'product' stays as a documented alias.
        by_name = {t["name"]: t for t in build_tool_definitions()}
        for name, tool in by_name.items():
            props = tool["inputSchema"]["properties"]
            if "product" in props:
                self.assertIn("project", props, name)

    def test_dispatch_project_alias_resolves_like_product(self) -> None:
        created = dispatch_tool(
            self.h,
            "wl_create",
            {
                "title": "Alias via project",
                "description": "Problem: naming. Expected: project== product.",
                "project": "tradeos",
            },
        )
        self.assertTrue(created["ok"])
        self.assertEqual(created["task"]["product"], "tradeos")

    def test_dispatch_project_and_product_agree_is_fine(self) -> None:
        created = dispatch_tool(
            self.h,
            "wl_create",
            {
                "title": "Alias agree",
                "description": "Problem: naming. Expected: no conflict when equal.",
                "project": "tradeos",
                "product": "tradeos",
            },
        )
        self.assertTrue(created["ok"])

    def test_dispatch_project_product_conflict_rejected(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            dispatch_tool(
                self.h,
                "wl_create",
                {
                    "title": "Alias conflict",
                    "description": "Problem: naming. Expected: reject, don't guess.",
                    "project": "tradeos",
                    "product": "worklane",
                },
            )
        self.assertIn("conflicting", ctx.exception.message.lower())

    def test_dispatch_bare_product_still_works(self) -> None:
        # Back-compat: existing agent prompts pass 'product' only.
        listed = dispatch_tool(self.h, "wl_list", {"product": "tradeos", "status": "backlog"})
        self.assertEqual(listed["product"], "tradeos")

    def test_create_list_show(self) -> None:
        created = self.h.wl_create(
            title="Wire MCP",
            description="Ship stdio server; expected: agent can claim tickets.",
            priority=2,
            labels=["area:api"],
        )
        self.assertTrue(created["ok"])
        tid = created["task"]["id"]
        self.assertTrue(tid.startswith("t-"))
        self.assertEqual(created["task"]["status"], "backlog")

        listed = self.h.wl_list(status="backlog")
        self.assertGreaterEqual(listed["count"], 1)
        ids = {t["id"] for t in listed["tasks"]}
        self.assertIn(tid, ids)

        shown = self.h.wl_show(tid)
        self.assertEqual(shown["title"], "Wire MCP")
        self.assertTrue(
            any("Intake: filed by grok" in (c["body"] or "") for c in shown["comments"])
        )
        self.assertEqual(shown["comments"][0]["author"], "grok")

    def test_create_requires_description(self) -> None:
        with self.assertRaises(ToolError):
            self.h.wl_create(title="nope", description="  ")

    def test_claim_close_lifecycle(self) -> None:
        created = self.h.wl_create(
            title="Lifecycle ticket",
            description="Problem: need close path. Expected: done status.",
            product="tradeos",
        )
        tid = created["task"]["id"]

        claimed = self.h.wl_claim(
            tid, plan="implement\ntest", workdir="/tmp/work"
        )
        self.assertEqual(claimed["task"]["status"], "in_progress")
        self.assertIn("Owner: grok", claimed["owner_comment"])
        self.assertIn("Workdir: /tmp/work", claimed["owner_comment"])

        closed = self.h.wl_close(
            tid,
            completed="- handlers.py\n- server.py",
            verification="- pytest tests/test_mcp_server.py green",
            links="- tests/test_mcp_server.py",
            follow_ups="none",
        )
        self.assertTrue(closed["ok"])
        self.assertEqual(closed["task"]["status"], "done")
        body = closed["comment"]["body"]
        self.assertIn("Completed:", body)
        self.assertIn("Verification:", body)
        self.assertIn("Links:", body)
        self.assertIn("Follow-ups:", body)

    def test_close_rejects_missing_sections(self) -> None:
        created = self.h.wl_create(
            title="Bad close",
            description="Problem: incomplete close. Expected: ToolError.",
        )
        tid = created["task"]["id"]
        self.h.wl_claim(tid)
        with self.assertRaises(ToolError):
            self.h.wl_close(
                tid,
                completed="stuff",
                verification="",  # missing
                links="path",
            )

    def test_comment_rejects_unsigned_close_shape(self) -> None:
        created = self.h.wl_create(
            title="Comment guard",
            description="Problem: freeform close. Expected: prefer wl_close.",
        )
        tid = created["task"]["id"]
        self.h.wl_claim(tid)
        with self.assertRaises(ToolError) as ctx:
            self.h.wl_comment(tid, "Completed:\n- x\n")
        self.assertIn("Verification:", ctx.exception.message)

    def test_release_returns_to_backlog(self) -> None:
        created = self.h.wl_create(
            title="Release me",
            description="Problem: wrong pick. Expected: back to pool.",
        )
        tid = created["task"]["id"]
        self.h.wl_claim(tid)
        released = self.h.wl_release(tid, reason="scope too large")
        self.assertEqual(released["task"]["status"], "backlog")
        self.assertIn("Blocked:", released["comment"]["body"])
        self.assertIn("Next step:", released["comment"]["body"])

    def test_product_worklane_composite_id(self) -> None:
        created = self.h.wl_create(
            title="WL self ticket",
            description="Problem: MCP must reach wl store. Expected: wl-N id.",
            product="worklane",
        )
        tid = created["task"]["id"]
        self.assertTrue(tid.startswith("wl-"), tid)
        shown = self.h.wl_show(tid)
        self.assertEqual(shown["product"], "worklane")
        listed = self.h.wl_list(product="worklane", status="backlog")
        self.assertIn(tid, {t["id"] for t in listed["tasks"]})

    def test_ready_excludes_blocked(self) -> None:
        anchor = self.h.wl_create(
            title="Anchor",
            description="Problem: blocker. Expected: done first.",
        )
        anchor_raw = anchor["task"]["raw_id"]
        blocked = self.h.wl_create(
            title="Blocked child",
            description=f"Depends on #{anchor_raw}\n\nProblem: waiting. Expected: unblocked later.",
        )
        free = self.h.wl_create(
            title="Free work",
            description="Problem: free. Expected: shows in ready.",
        )
        ready = self.h.wl_ready(product="tradeos")
        ready_ids = {t["id"] for t in ready["tasks"]}
        self.assertIn(free["task"]["id"], ready_ids)
        self.assertNotIn(blocked["task"]["id"], ready_ids)
        self.assertIn(anchor["task"]["id"], ready_ids)

    def test_ready_excludes_gated(self) -> None:
        gated = self.h.wl_create(
            title="Gated ticket",
            description="Problem: needs founder sign-off. Expected: hidden from ready.",
        )
        free = self.h.wl_create(
            title="Free work",
            description="Problem: free. Expected: shows in ready.",
        )
        self.h.wl_update(gated["task"]["id"], gate_type="human", gate_note="ask founder")

        ready = self.h.wl_ready(product="tradeos")
        ready_ids = {t["id"] for t in ready["tasks"]}
        self.assertIn(free["task"]["id"], ready_ids)
        self.assertNotIn(gated["task"]["id"], ready_ids)

    def test_dispatch_unknown_tool(self) -> None:
        with self.assertRaises(ToolError):
            dispatch_tool(self.h, "wl_nope", {})

    def test_unknown_product(self) -> None:
        with self.assertRaises(ToolError):
            self.h.wl_list(product="does-not-exist")

    def test_label_add_remove(self) -> None:
        created = self.h.wl_create(
            title="Lane routing",
            description="Problem: need labels. Expected: add/remove via MCP.",
            labels=["area:api"],
        )
        tid = created["task"]["id"]

        labeled = self.h.wl_label(tid, add=["lane:grok", "epic:wl-18"])
        self.assertTrue(labeled["ok"])
        labels = set(labeled["task"]["labels"])
        self.assertTrue(
            {"area:api", "lane:grok", "epic:wl-18"}.issubset(labels), labels
        )

        pruned = self.h.wl_label(tid, remove=["area:api"])
        pruned_labels = set(pruned["task"]["labels"])
        self.assertNotIn("area:api", pruned_labels)
        self.assertTrue(
            {"lane:grok", "epic:wl-18"}.issubset(pruned_labels), pruned_labels
        )

        # Audit comment is signed with connect-time author.
        shown = self.h.wl_show(tid)
        label_comments = [
            c for c in shown["comments"] if "Updated labels" in (c["body"] or "")
        ]
        self.assertTrue(label_comments)
        self.assertEqual(label_comments[-1]["author"], "grok")

    def test_label_requires_add_or_remove(self) -> None:
        created = self.h.wl_create(
            title="No labels",
            description="Problem: empty label call. Expected: ToolError.",
        )
        with self.assertRaises(ToolError) as ctx:
            self.h.wl_label(created["task"]["id"])
        self.assertIn("add", ctx.exception.message.lower())

    def test_label_bad_id(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            self.h.wl_label("99999", add=["lane:grok"])
        self.assertIn("not found", ctx.exception.message)

    def test_update_priority_and_title(self) -> None:
        created = self.h.wl_create(
            title="Old title",
            description="Problem: triage rescope. Expected: fields update.",
            priority=3,
        )
        tid = created["task"]["id"]

        updated = self.h.wl_update(
            tid,
            title="New title",
            priority=1,
            description="Problem: bumped. Expected: P1 + new title.",
        )
        self.assertTrue(updated["ok"])
        self.assertEqual(updated["task"]["title"], "New title")
        self.assertEqual(updated["task"]["priority"], 1)
        self.assertIn("bumped", updated["task"]["description"])

        shown = self.h.wl_show(tid)
        audit = [
            c for c in shown["comments"] if "Updated fields" in (c["body"] or "")
        ]
        self.assertTrue(audit)
        self.assertEqual(audit[-1]["author"], "grok")

    def test_update_requires_a_field(self) -> None:
        created = self.h.wl_create(
            title="Noop update",
            description="Problem: empty update. Expected: ToolError.",
        )
        with self.assertRaises(ToolError):
            self.h.wl_update(created["task"]["id"])

    def test_update_rejects_bad_priority(self) -> None:
        created = self.h.wl_create(
            title="Prio guard",
            description="Problem: bad prio. Expected: ToolError.",
        )
        with self.assertRaises(ToolError):
            self.h.wl_update(created["task"]["id"], priority=9)

    def test_update_sets_and_clears_gate(self) -> None:
        created = self.h.wl_create(
            title="Gate via MCP",
            description="Problem: needs a gate. Expected: wl_update sets it.",
        )
        tid = created["task"]["id"]

        gated = self.h.wl_update(tid, gate_type="human", gate_note="waiting on X")
        self.assertEqual(gated["task"]["gate_type"], "human")
        self.assertEqual(gated["task"]["gate_note"], "waiting on X")

        cleared = self.h.wl_update(tid, gate_type="")
        self.assertNotIn("gate_type", cleared["task"])

    def test_update_timer_gate_requires_gate_until(self) -> None:
        created = self.h.wl_create(
            title="Timer gate guard",
            description="Problem: missing gate_until. Expected: ToolError.",
        )
        with self.assertRaises(ToolError):
            self.h.wl_update(created["task"]["id"], gate_type="timer")

    def test_update_rejects_bad_gate_type(self) -> None:
        created = self.h.wl_create(
            title="Bad gate type guard",
            description="Problem: bogus gate_type. Expected: ToolError.",
        )
        with self.assertRaises(ToolError):
            self.h.wl_update(created["task"]["id"], gate_type="bogus")

    def test_update_product_resolution(self) -> None:
        created = self.h.wl_create(
            title="WL product update",
            description="Problem: composite product. Expected: wl- id updates.",
            product="worklane",
            priority=3,
        )
        tid = created["task"]["id"]
        self.assertTrue(tid.startswith("wl-"))
        updated = self.h.wl_update(tid, priority=2)
        self.assertEqual(updated["task"]["product"], "worklane")
        self.assertEqual(updated["task"]["priority"], 2)

    def test_cancel_and_reopen(self) -> None:
        created = self.h.wl_create(
            title="Dupe ticket",
            description="Problem: duplicate of another. Expected: cancel path.",
        )
        tid = created["task"]["id"]

        canceled = self.h.wl_cancel(tid, reason="duplicate of #1")
        self.assertTrue(canceled["ok"])
        self.assertEqual(canceled["task"]["status"], "canceled")
        self.assertIn("Canceled:", canceled["comment"]["body"])
        self.assertEqual(canceled["comment"]["author"], "grok")

        reopened = self.h.wl_reopen(tid, reason="regression — still needed")
        self.assertEqual(reopened["task"]["status"], "backlog")
        self.assertIn("Reopened:", reopened["comment"]["body"])

    def test_reopen_from_done(self) -> None:
        created = self.h.wl_create(
            title="Close then reopen",
            description="Problem: regression reopen. Expected: done→backlog.",
        )
        tid = created["task"]["id"]
        self.h.wl_claim(tid)
        self.h.wl_close(
            tid,
            completed="- shipped",
            verification="- pytest green",
            links="- tests/test_mcp_server.py",
        )
        reopened = self.h.wl_reopen(tid)
        self.assertEqual(reopened["task"]["status"], "backlog")
        self.assertIn("Reopened by grok", reopened["comment"]["body"])

    def test_cancel_requires_reason(self) -> None:
        created = self.h.wl_create(
            title="No reason",
            description="Problem: bare cancel. Expected: ToolError.",
        )
        with self.assertRaises(ToolError):
            self.h.wl_cancel(created["task"]["id"], reason="  ")

    def test_cancel_rejects_done(self) -> None:
        created = self.h.wl_create(
            title="Already done",
            description="Problem: cancel after close. Expected: ToolError.",
        )
        tid = created["task"]["id"]
        self.h.wl_claim(tid)
        self.h.wl_close(
            tid,
            completed="- x",
            verification="- y",
            links="- z",
        )
        with self.assertRaises(ToolError) as ctx:
            self.h.wl_cancel(tid, reason="oops")
        self.assertIn("done", ctx.exception.message.lower())

    def test_reopen_rejects_open_ticket(self) -> None:
        created = self.h.wl_create(
            title="Still open",
            description="Problem: reopen backlog. Expected: ToolError.",
        )
        with self.assertRaises(ToolError) as ctx:
            self.h.wl_reopen(created["task"]["id"])
        self.assertIn("done/canceled", ctx.exception.message)

    def test_dispatch_triage_tools(self) -> None:
        created = self.h.wl_create(
            title="Dispatch path",
            description="Problem: dispatch_tool wiring. Expected: label works.",
        )
        tid = created["task"]["id"]
        out = dispatch_tool(
            self.h, "wl_label", {"task_id": tid, "add": ["lane:cursor"]}
        )
        self.assertIn("lane:cursor", out["task"]["labels"])
        out2 = dispatch_tool(
            self.h, "wl_update", {"task_id": tid, "priority": 2}
        )
        self.assertEqual(out2["task"]["priority"], 2)
        out3 = dispatch_tool(
            self.h, "wl_cancel", {"task_id": tid, "reason": "test dupe"}
        )
        self.assertEqual(out3["task"]["status"], "canceled")
        out4 = dispatch_tool(self.h, "wl_reopen", {"task_id": tid})
        self.assertEqual(out4["task"]["status"], "backlog")

    def test_reserve_and_claim_promote(self) -> None:
        created = self.h.wl_create(
            title="Soft-lock me",
            description="Problem: reserve while reading. Expected: in_review.",
        )
        tid = created["task"]["id"]

        reserved = self.h.wl_reserve(tid, note="scanning for bundle mates")
        self.assertTrue(reserved["ok"])
        self.assertEqual(reserved["task"]["status"], "in_review")
        self.assertIn("Owner: grok", reserved["comment"]["body"])
        self.assertIn("Reserved:", reserved["comment"]["body"])
        self.assertEqual(reserved["comment"]["author"], "grok")

        # Idempotent re-reserve stays in_review.
        again = self.h.wl_reserve(tid)
        self.assertEqual(again["task"]["status"], "in_review")

        # Promote reserved ticket via claim.
        claimed = self.h.wl_claim(tid, plan="work it")
        self.assertEqual(claimed["task"]["status"], "in_progress")

    def test_park_bundle_rotate(self) -> None:
        live = self.h.wl_create(
            title="Live ticket",
            description="Problem: park for rotate. Expected: in_review.",
        )
        sibling = self.h.wl_create(
            title="Sibling",
            description="Problem: next in bundle. Expected: claim after park.",
        )
        live_id = live["task"]["id"]
        sib_id = sibling["task"]["id"]

        self.h.wl_claim(live_id)
        self.h.wl_reserve(sib_id, note="bundled with live")

        parked = self.h.wl_park(live_id, reason="rotate to sibling")
        self.assertEqual(parked["task"]["status"], "in_review")
        self.assertIn("Parked:", parked["comment"]["body"])
        self.assertEqual(parked["comment"]["author"], "grok")

        claimed_sib = self.h.wl_claim(sib_id)
        self.assertEqual(claimed_sib["task"]["status"], "in_progress")

        # Parked sibling still soft-locked, not free pool.
        shown = self.h.wl_show(live_id)
        self.assertEqual(shown["status"], "in_review")

    def test_reserve_rejects_in_progress(self) -> None:
        created = self.h.wl_create(
            title="Already live",
            description="Problem: reserve live ticket. Expected: ToolError.",
        )
        tid = created["task"]["id"]
        self.h.wl_claim(tid)
        with self.assertRaises(ToolError) as ctx:
            self.h.wl_reserve(tid)
        self.assertIn("wl_park", ctx.exception.message)

    def test_park_rejects_backlog(self) -> None:
        created = self.h.wl_create(
            title="Not live",
            description="Problem: park backlog. Expected: ToolError.",
        )
        with self.assertRaises(ToolError) as ctx:
            self.h.wl_park(created["task"]["id"])
        self.assertIn("in_progress", ctx.exception.message)

    def test_mine_finds_owner_markers(self) -> None:
        other = TPHandlers(author="cursor", default_product="tradeos")
        mine = self.h.wl_create(
            title="My claim",
            description="Problem: ownership scan. Expected: appears in wl_mine.",
        )
        theirs = other.wl_create(
            title="Their claim",
            description="Problem: other agent. Expected: not in my wl_mine.",
        )
        reserved = self.h.wl_create(
            title="My reserve",
            description="Problem: soft-lock ownership. Expected: in wl_mine.",
        )
        free = self.h.wl_create(
            title="Unowned backlog",
            description="Problem: free pool. Expected: not in wl_mine.",
        )

        mine_id = mine["task"]["id"]
        theirs_id = theirs["task"]["id"]
        reserved_id = reserved["task"]["id"]
        free_id = free["task"]["id"]

        self.h.wl_claim(mine_id)
        other.wl_claim(theirs_id)
        self.h.wl_reserve(reserved_id)

        owned = self.h.wl_mine(product="tradeos")
        ids = {t["id"] for t in owned["tasks"]}
        self.assertEqual(owned["author"], "grok")
        self.assertIn(mine_id, ids)
        self.assertIn(reserved_id, ids)
        self.assertNotIn(theirs_id, ids)
        self.assertNotIn(free_id, ids)
        # in_progress sorts before in_review
        statuses = [t["status"] for t in owned["tasks"] if t["id"] in (mine_id, reserved_id)]
        self.assertEqual(statuses[0], "in_progress")

    def test_counts_histogram(self) -> None:
        self.h.wl_create(
            title="Count A",
            description="Problem: counts. Expected: backlog bucket.",
        )
        b = self.h.wl_create(
            title="Count B",
            description="Problem: counts live. Expected: in_progress bucket.",
        )
        self.h.wl_claim(b["task"]["id"])

        counts = self.h.wl_counts(product="tradeos")
        self.assertEqual(counts["product"], "tradeos")
        self.assertGreaterEqual(counts["total"], 2)
        self.assertGreaterEqual(counts["counts"].get("backlog", 0), 1)
        self.assertGreaterEqual(counts["counts"].get("in_progress", 0), 1)

        all_counts = self.h.wl_counts(product="all")
        self.assertEqual(all_counts["product"], "all")
        self.assertIn("by_product", all_counts)
        self.assertIn("tradeos", all_counts["by_product"])
        self.assertIn("worklane", all_counts["by_product"])

    def test_dispatch_softlock_and_pulse(self) -> None:
        created = self.h.wl_create(
            title="Dispatch softlock",
            description="Problem: dispatch wiring. Expected: reserve/park/mine/counts.",
        )
        tid = created["task"]["id"]
        out = dispatch_tool(self.h, "wl_reserve", {"task_id": tid})
        self.assertEqual(out["task"]["status"], "in_review")
        out2 = dispatch_tool(self.h, "wl_claim", {"task_id": tid})
        self.assertEqual(out2["task"]["status"], "in_progress")
        out3 = dispatch_tool(
            self.h, "wl_park", {"task_id": tid, "reason": "pause"}
        )
        self.assertEqual(out3["task"]["status"], "in_review")
        mine = dispatch_tool(self.h, "wl_mine", {})
        self.assertIn(tid, {t["id"] for t in mine["tasks"]})
        counts = dispatch_tool(self.h, "wl_counts", {"product": "tradeos"})
        self.assertGreaterEqual(counts["total"], 1)


class StdioProtocolTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._env_before = {
            k: os.environ.get(k)
            for k in (
                "WORKLANE_RUNTIME_DIR",
                "WORKLANE_DB",
                "TRADEOS_TRACKER_DB",
                "TRADEOS_TICKETS_SOURCE",
            )
        }
        _make_env(self.root)
        self.handlers = TPHandlers(author="cursor", default_product="tradeos")

    def tearDown(self) -> None:
        for k, v in self._env_before.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    def _session(self, messages: list) -> list:
        stdin = io.StringIO("\n".join(json.dumps(m) for m in messages) + "\n")
        stdout = io.StringIO()
        server = MCPServer(self.handlers, stdin=stdin, stdout=stdout)
        server.serve_forever()
        lines = [ln for ln in stdout.getvalue().splitlines() if ln.strip()]
        return [json.loads(ln) for ln in lines]

    def test_initialize_and_tools_list(self) -> None:
        replies = self._session(
            [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "0"},
                    },
                },
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            ]
        )
        self.assertEqual(replies[0]["id"], 1)
        self.assertEqual(
            replies[0]["result"]["serverInfo"]["name"], "worklane"
        )
        self.assertIn("author='cursor'", replies[0]["result"]["instructions"])
        tools = replies[1]["result"]["tools"]
        self.assertEqual(len(tools), 16)
        tool_names = {t["name"] for t in tools}
        self.assertIn("wl_label", tool_names)
        self.assertIn("wl_update", tool_names)
        self.assertIn("wl_cancel", tool_names)
        self.assertIn("wl_reopen", tool_names)
        self.assertIn("wl_reserve", tool_names)
        self.assertIn("wl_park", tool_names)
        self.assertIn("wl_mine", tool_names)
        self.assertIn("wl_counts", tool_names)

    def test_tools_call_create_and_list(self) -> None:
        replies = self._session(
            [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "0"},
                    },
                },
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "wl_create",
                        "arguments": {
                            "title": "Via MCP",
                            "description": "Problem: rpc path. Expected: ticket exists.",
                            "priority": 3,
                        },
                    },
                },
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "wl_list",
                        "arguments": {"status": "backlog"},
                    },
                },
            ]
        )
        create_reply = replies[1]
        self.assertFalse(create_reply["result"]["isError"])
        text = create_reply["result"]["content"][0]["text"]
        payload = json.loads(text)
        self.assertTrue(payload["ok"])
        tid = payload["task"]["id"]

        list_reply = replies[2]
        listed = json.loads(list_reply["result"]["content"][0]["text"])
        self.assertIn(tid, {t["id"] for t in listed["tasks"]})

    def test_tools_call_error_is_flagged(self) -> None:
        replies = self._session(
            [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "0"},
                    },
                },
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "wl_show",
                        "arguments": {"task_id": "99999"},
                    },
                },
            ]
        )
        self.assertTrue(replies[1]["result"]["isError"])
        self.assertIn("not found", replies[1]["result"]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
