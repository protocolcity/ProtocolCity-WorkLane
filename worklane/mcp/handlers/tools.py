"""MCP tool schemas and dispatch (project/product alias resolution)."""
from __future__ import annotations

from typing import Any, Dict, List

from worklane.mcp.handlers.errors import ToolError
from worklane.mcp.handlers.session import TPHandlers
from worklane.trackers.protocol import TaskStatus

def build_tool_definitions() -> List[Dict[str, Any]]:
    """MCP tools/list payload — schemas for work + triage tools."""
    project_prop = {
        "type": "string",
        "description": (
            "Project store slug (e.g. worklane, tradeos) — canonical "
            "name (wl-64). 'product' is a silent back-compat alias for this "
            "same field; passing both with different values is an error. "
            "On write tools with a bare task_id, project= is required (wl-344) — "
            "connect-time default alone is not enough. With a composite id, "
            "omit or pass the matching store. "
            "wl_list/wl_ready/wl_mine/wl_counts also accept 'all'."
        ),
    }
    product_prop = {
        "type": "string",
        "description": (
            "Back-compat alias for 'project' (PROTOCOL.md §5.2 — same field, "
            "same meaning). Prefer 'project' in new integrations."
        ),
    }
    task_id_prop = {
        "type": "string",
        "description": (
            "Ticket id — prefer composite (wl-328, ts-12). Bare numeric ids "
            "on write tools require an explicit project= (or product= alias); "
            "default-store fallback is refused to stop cross-store bleed (wl-344). "
            "Reads may still omit project when the connect-time default is intended."
        ),
    }

    return [
        {
            "name": "wl_list",
            "description": (
                "List WorkLane tickets. Filter by status, label, "
                "priority, or gate class. Returns composite ids."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project": project_prop,
                    "product": product_prop,
                    "status": {
                        "type": "string",
                        "enum": list(TaskStatus.ALL),
                        "description": "Filter by status",
                    },
                    "label": {"type": "string", "description": "Filter by label"},
                    "priority": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 4,
                        "description": "1=urgent … 4=low",
                    },
                    "gate_type": {
                        "type": "string",
                        "enum": ["", "human", "timer", "deferred", "tracking"],
                        "description": (
                            "Filter by gate class: 'deferred' = parked tickets; "
                            "'tracking' = structural epic umbrellas (wl-434); "
                            "'human' = act-now gates; 'timer' = embargoed; "
                            "'' = ungated (no active gate)"
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 500,
                        "default": 50,
                    },
                },
            },
        },
        {
            "name": "wl_ready",
            "description": (
                "List backlog tickets ready for dispatch (declared blockers "
                "all done). Prefer this over raw backlog scan."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project": project_prop,
                    "product": product_prop,
                    "label": {"type": "string"},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 200,
                        "default": 20,
                    },
                },
            },
        },
        {
            "name": "wl_show",
            "description": "Show full ticket detail including comment trail.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": task_id_prop,
                    "project": project_prop,
                    "product": product_prop,
                    "comments_limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 200,
                        "default": 50,
                    },
                },
                "required": ["task_id"],
            },
        },
        {
            "name": "wl_create",
            "description": (
                "File a new ticket with signed intake. Description is "
                "required (problem + expected outcome)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "project": project_prop,
                    "product": product_prop,
                    "priority": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 4,
                        "default": 3,
                    },
                    "labels": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Prefer include worker:<hand-id> so scheduled hands "
                            "drain the ticket. Required when hands are hired for "
                            "this product — omit a seat and create is rejected "
                            "with valid seat options. Pre-hire: stamps needs:routing."
                        ),
                    },
                    "intake": {
                        "type": "string",
                        "description": (
                            "Entry channel — how the ticket entered the system. "
                            "Defaults to 'mcp' for MCP callers. "
                            "Values: mcp | cli | api | agent | import | unknown"
                        ),
                    },
                },
                "required": ["title", "description"],
            },
        },
        {
            "name": "wl_claim",
            "description": (
                "Claim a ticket: move to in_progress and post a signed "
                "Owner marker (PROTOCOL.md §2/§5)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": task_id_prop,
                    "project": project_prop,
                    "product": product_prop,
                    "plan": {
                        "type": "string",
                        "description": "Plan bullets (newline-separated)",
                    },
                    "workdir": {
                        "type": "string",
                        "description": "Absolute working-copy path for Owner marker",
                    },
                    "branch": {
                        "type": "string",
                        "description": "Branch name if not working on main",
                    },
                },
                "required": ["task_id"],
            },
        },
        {
            "name": "wl_comment",
            "description": (
                "Post a signed comment. For close-outs prefer wl_close; "
                "for blockers include 'Blocked:' + 'Next step:'."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": task_id_prop,
                    "body": {"type": "string"},
                    "project": project_prop,
                    "product": product_prop,
                },
                "required": ["task_id", "body"],
            },
        },
        {
            "name": "wl_close",
            "description": (
                "Close a ticket with structured PROTOCOL.md §5 sections. "
                "Malformed close-outs are rejected by construction."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": task_id_prop,
                    "completed": {
                        "type": "string",
                        "description": "What changed (files/surfaces)",
                    },
                    "verification": {
                        "type": "string",
                        "description": "Commands/tests run + result",
                    },
                    "links": {
                        "type": "string",
                        "description": "PR URL, commit SHA, or repo-relative path",
                    },
                    "follow_ups": {
                        "type": "string",
                        "description": "Ticket refs or 'none'",
                        "default": "none",
                    },
                    "project": project_prop,
                    "product": product_prop,
                },
                "required": ["task_id", "completed", "verification", "links"],
            },
        },
        {
            "name": "wl_release",
            "description": (
                "Release a claim back to backlog. Optional reason+next_step "
                "posts a Blocked: comment."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": task_id_prop,
                    "project": project_prop,
                    "product": product_prop,
                    "reason": {"type": "string"},
                    "next_step": {"type": "string"},
                },
                "required": ["task_id"],
            },
        },
        {
            "name": "wl_label",
            "description": (
                "Add and/or remove labels on an existing ticket (lane routing, "
                "area tags). At least one of add/remove required."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": task_id_prop,
                    "project": project_prop,
                    "product": product_prop,
                    "add": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Labels to add",
                    },
                    "remove": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Labels to remove",
                    },
                },
                "required": ["task_id"],
            },
        },
        {
            "name": "wl_update",
            "description": (
                "Edit title, description, priority, and/or gate on an existing "
                "ticket (triage re-scoping). At least one field required. "
                "gate_type controls dispatch: '' clears the gate; 'human' withholds "
                "until manually cleared AND surfaces in For You (act-now); "
                "'timer' withholds until gate_until then auto-thaws; "
                "'deferred' parks the ticket — withholds from ready AND never enters "
                "For You / Map gold (PROCESS §3.9 Deferred class, wl-261); "
                "'tracking' marks a structural epic umbrella — never ready, never "
                "For You, still listable for decomposition (wl-434). "
                "Use deferred when work is real but not yet actionable; use tracking "
                "for coordination wrappers that must not be claimed; use human "
                "only when founder action is needed now. To thaw a deferred/tracking "
                "ticket, call wl_update with gate_type='' (clears the gate)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": task_id_prop,
                    "project": project_prop,
                    "product": product_prop,
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "priority": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 4,
                        "description": "1=urgent … 4=low",
                    },
                    "gate_type": {
                        "type": "string",
                        "enum": ["", "human", "timer", "deferred", "tracking"],
                        "description": (
                            "'' clears the gate; 'human' = act-now (surfaces in For You); "
                            "'timer' = embargoed until gate_until; "
                            "'deferred' = parked (withholds ready, never enters For You); "
                            "'tracking' = structural epic (withholds ready, never For You)"
                        ),
                    },
                    "gate_until": {
                        "type": "string",
                        "description": "ISO timestamp; required when gate_type is 'timer'",
                    },
                    "gate_note": {
                        "type": "string",
                        "description": (
                            "Optional context for the gate. For human gates: "
                            "describe what decision or action is needed. "
                            "For deferred gates: describe what condition would thaw it. "
                            "For tracking gates: optional epic/track context."
                        ),
                    },
                },
                "required": ["task_id"],
            },
        },
        {
            "name": "wl_cancel",
            "description": (
                "Cancel a ticket (* → canceled) with a signed rationale. "
                "Does not apply to done tickets."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": task_id_prop,
                    "project": project_prop,
                    "product": product_prop,
                    "reason": {
                        "type": "string",
                        "description": "Short rationale (required)",
                    },
                },
                "required": ["task_id", "reason"],
            },
        },
        {
            "name": "wl_reopen",
            "description": (
                "Reopen a closed ticket (done or canceled → backlog). "
                "Optional reason is recorded as a signed comment."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": task_id_prop,
                    "project": project_prop,
                    "product": product_prop,
                    "reason": {
                        "type": "string",
                        "description": "Optional reopen note",
                    },
                },
                "required": ["task_id"],
            },
        },
        {
            "name": "wl_reserve",
            "description": (
                "Soft-lock a ticket to in_review without starting work "
                "(PROTOCOL.md §2 reserve / bundle). Promote later with wl_claim."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": task_id_prop,
                    "project": project_prop,
                    "product": product_prop,
                    "note": {
                        "type": "string",
                        "description": "Optional reserve note (why soft-locked)",
                    },
                },
                "required": ["task_id"],
            },
        },
        {
            "name": "wl_park",
            "description": (
                "Park a live ticket (in_progress → in_review) for bundle rotate. "
                "Does not return it to the free pool — use wl_release for that."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": task_id_prop,
                    "project": project_prop,
                    "product": product_prop,
                    "reason": {
                        "type": "string",
                        "description": "Optional park reason",
                    },
                },
                "required": ["task_id"],
            },
        },
        {
            "name": "wl_mine",
            "description": (
                "List open tickets owned by this agent (latest Owner: marker on "
                "in_progress/in_review). For session resume and ghost-audit."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project": project_prop,
                    "product": product_prop,
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 200,
                        "default": 50,
                    },
                },
            },
        },
        {
            "name": "wl_counts",
            "description": (
                "Status histogram for a product (or all). Counts only — cheap "
                "board pulse without listing tickets."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project": project_prop,
                    "product": product_prop,
                },
            },
        },
    ]


