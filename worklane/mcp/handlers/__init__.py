"""Pure tool handlers for the WL MCP server.

Package peel of the former ``worklane/mcp/handlers.py`` monolith.
No MCP transport dependency — unit-tested directly. Existing imports
stay stable: ``from worklane.mcp.handlers import TPHandlers, ToolError,
build_tool_definitions, dispatch_tool``.
"""
from __future__ import annotations

from worklane.mcp.handlers.errors import (
    ToolError,
    _DEFAULT_PRODUCT,
    _DEFAULT_PROJECT,
)
from worklane.mcp.handlers.session import TPHandlers
from worklane.mcp.handlers.tools import build_tool_definitions, dispatch_tool

__all__ = [
    "TPHandlers",
    "ToolError",
    "_DEFAULT_PRODUCT",
    "_DEFAULT_PROJECT",
    "build_tool_definitions",
    "dispatch_tool",
]
