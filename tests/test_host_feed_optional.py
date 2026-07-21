"""host_feed plugin: Phase B guard and no-tradeOS-present behaviour (wl-223).

Validates that:
- plugins/host_feed imports cleanly with no tradeOS process running.
- tradeos_configured() reflects the environment correctly.
- Feed functions return empty/safe values when no host is reachable.
- _fetch_tradeos_ops_snapshot() returns a dict keyed by data type.
- task_server imports successfully (the try/except ImportError guard works).
"""

from __future__ import annotations

import os
import sys
import unittest


class TradeosConfiguredTest(unittest.TestCase):
    def _call(self) -> bool:
        from worklane.plugins.host_feed import tradeos_configured
        return tradeos_configured()

    def test_false_when_tradeos_host_not_set(self) -> None:
        env_backup = os.environ.pop("TRADEOS_HOST", None)
        try:
            self.assertFalse(self._call())
        finally:
            if env_backup is not None:
                os.environ["TRADEOS_HOST"] = env_backup

    def test_true_when_tradeos_host_set(self) -> None:
        original = os.environ.get("TRADEOS_HOST")
        os.environ["TRADEOS_HOST"] = "10.0.0.1"
        try:
            self.assertTrue(self._call())
        finally:
            if original is None:
                del os.environ["TRADEOS_HOST"]
            else:
                os.environ["TRADEOS_HOST"] = original


class FeedFallbackTest(unittest.TestCase):
    """Feed functions return empty results when no tradeOS process is running."""

    def setUp(self) -> None:
        # Point at an unroutable address so network calls fail fast.
        self._orig_host = os.environ.get("TRADEOS_HOST")
        self._orig_port = os.environ.get("TRADEOS_PORT")
        os.environ["TRADEOS_HOST"] = "127.0.0.1"
        os.environ["TRADEOS_PORT"] = "19999"  # nothing listening here

    def tearDown(self) -> None:
        if self._orig_host is None:
            os.environ.pop("TRADEOS_HOST", None)
        else:
            os.environ["TRADEOS_HOST"] = self._orig_host
        if self._orig_port is None:
            os.environ.pop("TRADEOS_PORT", None)
        else:
            os.environ["TRADEOS_PORT"] = self._orig_port

    def test_fetch_tradeos_json_returns_none_when_unreachable(self) -> None:
        from worklane.plugins.host_feed import _fetch_tradeos_json
        result = _fetch_tradeos_json("/api/ops/status", timeout=0.3)
        self.assertIsNone(result)

    def test_fetch_tradeos_ops_snapshot_returns_keyed_dict(self) -> None:
        from worklane.plugins.host_feed import _fetch_tradeos_ops_snapshot
        snap = _fetch_tradeos_ops_snapshot()
        self.assertIsInstance(snap, dict)
        for key in ("status", "positions", "trades", "signals"):
            self.assertIn(key, snap)
            self.assertIsNone(snap[key])  # unreachable host → all None

    def test_tradeos_tickets_use_http_feed_false_by_default(self) -> None:
        from worklane.plugins.host_feed import _tradeos_tickets_use_http_feed
        env_backup = os.environ.pop("TRADEOS_TICKETS_SOURCE", None)
        try:
            self.assertFalse(_tradeos_tickets_use_http_feed())
        finally:
            if env_backup is not None:
                os.environ["TRADEOS_TICKETS_SOURCE"] = env_backup

    def test_fetch_tradeos_tasks_via_http_returns_empty_when_unreachable(self) -> None:
        from worklane.plugins.host_feed import _fetch_tradeos_tasks_via_http
        tasks, previews = _fetch_tradeos_tasks_via_http(
            status=None, label=None, priority=None, limit=10, with_preview=False
        )
        self.assertEqual(tasks, [])
        self.assertEqual(previews, {})


class TaskServerImportTest(unittest.TestCase):
    """task_server must be importable even without a running tradeOS."""

    def test_task_server_imports_without_error(self) -> None:
        # Force a fresh import in case the module was already loaded.
        mod_name = "worklane.task_server"
        if mod_name in sys.modules:
            # Already imported — just confirm the expected names are present.
            mod = sys.modules[mod_name]
        else:
            import importlib
            mod = importlib.import_module(mod_name)
        # Verify the guarded names exist (either real or stubs).
        for name in (
            "_tradeos_api_base",
            "_fetch_tradeos_json",
            "_tradeos_tickets_use_http_feed",
            "_fetch_tradeos_ops_snapshot",
            "_list_tasks_for_wq_multi_resolved",
        ):
            self.assertTrue(
                hasattr(mod, name),
                f"task_server missing {name!r} after import",
            )

    def test_stub_fetch_tradeos_ops_snapshot_returns_dict(self) -> None:
        """When host_feed is present the live version is used; when absent the
        stub returns {}.  Either way the caller gets a dict."""
        import worklane.task_server as ts
        result = ts._fetch_tradeos_ops_snapshot()
        self.assertIsInstance(result, dict)


if __name__ == "__main__":
    unittest.main()
