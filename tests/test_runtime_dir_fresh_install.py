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


if __name__ == "__main__":
    unittest.main()
