"""MCP tool errors and connect-time default store names."""
from __future__ import annotations

# Terminal safety-net when a tool omits ``project``, no WL_PROJECT/
# WL_DEFAULT_PROJECT env or products.json default is set, and nothing is
# discovered on disk yet. Deliberately the literal "tradeos", not config-
# driven: trackers/sqlite.py's legacy SQLiteTracker always resolves *some*
# tradeos.db store (see its DEFAULT_DB_PATH fallback chain), and
# products.product_tracker() routes slug == "tradeos" to that guaranteed
# store for the same reason — see its docstring for why comparing against
# the *configured* default here instead would risk a silent collision.
_DEFAULT_PROJECT = "tradeos"
_DEFAULT_PRODUCT = _DEFAULT_PROJECT  # back-compat alias for external callers


class ToolError(Exception):
    """Raised for tool-level failures that should surface to the MCP client."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message
