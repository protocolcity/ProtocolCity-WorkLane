"""wl-124: runtime data dir + fresh-install DB filename resolution.

Covers the two gaps a ``pip install`` of the exported package hits that a
source checkout never does:

- :func:`products.wl_data_dir` must not default inside the installed
  package (site-packages) — it should fall back to a user-level dir.
- :class:`SQLiteTracker`'s truly-fresh-install fallback (nothing on disk
  anywhere) must name the DB after the configured default product, not the
  ``tradeos.db`` literal.

Existing hosts are unaffected: both defaults are only reached when their
respective "nothing exists yet" branch is taken, which a live checkout
with real data on disk never hits.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import worklane.products as products
import worklane.trackers.sqlite as sqlite_mod


class TpDataDirTest(unittest.TestCase):
    def setUp(self) -> None:
        self._prev = os.environ.get("WORKLANE_RUNTIME_DIR")
        os.environ.pop("WORKLANE_RUNTIME_DIR", None)

    def tearDown(self) -> None:
        if self._prev is None:
            os.environ.pop("WORKLANE_RUNTIME_DIR", None)
        else:
            os.environ["WORKLANE_RUNTIME_DIR"] = self._prev

    def test_source_checkout_keeps_in_repo_default(self) -> None:
        with patch.object(products, "_is_source_checkout", return_value=True):
            self.assertEqual(
                products.wl_data_dir(),
                Path(products.__file__).parent / "local" / "data",
            )

    def test_installed_package_uses_user_level_dir(self) -> None:
        with patch.object(products, "_is_source_checkout", return_value=False):
            self.assertEqual(
                products.wl_data_dir(), Path.home() / ".worklane" / "data"
            )

    def test_runtime_dir_override_wins_regardless_of_checkout_detection(self) -> None:
        os.environ["WORKLANE_RUNTIME_DIR"] = "/tmp/pinned-host"
        with patch.object(products, "_is_source_checkout", return_value=False):
            self.assertEqual(products.wl_data_dir(), Path("/tmp/pinned-host/data"))


class FreshInstallDbFilenameTest(unittest.TestCase):
    """Exercises SQLiteTracker's ``db_path=None`` branch with every
    existence check patched false, simulating a genuinely fresh install."""

    def _fresh_tracker(self, tmp: Path):
        missing = tmp / "data" / "nothing-here.db"
        with patch.object(sqlite_mod, "DEFAULT_DB_PATH", missing), patch.object(
            sqlite_mod, "LEGACY_HIDDEN_DB_PATH", missing
        ), patch.object(sqlite_mod, "LEGACY_DB_PATH", missing), patch.dict(
            os.environ, {}, clear=False
        ):
            os.environ.pop("WORKLANE_DB", None)
            os.environ.pop("TRADEOS_TRACKER_DB", None)
            return sqlite_mod.SQLiteTracker()

    def test_fresh_install_routes_through_default_product_slug(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(products, "default_product_slug", return_value="myapp"), \
                 patch.object(products, "wl_data_dir", return_value=root / "data"):
                tracker = self._fresh_tracker(root)
        self.assertEqual(tracker._db_path, root / "data" / "myapp.db")

    def test_fresh_install_with_no_configured_default_still_uses_tradeos(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing = root / "data" / "nothing-here.db"
            with patch.object(products, "default_product_slug", return_value=""), \
                 patch.object(sqlite_mod, "DEFAULT_DB_PATH", missing):
                tracker = self._fresh_tracker(root)
        self.assertEqual(tracker._db_path, missing)


class EmptyRuntimeOverrideTest(unittest.TestCase):
    """wl-374: empty RUNTIME_DIR override must fail loud / self-diagnose."""

    _ENV_KEYS = (
        "WORKLANE_RUNTIME_DIR",
        "WORKLANE_RUNTIME_DIR",
        "WL_DEFAULT_PROJECT",
        "WL_DEFAULT_PRODUCT",
        "WL_PROJECT",
        "WL_PRODUCT",
        "WL_DEFAULT_PROJECT",
        "WL_DEFAULT_PRODUCT",
        "WL_PROJECT",
        "WL_PRODUCT",
    )

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._env_before = {k: os.environ.get(k) for k in self._ENV_KEYS}
        for k in self._ENV_KEYS:
            os.environ.pop(k, None)
        products.reset_empty_runtime_override_warning_for_tests()

    def tearDown(self) -> None:
        for k, v in self._env_before.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        products.reset_empty_runtime_override_warning_for_tests()
        self._tmp.cleanup()

    def test_empty_override_detected_and_warns(self) -> None:
        empty = self.root / "empty-runtime"
        empty.mkdir()
        os.environ["WORKLANE_RUNTIME_DIR"] = str(empty)
        # Default env would still invent tradeos — that is the silent
        # single-product trap; empty-override detection must still fire.
        os.environ["WL_DEFAULT_PRODUCT"] = "tradeos"

        self.assertTrue(products.is_empty_runtime_override())
        warn = products.empty_runtime_override_warning()
        assert warn is not None
        self.assertIn(str(empty), warn)
        self.assertIn("empty RUNTIME_DIR", warn)

        # One-shot emit
        self.assertTrue(products.emit_empty_runtime_override_warning())
        self.assertFalse(products.emit_empty_runtime_override_warning())

    def test_override_with_products_json_not_empty(self) -> None:
        rt = self.root / "with-config"
        (rt / "config").mkdir(parents=True)
        (rt / "config" / "products.json").write_text(
            '{"default": "tradeos"}', encoding="utf-8"
        )
        os.environ["WORKLANE_RUNTIME_DIR"] = str(rt)
        self.assertFalse(products.is_empty_runtime_override())
        self.assertIsNone(products.empty_runtime_override_warning())

    def test_override_with_product_db_not_empty(self) -> None:
        rt = self.root / "with-db"
        data = rt / "data"
        data.mkdir(parents=True)
        (data / "worklane.db").write_bytes(b"")  # presence is enough
        os.environ["WORKLANE_RUNTIME_DIR"] = str(rt)
        self.assertFalse(products.is_empty_runtime_override())

    def test_no_override_never_flags_empty(self) -> None:
        # Even with no env pin, do not treat package/checkout defaults as miswired.
        self.assertEqual(products.runtime_dir_override(), "")
        self.assertFalse(products.is_empty_runtime_override())

    def test_unknown_product_message_includes_runtime_dir(self) -> None:
        empty = self.root / "miswired"
        empty.mkdir()
        os.environ["WORKLANE_RUNTIME_DIR"] = str(empty)
        os.environ["WL_DEFAULT_PRODUCT"] = "tradeos"

        msg = products.unknown_product_message("protocolcity", ["tradeos"])
        self.assertIn("unknown product 'protocolcity'", msg)
        self.assertIn("known: ['tradeos']", msg)
        self.assertIn(f"runtime_dir={empty}", msg)
        self.assertIn("empty RUNTIME_DIR override", msg)

    def test_mcp_handler_unknown_product_includes_runtime_and_one_time_hint(self) -> None:
        from worklane.mcp.handlers import (
            TPHandlers,
            ToolError,
            dispatch_tool,
        )

        empty = self.root / "mcp-empty"
        empty.mkdir()
        os.environ["WORKLANE_RUNTIME_DIR"] = str(empty)
        os.environ["WL_DEFAULT_PRODUCT"] = "tradeos"
        # Avoid inheriting a live DB path from the host env.
        os.environ.pop("WORKLANE_DB", None)
        os.environ.pop("TRADEOS_TRACKER_DB", None)

        h = TPHandlers(author="lili", default_product="tradeos")
        with self.assertRaises(ToolError) as cm:
            h.wl_counts(product="protocolcity")
        self.assertIn("runtime_dir=", cm.exception.message)
        self.assertIn(str(empty), cm.exception.message)

        # First successful tool result carries one-time runtime_warning.
        first = dispatch_tool(h, "wl_counts", {"product": "tradeos"})
        self.assertIn("runtime_warning", first)
        self.assertIn(str(empty), first["runtime_warning"])
        second = dispatch_tool(h, "wl_counts", {"product": "tradeos"})
        self.assertNotIn("runtime_warning", second)


if __name__ == "__main__":
    unittest.main()
