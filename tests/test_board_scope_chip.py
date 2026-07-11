"""wl-55: scoped views drop their own product:<slug> chip from cards/rows.

The tradeOS tab saying product:tradeos on every card is noise; the All view
keeps the chip because there it disambiguates. The rule keys off the parsed
scope slug, so any future auto-discovered product behaves identically.
"""

from __future__ import annotations

import unittest

from worklane.board import _render_task_card, _scoped_labels
from worklane.trackers.protocol import Task, TaskStatus


class ScopedLabelsTest(unittest.TestCase):
    def test_scope_drops_only_its_own_chip(self) -> None:
        labels = ["product:tradeos", "area:board", "product:worklane"]
        self.assertEqual(
            _scoped_labels(labels, "tradeos"),
            ["area:board", "product:worklane"],
        )

    def test_all_view_keeps_everything(self) -> None:
        labels = ["product:tradeos", "area:board"]
        self.assertEqual(_scoped_labels(labels, ""), labels)

    def test_empty_labels(self) -> None:
        self.assertEqual(_scoped_labels(None, "tradeos"), [])


class CardChipTest(unittest.TestCase):
    def _card(self, scope: str) -> str:
        task = Task(
            id="9", title="t", status=TaskStatus.BACKLOG,
            labels=["product:tradeos", "area:board"],
        )
        return _render_task_card(task, {}, scope)

    def test_scoped_card_hides_product_chip(self) -> None:
        html = self._card("tradeos")
        self.assertNotIn("product:tradeos", html)
        self.assertIn("area:board", html)

    def test_all_view_card_shows_product_chip(self) -> None:
        html = self._card("")
        self.assertIn("product:tradeos", html)

    def test_overflow_count_uses_filtered_list(self) -> None:
        task = Task(
            id="9", title="t", status=TaskStatus.BACKLOG,
            labels=["product:tradeos", "a", "b", "c", "d"],
        )
        # 5 labels, scope removes one → exactly 4 chips, no "+1" overflow.
        html = _render_task_card(task, {}, "tradeos")
        self.assertNotIn("tb-card-more'>+", html)


if __name__ == "__main__":
    unittest.main()
