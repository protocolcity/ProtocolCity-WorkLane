"""Demo product store seed (wl-45).

Fixture DBs only — never the live product stores under local/data/.
"""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from worklane import demo as demo_mod
from worklane.cli import wl as wl_cli
from worklane.trackers.sqlite import SQLiteTracker


class DemoSeedTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.data = self.root / "data"
        self.data.mkdir(parents=True)
        self._env_before = {
            k: os.environ.get(k)
            for k in (
                "WORKLANE_RUNTIME_DIR",
                "WL_DEFAULT_PRODUCT",
                "WL_PRODUCT",
            )
        }
        os.environ["WORKLANE_RUNTIME_DIR"] = str(self.root)
        os.environ.pop("WL_DEFAULT_PRODUCT", None)
        os.environ.pop("WL_PRODUCT", None)

    def tearDown(self) -> None:
        for k, v in self._env_before.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    def test_catalog_covers_four_board_statuses(self) -> None:
        statuses = {t.status for t in demo_mod.seed_catalog()}
        self.assertEqual(
            statuses, {"backlog", "in_progress", "in_review", "done"}
        )
        # At least one claimable backlog ticket with a clear title cue.
        backlog = [t for t in demo_mod.seed_catalog() if t.status == "backlog"]
        self.assertTrue(any("Claim me" in t.title for t in backlog))

    def test_bootstrap_seeds_expected_contents(self) -> None:
        report = demo_mod.bootstrap_demo(
            data_dir=self.data, register_meta=False
        )
        self.assertFalse(report["skipped"])
        catalog = demo_mod.seed_catalog()
        self.assertEqual(report["task_count"], len(catalog))
        self.assertEqual(len(report["created"]), len(catalog))

        tracker = SQLiteTracker(
            db_path=Path(report["db_path"]), product_default="product:demo"
        )
        tasks = tracker.list_tasks()
        by_title = {t.title: t for t in tasks}
        for item in catalog:
            self.assertIn(item.title, by_title)
            task = by_title[item.title]
            self.assertEqual(task.status, item.status)
            self.assertEqual(task.priority, item.priority)
            for lab in item.labels:
                self.assertIn(lab, task.labels)
            self.assertIn("product:demo", task.labels)

        # Comments landed on tickets that declare them.
        claim = by_title[[t for t in catalog if "Claim me" in t.title][0].title]
        comments = tracker.list_comments(str(claim.id))
        self.assertTrue(any(c.author == "demo-seed" for c in comments))

        done = by_title[[t for t in catalog if t.status == "done"][0].title]
        done_comments = tracker.list_comments(str(done.id))
        bodies = "\n".join(c.body for c in done_comments)
        self.assertIn("Completed:", bodies)
        self.assertIn("Verification:", bodies)

        by_status = report["by_status"]
        for status in ("backlog", "in_progress", "in_review", "done"):
            self.assertGreaterEqual(by_status.get(status, 0), 1)

    def test_idempotent_skip_without_force(self) -> None:
        first = demo_mod.bootstrap_demo(
            data_dir=self.data, register_meta=False
        )
        second = demo_mod.bootstrap_demo(
            data_dir=self.data, register_meta=False
        )
        self.assertFalse(first["skipped"])
        self.assertTrue(second["skipped"])
        self.assertEqual(second["created"], [])
        self.assertEqual(second["task_count"], first["task_count"])

    def test_force_reseeds(self) -> None:
        demo_mod.bootstrap_demo(data_dir=self.data, register_meta=False)
        # Mutate store so we can tell a re-seed happened.
        path = self.data / "demo.db"
        tr = SQLiteTracker(db_path=path, product_default="product:demo")
        tr.create_task(title="EXTRA should vanish", status="backlog")
        before = len(tr.list_tasks())
        self.assertGreater(before, len(demo_mod.seed_catalog()))

        report = demo_mod.bootstrap_demo(
            data_dir=self.data, force=True, register_meta=False
        )
        self.assertFalse(report["skipped"])
        self.assertEqual(report["task_count"], len(demo_mod.seed_catalog()))
        titles = {t.title for t in tr.list_tasks()}
        self.assertNotIn("EXTRA should vanish", titles)

    def test_refuses_protected_slugs(self) -> None:
        for slug in sorted(demo_mod.PROTECTED_SLUGS):
            with self.assertRaises(demo_mod.DemoError):
                demo_mod.bootstrap_demo(
                    slug=slug, data_dir=self.data, register_meta=False
                )
            # Ensure no protected DB was created.
            self.assertFalse((self.data / f"{slug}.db").exists())

    def test_refuses_protected_db_path_basename(self) -> None:
        bad = self.data / "tradeos.db"
        with self.assertRaises(demo_mod.DemoError):
            demo_mod.bootstrap_demo(
                slug="demo",
                db_path=bad,
                register_meta=False,
            )
        self.assertFalse(bad.exists())

    def test_never_touches_sibling_product_dbs(self) -> None:
        sibling = self.data / "tradeos.db"
        other = SQLiteTracker(db_path=sibling, product_default="product:tradeos")
        other.create_task(title="LIVE keep me", description="do not touch")
        before = other.list_tasks()
        self.assertEqual(len(before), 1)

        demo_mod.bootstrap_demo(
            data_dir=self.data, force=True, register_meta=False
        )
        after = other.list_tasks()
        self.assertEqual(len(after), 1)
        self.assertEqual(after[0].title, "LIVE keep me")
        self.assertEqual(after[0].id, before[0].id)

        # Force re-seed still leaves sibling intact.
        demo_mod.bootstrap_demo(
            data_dir=self.data, force=True, register_meta=False
        )
        after2 = other.list_tasks()
        self.assertEqual(len(after2), 1)
        self.assertEqual(after2[0].title, "LIVE keep me")

    def test_register_meta_writes_overlay(self) -> None:
        report = demo_mod.bootstrap_demo(
            data_dir=self.data, register_meta=True
        )
        self.assertFalse(report["skipped"])
        cfg = self.root / "config" / "products.json"
        self.assertTrue(cfg.is_file())
        text = cfg.read_text(encoding="utf-8")
        self.assertIn("demo", text)
        self.assertIn("Demo", text)


class DemoCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "data").mkdir(parents=True)
        self._env_before = {
            k: os.environ.get(k)
            for k in ("WORKLANE_RUNTIME_DIR", "WL_DEMO_PRODUCT")
        }
        os.environ["WORKLANE_RUNTIME_DIR"] = str(self.root)

    def tearDown(self) -> None:
        for k, v in self._env_before.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    def test_demo_subcommand_seeds(self) -> None:
        parser = wl_cli._build_parser()
        args = parser.parse_args(["demo", "--json"])
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            wl_cli.cmd_demo(args)
        out = buf.getvalue()
        self.assertIn('"slug": "demo"', out)
        self.assertIn('"skipped": false', out)
        self.assertTrue((self.root / "data" / "demo.db").is_file())

    def test_demo_cli_refuses_protected_product(self) -> None:
        parser = wl_cli._build_parser()
        args = parser.parse_args(["demo", "--product", "tradeos"])
        with self.assertRaises(SystemExit) as ctx:
            wl_cli.cmd_demo(args)
        self.assertEqual(ctx.exception.code, 1)
        self.assertFalse((self.root / "data" / "tradeos.db").exists())


if __name__ == "__main__":
    unittest.main()
