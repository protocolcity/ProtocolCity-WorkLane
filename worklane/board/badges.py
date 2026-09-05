"""Status / priority / label badge helpers for the board."""
from __future__ import annotations

from typing import List

from worklane.board.constants import _PRIORITY_LABELS, _PRIORITY_TIERS, _STATUS_LABELS, _STATUS_TIERS
from worklane.rendering import _badge, _label_chip

def _label_tier(label: str) -> str:
    s = label.lower()
    if s.startswith("product:"):
        return "positive"
    if s.startswith("area:"):
        return "info"
    if s.startswith("sys:"):
        return "warning"
    if s in ("bug",):
        return "critical"
    if s in ("feature",):
        return "positive"
    return "neutral"


def _render_labels(labels: List[str]) -> str:
    if not labels:
        return "<span class='dim'>—</span>"
    return " ".join(_label_chip(l, _label_tier(l)) for l in labels)


def _render_status_badge(status: str) -> str:
    return _badge(_STATUS_LABELS.get(status, status), _STATUS_TIERS.get(status, "neutral"))


def _render_priority_badge(priority: int) -> str:
    p = int(priority or 0)
    return _badge(_PRIORITY_LABELS.get(p, "—"), _PRIORITY_TIERS.get(p, "neutral"))

