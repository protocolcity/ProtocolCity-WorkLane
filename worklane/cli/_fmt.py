"""Shared CLI display formatters (wl-411).

Single home for priority/label pretty-print used by both
:mod:`worklane.cli.task` (SQLite CLI) and :mod:`worklane.cli.wl`
(HTTP CLI). Kept stdlib-only so ``wl`` stays free of tracker imports.
"""
from __future__ import annotations

from typing import Optional, Sequence

_PRIORITY_NAMES = {1: "urgent", 2: "high", 3: "normal", 4: "low"}


def _fmt_priority(p: int) -> str:
    return f"P{p} ({_PRIORITY_NAMES.get(p, '?')})"


def _fmt_labels(labels: Optional[Sequence[str]]) -> str:
    return ", ".join(labels) if labels else "(none)"
