"""JSONL export/import round-trips (wl-22).

Uses fixture SQLite DBs only — never the live tasks.db / product stores.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List

from worklane import portability
from worklane.trackers.sqlite import SQLiteTracker


def _strip_ids(obj: Dict[str, Any]) -> Dict[str, Any]:
    """Drop task/comment ids for content comparison across renumbering."""
    out = dict(obj)
    out.pop("id", None)
    comments = []
    for c in out.get("comments") or []:
        cc = dict(c)
        cc.pop("id", None)
        comments.append(cc)
    out["comments"] = comments
    return out


class PortabilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.src_path = self.root / "src.db"
        self.dst_path = self.root / "dst.db"
        self.src = SQLiteTracker(db_path=self.src_path, product_default="")
        self.dst = SQLiteTracker(db_path=self.dst_path, product_default="")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _seed_rich(self) -> None:
        # Alpha is a live backlog ticket with a proper worker seat + product
        # label so it round-trips cleanly (routing/product stamps not applied
        # when both are already present — wl-338 / wl-364).
        t1 = self.src.create_task(
            title="Alpha",
            description="First ticket",
            status="backlog",
            priority=2,
            labels=["area:data", "worker:grok", "product:fixture"],
            ext_id="EXT-alpha",
        )
        self.src.add_comment(str(t1.id), "hello from seed", author="grok")
        self.src.add_comment(str(t1.id), "second note", author="work-pool")
        self.src.create_task(
            title="Beta",
            description="",
            status="done",
            priority=3,
            labels=["product:demo"],
            ext_id=None,
        )

    # ── empty product ────────────────────────────────────────────────

    def test_empty_product_export_yields_no_lines(self) -> None:
        # Touch the schema so the empty store exists.
        self.src.list_tasks()
        lines = list(portability.export_product("fixture", tracker=self.src))
        self.assertEqual(lines, [])

    # ── round-trip ───────────────────────────────────────────────────

    def test_round_trip_content_identical_modulo_ids(self) -> None:
        self._seed_rich()
        lines_src = list(portability.export_product("fixture", tracker=self.src))
        self.assertEqual(len(lines_src), 2)

        report = portability.import_jsonl(lines_src, "fixture", tracker=self.dst)
        self.assertEqual(len(report.created), 2)
        self.assertEqual(report.collisions, [])
        # Mapping covers every source id.
        src_ids = {json.loads(l)["id"] for l in lines_src}
        mapped_old = {o for o, _ in report.created}
        self.assertEqual(src_ids, mapped_old)

        lines_dst = list(portability.export_product("fixture", tracker=self.dst))
        self.assertEqual(len(lines_dst), 2)

        src_bodies = [_strip_ids(json.loads(l)) for l in lines_src]
        dst_bodies = [_strip_ids(json.loads(l)) for l in lines_dst]
        self.assertEqual(src_bodies, dst_bodies)

        # New ids differ from source (autoincrement in a fresh store starts
        # at 1 again — may match by coincidence on a fresh empty dst; force
        # a third import target that already has a row so ids shift).
        occupied = SQLiteTracker(db_path=self.root / "occ.db", product_default="")
        occupied.create_task(title="padding")
        report2 = portability.import_jsonl(lines_src, "fixture", tracker=occupied)
        for old_id, new_id in report2.created:
            # padding took id 1; imports should be 2, 3, ...
            self.assertNotEqual(old_id, new_id)

    def test_export_stable_field_order(self) -> None:
        self._seed_rich()
        line = next(portability.export_product("fixture", tracker=self.src))
        # Keys appear in declared order; first key is "id".
        self.assertTrue(line.startswith('{"id":'))
        obj = json.loads(line)
        self.assertEqual(list(obj.keys()), list(portability._TASK_KEYS))
        self.assertEqual(
            list(obj["comments"][0].keys()), list(portability._COMMENT_KEYS)
        )

    # ── collisions ───────────────────────────────────────────────────

    def test_ext_id_collision_skipped_and_reported(self) -> None:
        self._seed_rich()
        lines = list(portability.export_product("fixture", tracker=self.src))

        first = portability.import_jsonl(lines, "fixture", tracker=self.dst)
        self.assertEqual(len(first.created), 2)

        second = portability.import_jsonl(lines, "fixture", tracker=self.dst)
        # Alpha has ext_id EXT-alpha → collision; Beta has null ext_id → creates again.
        self.assertEqual(second.collisions, ["1"])  # Alpha was id 1 in export
        self.assertEqual(len(second.created), 1)
        self.assertEqual(second.created[0][0], "2")  # Beta

        # Destination ends with 3 rows: Alpha once, Beta twice.
        self.assertEqual(len(self.dst.list_tasks()), 3)

    # ── malformed lines ──────────────────────────────────────────────

    def test_malformed_json_rejected(self) -> None:
        with self.assertRaises(portability.PortabilityError) as ctx:
            portability.import_jsonl(["not-json"], "fixture", tracker=self.dst)
        self.assertIn("malformed JSON", str(ctx.exception))

    def test_missing_title_rejected(self) -> None:
        bad = json.dumps({"id": "9", "description": "no title"})
        with self.assertRaises(portability.PortabilityError) as ctx:
            portability.import_jsonl([bad], "fixture", tracker=self.dst)
        self.assertIn("title", str(ctx.exception).lower())

    def test_empty_line_skipped_between_valid(self) -> None:
        self._seed_rich()
        lines = list(portability.export_product("fixture", tracker=self.src))
        padded: List[str] = [lines[0], "", "  ", lines[1]]
        report = portability.import_jsonl(padded, "fixture", tracker=self.dst)
        self.assertEqual(len(report.created), 2)

    # ── import never updates existing rows ───────────────────────────

    def test_import_does_not_mutate_existing_task(self) -> None:
        original = self.dst.create_task(
            title="Keep me",
            description="original",
            ext_id="KEEP-1",
        )
        line = json.dumps(
            {
                "id": "99",
                "ext_id": "KEEP-1",
                "title": "Overwrite attempt",
                "description": "should not land",
                "status": "done",
                "priority": 1,
                "labels": [],
                "created_at": "2020-01-01T00:00:00+00:00",
                "updated_at": "2020-01-01T00:00:00+00:00",
                "comments": [],
            },
            separators=(",", ":"),
        )
        report = portability.import_jsonl([line], "fixture", tracker=self.dst)
        self.assertEqual(report.collisions, ["99"])
        self.assertEqual(report.created, [])
        still = self.dst.get_task(str(original.id))
        assert still is not None
        self.assertEqual(still.title, "Keep me")
        self.assertEqual(still.description, "original")

    # ── CLI wiring smoke ─────────────────────────────────────────────

    def test_cli_export_parser_and_stdout(self) -> None:
        from worklane.cli import wl as wl_cli

        self._seed_rich()
        parser = wl_cli._build_parser()
        args = parser.parse_args(["export", "--product", "fixture", "--out", str(self.root / "out.jsonl")])
        self.assertEqual(args.command, "export")

        # Drive portability via the same helpers the CLI uses.
        count = portability.export_to_path(
            "fixture", self.root / "out.jsonl", tracker=self.src
        )
        self.assertEqual(count, 2)
        text = (self.root / "out.jsonl").read_text(encoding="utf-8")
        self.assertEqual(len([ln for ln in text.splitlines() if ln.strip()]), 2)


class ImportRoutingTest(unittest.TestCase):
    """Routing law on import paths (wl-338).

    Live tickets without a worker:* seat must arrive with needs:routing.
    Done/canceled tickets are exempt. Already-routed tickets are untouched.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dst = SQLiteTracker(
            db_path=Path(self._tmp.name) / "dst.db", product_default=""
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _import_one(self, obj: Dict[str, Any]) -> Dict[str, Any]:
        line = json.dumps(obj, separators=(",", ":"))
        portability.import_jsonl([line], "fixture", tracker=self.dst)
        tasks = self.dst.list_tasks()
        self.assertEqual(len(tasks), 1)
        return tasks[0].__dict__

    def test_live_ticket_without_seat_gets_needs_routing(self) -> None:
        task = self._import_one(
            {
                "id": "1",
                "ext_id": None,
                "title": "Unrouted",
                "description": "live, no seat",
                "status": "backlog",
                "priority": 3,
                "labels": ["area:backend"],
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "comments": [],
            }
        )
        self.assertIn("needs:routing", task["labels"])

    def test_live_ticket_already_routed_is_unchanged(self) -> None:
        task = self._import_one(
            {
                "id": "1",
                "ext_id": None,
                "title": "Routed",
                "description": "live, has seat",
                "status": "backlog",
                "priority": 3,
                "labels": ["worker:grok", "area:backend"],
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "comments": [],
            }
        )
        self.assertNotIn("needs:routing", task["labels"])
        self.assertIn("worker:grok", task["labels"])

    def test_done_ticket_without_seat_is_not_stamped(self) -> None:
        task = self._import_one(
            {
                "id": "1",
                "ext_id": None,
                "title": "Closed",
                "description": "done, no seat",
                "status": "done",
                "priority": 3,
                "labels": ["product:demo"],
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "comments": [],
            }
        )
        self.assertNotIn("needs:routing", task["labels"])

    def test_canceled_ticket_without_seat_is_not_stamped(self) -> None:
        task = self._import_one(
            {
                "id": "1",
                "ext_id": None,
                "title": "Canceled",
                "description": "canceled, no seat",
                "status": "canceled",
                "priority": 3,
                "labels": [],
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "comments": [],
            }
        )
        self.assertNotIn("needs:routing", task["labels"])

    def test_in_progress_ticket_without_seat_gets_stamped(self) -> None:
        task = self._import_one(
            {
                "id": "1",
                "ext_id": None,
                "title": "InFlight",
                "description": "in_progress, no seat",
                "status": "in_progress",
                "priority": 3,
                "labels": ["area:api"],
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "comments": [],
            }
        )
        self.assertIn("needs:routing", task["labels"])

    def test_needs_routing_not_doubled_when_already_present(self) -> None:
        task = self._import_one(
            {
                "id": "1",
                "ext_id": None,
                "title": "AlreadyStamped",
                "description": "already has needs:routing",
                "status": "backlog",
                "priority": 3,
                "labels": ["needs:routing"],
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "comments": [],
            }
        )
        self.assertEqual(task["labels"].count("needs:routing"), 1)


class ImportStrictRoutingTest(unittest.TestCase):
    """Hard-B import default (wl-367 opt-in → wl-417 always-on for live).

    Default hard_when_hands=True with a non-empty hired_hands list rejects bare
    seats; worker:lili is accepted. Soft override is archival-restore only.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dst = SQLiteTracker(
            db_path=Path(self._tmp.name) / "dst.db", product_default=""
        )
        self._hired = ["worker:lili"]

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _line(self, labels: List[str], *, task_id: str = "1") -> str:
        return json.dumps(
            {
                "id": task_id,
                "ext_id": None,
                "title": "StrictImport",
                "description": "wl-417 hard-B import routing case",
                "status": "backlog",
                "priority": 3,
                "labels": labels,
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "comments": [],
            },
            separators=(",", ":"),
        )

    def test_default_hard_under_hired_hands_rejects_bare(self) -> None:
        """Live default: hard_when_hands=True rejects bare seats when hired."""
        with self.assertRaises(portability.PortabilityError) as ctx:
            portability.import_jsonl(
                [self._line(["area:backend"])],
                "fixture",
                tracker=self.dst,
                hired_hands=self._hired,
            )
        msg = str(ctx.exception)
        self.assertIn("worker:* required", msg)
        self.assertIn("worker:lili", msg)
        self.assertEqual(self.dst.list_tasks(), [])

    def test_soft_override_stamps_needs_routing(self) -> None:
        """Archival soft path never rejects even when hands exist."""
        report = portability.import_jsonl(
            [self._line(["area:backend"])],
            "fixture",
            tracker=self.dst,
            hard_when_hands=False,
            hired_hands=self._hired,
        )
        self.assertEqual(len(report.created), 1)
        labs = list(self.dst.list_tasks()[0].labels or [])
        self.assertIn("needs:routing", labs)
        self.assertNotIn("worker:lili", labs)

    def test_strict_bare_under_hired_hands_rejects(self) -> None:
        with self.assertRaises(portability.PortabilityError) as ctx:
            portability.import_jsonl(
                [self._line(["area:backend"])],
                "fixture",
                tracker=self.dst,
                hard_when_hands=True,
                hired_hands=self._hired,
            )
        msg = str(ctx.exception)
        self.assertIn("worker:* required", msg)
        self.assertIn("worker:lili", msg)
        self.assertEqual(self.dst.list_tasks(), [])

    def test_strict_worker_lili_accepted(self) -> None:
        report = portability.import_jsonl(
            [self._line(["worker:lili", "area:backend"])],
            "fixture",
            tracker=self.dst,
            hard_when_hands=True,
            hired_hands=self._hired,
        )
        self.assertEqual(len(report.created), 1)
        labs = list(self.dst.list_tasks()[0].labels or [])
        self.assertIn("worker:lili", labs)
        self.assertNotIn("needs:routing", labs)

    def test_strict_without_hired_hands_still_soft(self) -> None:
        """Hard flag alone with empty roster is pre-hire soft stamp."""
        report = portability.import_jsonl(
            [self._line(["area:backend"])],
            "fixture",
            tracker=self.dst,
            hard_when_hands=True,
            hired_hands=[],
        )
        self.assertEqual(len(report.created), 1)
        labs = list(self.dst.list_tasks()[0].labels or [])
        self.assertIn("needs:routing", labs)

    def test_cli_routing_flags_wired(self) -> None:
        from worklane.cli import wl as wl_cli

        parser = wl_cli._build_parser()
        soft = parser.parse_args(
            ["import", "x.jsonl", "--project", "fixture", "--soft-routing"]
        )
        self.assertTrue(soft.soft_routing)
        default = parser.parse_args(["import", "x.jsonl", "--project", "fixture"])
        self.assertFalse(default.soft_routing)
        strict = parser.parse_args(
            ["import", "x.jsonl", "--project", "fixture", "--strict-routing"]
        )
        self.assertTrue(strict.strict_routing)


class ImportProductStampTest(unittest.TestCase):
    """product:<dest> attribution on import (wl-364).

    Live tickets missing product:* get product:<destination-slug>.
    Existing product:* is left untouched. Done/canceled are exempt.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dst = SQLiteTracker(
            db_path=Path(self._tmp.name) / "dst.db", product_default=""
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _import_one(
        self, obj: Dict[str, Any], product: str = "fixture"
    ) -> Dict[str, Any]:
        line = json.dumps(obj, separators=(",", ":"))
        portability.import_jsonl([line], product, tracker=self.dst)
        tasks = self.dst.list_tasks()
        self.assertEqual(len(tasks), 1)
        return tasks[0].__dict__

    def test_live_missing_product_gets_dest_stamp(self) -> None:
        task = self._import_one(
            {
                "id": "1",
                "ext_id": None,
                "title": "NoProduct",
                "description": "live, no product label",
                "status": "backlog",
                "priority": 3,
                "labels": ["area:backend"],
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "comments": [],
            },
            product="worklane",
        )
        self.assertIn("product:worklane", task["labels"])
        self.assertEqual(
            sum(1 for l in task["labels"] if str(l).startswith("product:")), 1
        )
        # Seat law still applies alongside product stamp.
        self.assertIn("needs:routing", task["labels"])

    def test_existing_product_label_preserved(self) -> None:
        task = self._import_one(
            {
                "id": "1",
                "ext_id": None,
                "title": "HasProduct",
                "description": "export product is authoritative",
                "status": "backlog",
                "priority": 3,
                "labels": ["product:othercity", "worker:lili"],
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "comments": [],
            },
            product="worklane",
        )
        self.assertIn("product:othercity", task["labels"])
        self.assertNotIn("product:worklane", task["labels"])
        self.assertEqual(task["labels"].count("product:othercity"), 1)

    def test_product_stamp_not_doubled_when_already_present(self) -> None:
        task = self._import_one(
            {
                "id": "1",
                "ext_id": None,
                "title": "AlreadyProduct",
                "description": "already product:fixture",
                "status": "backlog",
                "priority": 3,
                "labels": ["product:fixture", "worker:lili"],
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "comments": [],
            },
            product="fixture",
        )
        self.assertEqual(task["labels"].count("product:fixture"), 1)

    def test_done_ticket_without_product_is_not_stamped(self) -> None:
        task = self._import_one(
            {
                "id": "1",
                "ext_id": None,
                "title": "DoneNoProduct",
                "description": "done, no product",
                "status": "done",
                "priority": 3,
                "labels": ["area:legacy"],
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "comments": [],
            },
            product="worklane",
        )
        self.assertFalse(
            any(str(l).startswith("product:") for l in task["labels"])
        )

    def test_canceled_ticket_without_product_is_not_stamped(self) -> None:
        task = self._import_one(
            {
                "id": "1",
                "ext_id": None,
                "title": "CanceledNoProduct",
                "description": "canceled, no product",
                "status": "canceled",
                "priority": 3,
                "labels": [],
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "comments": [],
            },
            product="worklane",
        )
        self.assertFalse(
            any(str(l).startswith("product:") for l in task["labels"])
        )


if __name__ == "__main__":
    unittest.main()
