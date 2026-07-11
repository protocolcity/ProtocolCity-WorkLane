"""WorkLane MCP server — agent-native ticket access (wl-19).

Stdio MCP over the product trackers. Author identity is required at
connect time (``--author`` / ``WL_AGENT_ID``) and signs every write per
PROCESS.md §3.8. Tools enforce the §5 close-out contract structurally
via ``wl_close``.
"""

from __future__ import annotations

__all__ = ["main"]


def main() -> None:
    from worklane.mcp.server import main as _main

    _main()