def dispatch_tool(handlers: TPHandlers, name: str, arguments: Dict[str, Any]) -> Any:
    """Route a tools/call to the matching handler method.

    ``project`` (wl-64) is the canonical name for what every handler still
    takes as ``product`` internally; ``product`` remains a silent back-compat
    alias. Resolved here, once, rather than renaming the parameter on all 16
    handler methods — same field, same store lookup, lower surface area.
    Passing both with different values is rejected rather than silently
    picking one (PROTOCOL.md §5.2 alias-precedence rule, wl-64).
    """
    import inspect

    args = dict(arguments or {})
    if "project" in args:
        project_val = args.pop("project")
        product_val = args.get("product")
        if (
            project_val not in (None, "")
            and product_val not in (None, "")
            and str(project_val).strip().lower() != str(product_val).strip().lower()
        ):
            raise ToolError(
                f"conflicting project/product values: project={project_val!r} "
                f"product={product_val!r} — pass only one"
            )
        if project_val not in (None, ""):
            args["product"] = project_val
    table = {
        "wl_list": handlers.wl_list,
        "wl_ready": handlers.wl_ready,
        "wl_show": handlers.wl_show,
        "wl_create": handlers.wl_create,
        "wl_claim": handlers.wl_claim,
        "wl_comment": handlers.wl_comment,
        "wl_close": handlers.wl_close,
        "wl_release": handlers.wl_release,
        "wl_label": handlers.wl_label,
        "wl_update": handlers.wl_update,
        "wl_cancel": handlers.wl_cancel,
        "wl_reopen": handlers.wl_reopen,
        "wl_reserve": handlers.wl_reserve,
        "wl_park": handlers.wl_park,
        "wl_mine": handlers.wl_mine,
        "wl_counts": handlers.wl_counts,
    }
    fn = table.get(name)
    if fn is None:
        raise ToolError(f"unknown tool: {name}")
    # Drop unknown keys so clients sending extra fields don't TypeError.
    sig = inspect.signature(fn)
    accepted = {k: v for k, v in args.items() if k in sig.parameters}
    result = fn(**accepted)
    # wl-374: one-time tool-result hint naming the resolved runtime dir when
    # an empty RUNTIME_DIR override would otherwise look like a healthy
    # single-product registry.
    hint = handlers._consume_empty_override_hint()
    if hint and isinstance(result, dict):
        out = dict(result)
        out["runtime_warning"] = hint
        return out
    return result
