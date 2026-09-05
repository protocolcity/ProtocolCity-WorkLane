"""Lock the mcp/handlers.py → worklane.mcp.handlers package peel."""
from __future__ import annotations

import unittest

from worklane.mcp import handlers as handlers_pkg
from worklane.mcp.handlers import session, tools
from worklane.mcp.handlers.errors import ToolError as ErrCls
from worklane.mcp.handlers.session import TPHandlers as SessionCls


class HandlersPackagePeelTest(unittest.TestCase):
    def test_stable_import_surface(self) -> None:
        self.assertIs(handlers_pkg.TPHandlers, SessionCls)
        self.assertIs(handlers_pkg.ToolError, ErrCls)
        self.assertIs(handlers_pkg.build_tool_definitions, tools.build_tool_definitions)
        self.assertIs(handlers_pkg.dispatch_tool, tools.dispatch_tool)

    def test_tool_catalog_unchanged(self) -> None:
        names = [t["name"] for t in handlers_pkg.build_tool_definitions()]
        self.assertEqual(
            names,
            [
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
            ],
        )

    def test_class_still_composes_read_and_write(self) -> None:
        self.assertTrue(hasattr(SessionCls, "wl_list"))
        self.assertTrue(hasattr(SessionCls, "wl_create"))
        self.assertTrue(hasattr(SessionCls, "wl_close"))
        self.assertTrue(hasattr(SessionCls, "_resolve_task"))


if __name__ == "__main__":
    unittest.main()
