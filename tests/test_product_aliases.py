"""Slug aliases + ignored legacy stems (wl-492 / comms cutover)."""

from __future__ import annotations

import unittest

from worklane.board import _PRODUCT_ALIASES, resolve_wq_product
from worklane.products import _IGNORED_DB_STEMS, _is_product_db_stem


class ProductAliasTest(unittest.TestCase):
    def test_davinci_aliases_to_comms(self) -> None:
        self.assertEqual(_PRODUCT_ALIASES.get("davinci"), "comms")

    def test_worklane_alias_unchanged(self) -> None:
        self.assertEqual(_PRODUCT_ALIASES.get("worklane"), "worklane")

    def test_resolve_unknown_still_fail_closed(self) -> None:
        prod, ok = resolve_wq_product("not-a-store")
        self.assertEqual(prod, "")
        self.assertFalse(ok)

    def test_davinci_db_stem_ignored(self) -> None:
        self.assertIn("davinci", _IGNORED_DB_STEMS)
        self.assertFalse(_is_product_db_stem("davinci"))
        self.assertTrue(_is_product_db_stem("comms"))
