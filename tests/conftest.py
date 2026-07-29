"""Hermetic isolation fixtures for the test suite."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_workforce(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the WorkForce lookup at a non-listening address for all tests.

    _workforce_workers_for_product catches all connection errors and returns
    []; pointing it at a refused port makes every test hermetic against the
    machine's live WorkForce roster so the suite is green regardless of what
    is running on port 8797 (wl-282).

    Tests that need a specific roster (test_create_task_routing_warning.py)
    override WL_WORKFORCE_URL in setUp and/or mock urllib.request.urlopen
    directly; those overrides take precedence inside the test body and are
    cleaned up by tearDown before this fixture restores the env.
    """
    monkeypatch.setenv("WL_WORKFORCE_URL", "http://127.0.0.1:1")
