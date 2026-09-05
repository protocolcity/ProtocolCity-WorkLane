"""Lock the D1 page-shell extract (task_server → surfaces.shell)."""
from __future__ import annotations

import unittest

from worklane.surfaces import shell as page_shell
from worklane.surfaces.chrome import _BRAND_MODE as CHROME_BRAND_MODE
from worklane import task_server


class PageShellExtractTest(unittest.TestCase):
    def test_task_server_reexports_shell_symbols(self) -> None:
        """Existing callers import chrome helpers from task_server."""
        self.assertIs(task_server._task_page, page_shell._task_page)
        self.assertIs(
            task_server._split_for_middle_truncate,
            page_shell._split_for_middle_truncate,
        )
        self.assertEqual(
            task_server._SCOPE_NAV_MAX_INLINE,
            page_shell._SCOPE_NAV_MAX_INLINE,
        )
        self.assertIs(
            task_server._ticket_create_surface_from_scope,
            page_shell._ticket_create_surface_from_scope,
        )

    def test_brand_tokens_come_from_chrome(self) -> None:
        """No second copy of WL_BRAND tokens next to the shell."""
        self.assertIs(page_shell._BRAND_MODE, CHROME_BRAND_MODE)
        self.assertIs(task_server._BRAND_MODE, CHROME_BRAND_MODE)

    def test_split_for_middle_truncate(self) -> None:
        head, tail = page_shell._split_for_middle_truncate("WorkLane → WorkLane")
        self.assertEqual(head, "WorkLane")
        self.assertEqual(tail, " → WorkLane")
        head, tail = page_shell._split_for_middle_truncate("Socials")
        self.assertEqual((head, tail), ("Socials", ""))

    def test_task_page_renders_shell_chrome(self) -> None:
        html = page_shell._task_page(
            "Probe",
            "<p>body</p>",
            nav_active="overview",
            shell="overview",
            page_scope="all",
        )
        self.assertIn("<!doctype html>", html)
        self.assertIn('data-ops-shell="overview"', html)
        self.assertIn('data-ops-scope="all"', html)
        self.assertIn("<p>body</p>", html)
        self.assertIn("/admin/overview/all", html)
        self.assertIn("ts-primary-shell", html)


if __name__ == "__main__":
    unittest.main()
