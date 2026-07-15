"""Public WorkLane verb aliases for MCP tools (wl-176 / wl-143).

Shipped public surface uses ``wl_*``; ``wl_*`` remains the internal
canonical name and keeps working as a silent back-compat path. Alias
registration is pure data + name rewrite — same handlers, no behavior
fork.

On the WorkLane public export, branding rewrites every ``wl_*`` token to
``wl_*`` before ship, so the dual-prefix catalog is private-only. When the
catalog no longer contains any internal-prefixed tool, alias expansion is
a no-op (export already wears the public names).

Prefix literals are built by concatenation so the export branding pass
(``wl_`` → ``wl_`` on source tokens) cannot rewrite the *detector* itself
into a always-true check against the already-public catalog.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List

# Concat form survives export branding (which rewrites the token "wl_").
_TP_PREFIX = "t" + "p_"
_WL_PREFIX = "w" + "l_"


def _catalog_has_internal_prefix(tools: List[Dict[str, Any]]) -> bool:
    return any((t.get("name") or "").startswith(_TP_PREFIX) for t in tools)


def canonical_tool_name(
    name: str, *, internal_catalog: bool = True
) -> str:
    """Resolve a tools/call name to the handler table key.

    ``internal_catalog=True`` (private WL): ``wl_list`` → ``wl_list``.
    ``internal_catalog=False`` (public export): names already ``wl_*``.
    """
    if not isinstance(name, str):
        return name
    if internal_catalog and name.startswith(_WL_PREFIX):
        return _TP_PREFIX + name[len(_WL_PREFIX) :]
    return name


def with_wl_tool_aliases(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return ``tools`` plus a ``wl_*`` clone of every ``wl_*`` definition.

    Clones share the same ``inputSchema`` and route to the same handler via
    :func:`canonical_tool_name`. Order: canonical tools first, then aliases
    in the same relative order.

    No-op when the catalog has no ``wl_*`` names (public export already
    rewrote tools to ``wl_*``).
    """
    if not _catalog_has_internal_prefix(tools):
        return list(tools)
    out: List[Dict[str, Any]] = list(tools)
    for tool in tools:
        name = tool.get("name") or ""
        if not name.startswith(_TP_PREFIX):
            continue
        alias = copy.deepcopy(tool)
        alias_name = _WL_PREFIX + name[len(_TP_PREFIX) :]
        alias["name"] = alias_name
        desc = (alias.get("description") or "").strip()
        # Avoid a contiguous "wl_" token in this format string so export
        # branding does not mangle the alias description on the public tree.
        prefix = (
            "Public alias of %s (WorkLane surface; same handler)." % name
        )
        alias["description"] = f"{prefix} {desc}".strip() if desc else prefix
        out.append(alias)
    return out
