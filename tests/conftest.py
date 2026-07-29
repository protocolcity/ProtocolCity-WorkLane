"""Hermetic isolation fixtures for the test suite."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_workforce(monkeypatch: pytest.MonkeyPatch) -> None:
    """Block the WorkForce lookup and roster fallback for all tests (wl-282, wl-287).

    _workforce_workers_for_product now has two sources: the HTTP API (blocked
    by pointing WL_WORKFORCE_URL at a refused port) and the local roster file
    (blocked by clearing WL_WORKFORCE_ROSTER and WORKFORCE_PREDIRTY).  Clearing
    both keeps every test hermetic against the machine's live roster regardless
    of what env vars the launch environment exports.

    Tests that need a specific roster (test_create_task_routing_warning.py)
    set WL_WORKFORCE_ROSTER explicitly in setUp and clean it up in tearDown;
    those overrides take precedence and are restored before this fixture runs.
    """
    monkeypatch.setenv("WL_WORKFORCE_URL", "http://127.0.0.1:1")
    monkeypatch.delenv("WL_WORKFORCE_ROSTER", raising=False)
    monkeypatch.delenv("WORKFORCE_PREDIRTY", raising=False)
