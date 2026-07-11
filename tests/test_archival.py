"""wl-23: done-ticket archival engine.

Fixture SQLite DBs only — never the live product stores / tasks.db.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from worklane import archival
from worklane import relations as relmod
from worklane.trackers.protocol import TaskStatus
from worklane.trackers.sqlite import SQLiteTracker


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


class ArchivalTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wl_archival_")
        self.db = Path(self._tmp.name) / "fixture.db"
        self.archive = archival.archive_db_path_for(self.db)
        self.tracker = SQLiteTracker(db_path=self.db, product_default="")
        self.now = datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # ── helpers ──────────────────────────────────────────────────────
    def _make(self, *, title, status, age_days, ext_id=None):
        t = self.tracker.create_task(title=title, description="x", ext_id=ext_id)
        self.tracker.update_status(t.id, status)
        self._set_updated(t.id, self.now - timedelta(days=age_days))
        return t

    def _set_updated(self, task_id: str, dt: datetime) -> None:
        conn = sqlite3.connect(str(self.db))
        try:
            conn.execute(
                "UPDATE tasks SET updated_at = ? WHERE id = ?", (_iso(dt), int(task_id))
            )
            conn.commit()
        finally:
            conn.close()

    def _hot_ids(self):
        return {t.id for t in self.tracker.list_tasks()}

    def _archive_ids(self):
        return {t.id for t in SQLiteTracker(db_path=self.archive, product_default="").list_tasks()}

    # ── tests ────────────────────────────────────────────────────────
    def test_archives_only_cold_terminal(self) -> None:
        old_done = self._make(title="old done", status=TaskStatus.DONE, age_days=200)
        old_cancel = self._make(title="old cancel", status=TaskStatus.CANCELED, age_days=120)
        recent_done = self._make(title="recent done", status=TaskStatus.DONE, age_days=5)
        old_backlog = self._make(title="old backlog", status=TaskStatus.BACKLOG, age_days=200)
        old_wip = self._make(title="old wip", status=TaskStatus.IN_PROGRESS, age_days=200)

        res = archival.archive_cold_tickets(self.db, older_than_days=90, now=self.now)

        self.assertEqual(res.tickets, 2)
        # Only the two cold terminal tickets moved.
        self.assertEqual(self._archive_ids(), {old_done.id, old_cancel.id})
        # Everything else stays hot: recent terminal + live work of any age.
        self.assertEqual(
            self._hot_ids(), {recent_done.id, old_backlog.id, old_wip.id}
        )

    def test_comments_and_relations_follow(self) -> None:
        a = self._make(title="A", status=TaskStatus.DONE, age_days=200)
        b = self._make(title="B", status=TaskStatus.DONE, age_days=200)
        self.tracker.add_comment(a.id, "first", author="work-pool")
        self.tracker.add_comment(a.id, "second", author="founder")
        relmod.create_relation(self.db, a.id, b.id, "blocks")
        # A comment is activity that bumps updated_at — backdate again so the
        # ticket is genuinely cold at archive time.
        self._set_updated(a.id, self.now - timedelta(days=200))

        res = archival.archive_cold_tickets(self.db, older_than_days=90, now=self.now)

        self.assertEqual(res.tickets, 2)
        self.assertEqual(res.comments, 2)
        self.assertEqual(res.relations, 1)
        # Comments followed the ticket into the archive; hot has none left.
        arc_comments = SQLiteTracker(
            db_path=self.archive, product_default=""
        ).list_comments(a.id)
        self.assertEqual([c.body for c in arc_comments], ["first", "second"])
        self.assertEqual(self._hot_ids(), set())
        # Relation preserved in archive, gone from hot.
        self.assertEqual(len(relmod.list_relations(self.archive)), 1)
        self.assertEqual(relmod.list_relations(self.db), [])

    def test_relation_to_live_counterpart_preserved(self) -> None:
        cold = self._make(title="cold done", status=TaskStatus.DONE, age_days=200)
        live = self._make(title="live backlog", status=TaskStatus.BACKLOG, age_days=200)
        relmod.create_relation(self.db, live.id, cold.id, "blocks")

        res = archival.archive_cold_tickets(self.db, older_than_days=90, now=self.now)

        self.assertEqual(res.tickets, 1)
        self.assertEqual(res.relations, 1)
        # The cross edge survives in the cold store even though its live
        # counterpart is not archived (archive tolerates dangling refs by design).
        self.assertEqual(len(relmod.list_relations(self.archive)), 1)
        # Live ticket stays hot; the now-cold edge is removed from the hot DB.
        self.assertEqual(self._hot_ids(), {live.id})
        self.assertEqual(relmod.list_relations(self.db), [])

    def test_idempotent_rerun(self) -> None:
        self._make(title="old done", status=TaskStatus.DONE, age_days=200)
        first = archival.archive_cold_tickets(self.db, older_than_days=90, now=self.now)
        second = archival.archive_cold_tickets(self.db, older_than_days=90, now=self.now)
        self.assertEqual(first.tickets, 1)
        self.assertEqual(second.tickets, 0)
        # No duplicate rows in the archive after a re-run.
        self.assertEqual(archival.archive_counts(self.archive), 1)

    def test_round_trip_restore_is_reversible(self) -> None:
        t = self._make(
            title="round trip", status=TaskStatus.DONE, age_days=200, ext_id="997"
        )
        self.tracker.add_comment(t.id, "note", author="work-pool")
        self._set_updated(t.id, self.now - timedelta(days=200))  # comment bumped updated_at

        archival.archive_cold_tickets(self.db, older_than_days=90, now=self.now)
        self.assertEqual(self._hot_ids(), set())

        # Restore keys on internal id (not ext_id) so NULL-ext_id tickets work.
        res = archival.restore_archived_tickets(self.db, [t.id])
        self.assertEqual(res.tickets, 1)
        self.assertEqual(res.comments, 1)
        # Ticket is back in the hot store with the same internal id + comment;
        # archival did not destroy anything.
        restored = self.tracker.get_task(t.id)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.id, t.id)
        self.assertEqual(restored.title, "round trip")
        self.assertEqual(
            [c.body for c in self.tracker.list_comments(t.id)], ["note"]
        )
        self.assertEqual(archival.archive_counts(self.archive), 0)

    def test_restore_by_internal_id_with_null_ext_id(self) -> None:
        """Real tickets can have NULL ext_id — restore must still find them."""
        t = self._make(title="null ext", status=TaskStatus.DONE, age_days=200, ext_id=None)
        archival.archive_cold_tickets(self.db, older_than_days=90, now=self.now)
        self.assertEqual(self._hot_ids(), set())
        self.assertEqual(self._archive_ids(), {t.id})

        res = archival.restore_archived_tickets(self.db, [t.id])
        self.assertEqual(res.tickets, 1)
        restored = self.tracker.get_task(t.id)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.title, "null ext")
        self.assertEqual(archival.archive_counts(self.archive), 0)

    def test_mixed_timestamp_formats(self) -> None:
        # App writes '+00:00', the SQL column DEFAULT writes 'Z'. Both must be
        # recognised as old; lexicographic compare would misorder them.
        z = self._make(title="z fmt", status=TaskStatus.DONE, age_days=200)
        offset = self._make(title="offset fmt", status=TaskStatus.DONE, age_days=200)
        conn = sqlite3.connect(str(self.db))
        try:
            conn.execute(
                "UPDATE tasks SET updated_at = ? WHERE id = ?",
                ("2026-01-01T00:00:00.000Z", int(z.id)),
            )
            conn.execute(
                "UPDATE tasks SET updated_at = ? WHERE id = ?",
                ("2026-01-01T00:00:00.000000+00:00", int(offset.id)),
            )
            conn.commit()
        finally:
            conn.close()

        res = archival.archive_cold_tickets(self.db, older_than_days=90, now=self.now)
        self.assertEqual(res.tickets, 2)
        self.assertEqual(self._archive_ids(), {z.id, offset.id})

    def test_empty_and_missing_db_are_safe(self) -> None:
        res = archival.archive_cold_tickets(self.db, older_than_days=90, now=self.now)
        self.assertEqual((res.tickets, res.comments, res.relations), (0, 0, 0))
        self.assertEqual(archival.archive_counts(self.archive), 0)

        missing = Path(self._tmp.name) / "nope.db"
        res2 = archival.archive_cold_tickets(missing, older_than_days=90, now=self.now)
        self.assertEqual(res2.tickets, 0)

    def test_archive_db_path_naming(self) -> None:
        self.assertEqual(
            archival.archive_db_path_for(Path("/x/tradeos.db")).name,
            "tradeos_archive.db",
        )


if __name__ == "__main__":
    unittest.main()
