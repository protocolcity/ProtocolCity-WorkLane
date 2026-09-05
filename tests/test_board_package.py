"""Lock the board.py → worklane.board package peel."""
from __future__ import annotations

import os
import unittest
from pathlib import Path

import worklane
import worklane.board as board
from worklane.board import assets, constants, ops, product, queries, render


class BoardPackagePeelTest(unittest.TestCase):
    def test_stable_import_surface(self) -> None:
        """Callers keep importing helpers from worklane.board."""
        self.assertIs(board._render_task_card, render._render_task_card)
        self.assertIs(board._board_styles, assets._board_styles)
        self.assertIs(board._client_js, assets._client_js)
        self.assertIs(board.resolve_wq_product, product.resolve_wq_product)
        self.assertIs(board._PRODUCT_ALIASES, product._PRODUCT_ALIASES)
        self.assertIs(board._parse_gate_filter, queries._parse_gate_filter)
        self.assertIs(board.ops_tickets_db_path, ops.ops_tickets_db_path)
        self.assertIs(board.TICKETS_APP_ALL, constants.TICKETS_APP_ALL)

    def test_ops_db_default_stays_under_worklane_pkg(self) -> None:
        """Peel must not relocate worklane/local/data/ops_tickets.db."""
        prev = os.environ.pop("OPS_TICKETS_DB", None)
        try:
            pkg_root = Path(worklane.__file__).resolve().parent
            expected = pkg_root / "local" / "data" / "ops_tickets.db"
            self.assertEqual(board.ops_tickets_db_path(), expected)
            self.assertEqual(
                board.ops_tickets_db_path().parent.parent.parent, pkg_root
            )
        finally:
            if prev is not None:
                os.environ["OPS_TICKETS_DB"] = prev

    def test_product_alias_and_card_still_work(self) -> None:
        prod, ok = board.resolve_wq_product("wl")
        self.assertTrue(ok)
        self.assertEqual(prod, "worklane")
        self.assertEqual(board._parse_gate_filter("none"), "")
        self.assertEqual(board._parse_gate_filter("human"), "human")


if __name__ == "__main__":
    unittest.main()
