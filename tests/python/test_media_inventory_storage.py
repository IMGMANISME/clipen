from pathlib import Path
import sqlite3
import tempfile
import unittest

from clipen.media import (
    AssetCandidate,
    AssetKind,
    InventoryRepository,
    InventoryState,
    ItemStatus,
    MediaStream,
    ProbeStatus,
)
from clipen.projects import DirectoryReference, DirectoryRole, ProjectError, ProjectRepository


class MediaInventoryStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.projects_root = self.root / "projects"
        self.source = self.root / "source"
        self.source.mkdir()
        self.projects = ProjectRepository(self.projects_root)
        created = self.projects.create_project("request-create-inventory", "素材測試")
        identity = self.source.stat()
        self.project = self.projects.set_directory(
            created.project_id,
            "request-set-inventory-source",
            "a" * 64,
            created.revision,
            DirectoryReference(
                DirectoryRole.SOURCE,
                str(self.source.resolve()),
                str(self.source),
                "source",
                identity.st_dev,
                identity.st_ino,
                "2026-09-01T00:00:00+00:00",
            ),
        )
        self.inventories = InventoryRepository(self.projects)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def start(self, request_id: str = "request-inventory-0001"):
        run = self.inventories.create_run(
            self.project.project_id,
            request_id,
            self.project.revision,
        )
        return self.inventories.transition(
            self.project.project_id,
            run.run_id,
            InventoryState.RUNNING,
            phase="discovering",
        )

    def usable_video(self) -> AssetCandidate:
        return AssetCandidate(
            role=DirectoryRole.SOURCE,
            relative_path="camera/A001.mov",
            kind=AssetKind.VIDEO,
            status=ItemStatus.USABLE,
            issue_code=None,
            size_bytes=1024,
            mtime_ns=123456789,
            fingerprint="b" * 64,
            probe_status=ProbeStatus.READY,
            probe={"duration_seconds": "2.5", "width": 1920, "height": 1080},
            streams=(
                MediaStream(
                    0,
                    "video",
                    "h264",
                    {"time_base": "1/90000", "fps": "30000/1001"},
                ),
                MediaStream(1, "audio", "aac", {"sample_rate": 48000, "channels": 2}),
            ),
        )

    def test_reinspection_adds_cover_flags_without_replacing_time_mapping(self) -> None:
        from dataclasses import replace
        import json
        original = self.usable_video()
        first = self.start()
        self.inventories.stage_items(self.project.project_id, first.run_id, (original,))
        self.inventories.publish(self.project.project_id, first.run_id)
        second = self.start("request-inventory-reinspect")
        enriched = replace(original, streams=(MediaStream(0, "video", "h264", {
            "time_base": "1/1000", "attached_pic": True, "timed_thumbnails": False,
        }), original.streams[1]))
        self.inventories.stage_items(self.project.project_id, second.run_id, (enriched,))
        with self.projects.connect(self.project.project_id) as connection:
            details = json.loads(connection.execute("SELECT details_json FROM media_streams WHERE stream_index=0").fetchone()[0])
        self.assertTrue(details["attached_pic"])
        self.assertEqual(details["time_base"], "1/90000")

    def test_create_run_is_durable_idempotent_and_does_not_change_project_revision(self) -> None:
        created = self.inventories.create_run(
            self.project.project_id,
            "request-inventory-create",
            self.project.revision,
        )
        replay = InventoryRepository(ProjectRepository(self.projects_root)).create_run(
            self.project.project_id,
            "request-inventory-create",
            self.project.revision,
        )

        self.assertEqual(replay, created)
        self.assertEqual(created.state, InventoryState.QUEUED)
        self.assertEqual(self.projects.get_project(self.project.project_id).revision, self.project.revision)
        public = created.to_public()
        self.assertNotIn("roots", public)
        self.assertNotIn("input_fingerprint", public)
        self.assertNotIn(str(self.source), repr(public))

        with self.assertRaises(ProjectError) as stale:
            self.inventories.create_run(
                self.project.project_id,
                "request-inventory-stale",
                self.project.revision - 1,
            )
        self.assertEqual(stale.exception.code, "revision_conflict")

        with self.assertRaises(ProjectError) as reused:
            self.inventories.create_run(
                self.project.project_id,
                "request-inventory-create",
                self.project.revision + 1,
            )
        self.assertEqual(reused.exception.code, "request_reused")

    def test_create_status_distinguishes_a_new_run_from_a_replayed_request(self) -> None:
        created, was_created = self.inventories.create_run_with_status(
            self.project.project_id,
            "request-inventory-status",
            self.project.revision,
        )
        replay, replay_was_created = self.inventories.create_run_with_status(
            self.project.project_id,
            "request-inventory-status",
            self.project.revision,
        )

        self.assertTrue(was_created)
        self.assertFalse(replay_was_created)
        self.assertEqual(replay, created)

    def test_cancel_request_is_idempotent_for_queued_running_and_terminal_runs(self) -> None:
        queued = self.inventories.create_run(
            self.project.project_id,
            "request-inventory-cancel-queued",
            self.project.revision,
        )
        cancelled = self.inventories.request_cancel(self.project.project_id, queued.run_id)
        self.assertEqual(cancelled.state, InventoryState.CANCELLED)
        self.assertEqual(
            self.inventories.request_cancel(self.project.project_id, queued.run_id),
            cancelled,
        )

        running = self.start("request-inventory-cancel-running")
        cancelling = self.inventories.request_cancel(self.project.project_id, running.run_id)
        self.assertEqual(cancelling.state, InventoryState.CANCELLING)
        self.assertEqual(
            self.inventories.request_cancel(self.project.project_id, running.run_id),
            cancelling,
        )
        finished = self.inventories.transition(
            self.project.project_id,
            running.run_id,
            InventoryState.CANCELLED,
            phase="cancelled",
        )
        self.assertEqual(
            self.inventories.request_cancel(self.project.project_id, running.run_id),
            finished,
        )

    def test_latest_run_is_separate_from_last_successful_run(self) -> None:
        first = self.start("request-inventory-latest-first")
        self.inventories.stage_item(
            self.project.project_id, first.run_id, self.usable_video()
        )
        published = self.inventories.publish(self.project.project_id, first.run_id)
        latest = self.inventories.create_run(
            self.project.project_id,
            "request-inventory-latest-second",
            self.project.revision,
        )

        self.assertEqual(self.inventories.current_run(self.project.project_id), published)
        self.assertEqual(self.inventories.latest_run(self.project.project_id), latest)

    def test_successful_publish_is_atomic_and_persists_assets_and_streams(self) -> None:
        running = self.start()
        staged = self.inventories.stage_item(
            self.project.project_id, running.run_id, self.usable_video()
        )
        staged = self.inventories.stage_item(
            self.project.project_id,
            running.run_id,
            AssetCandidate(
                DirectoryRole.SOURCE,
                "camera/notes.txt",
                AssetKind.UNKNOWN,
                ItemStatus.IGNORED,
                "non_media",
                None,
                None,
                None,
                None,
            ),
        )
        self.assertEqual(staged.counts.to_public(), {
            "discovered": 2,
            "probed": 1,
            "usable": 1,
            "issues": 0,
            "ignored": 1,
        })

        published = self.inventories.publish(self.project.project_id, running.run_id)
        self.assertEqual(published.state, InventoryState.SUCCEEDED)
        self.assertEqual(self.inventories.current_run(self.project.project_id), published)

        with self.projects.connect(self.project.project_id) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM assets").fetchone()[0], 2)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM asset_versions").fetchone()[0], 1
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM media_streams").fetchone()[0], 2
            )

        page = self.inventories.list_items(
            self.project.project_id, published.run_id, limit=1
        )
        self.assertEqual(page.total, 2)
        self.assertEqual(page.offset, 0)
        self.assertEqual(page.to_public()["next_offset"], 1)
        first = page.items[0].to_public()
        self.assertEqual(first["status"], "usable")
        self.assertEqual(first["relative_path"], "camera/A001.mov")
        self.assertNotIn("fingerprint", first)
        self.assertNotIn("mtime_ns", first)
        self.assertNotIn(str(self.source), repr(first))

        second_page = self.inventories.list_items(
            self.project.project_id, published.run_id, offset=1, limit=1
        )
        self.assertEqual(second_page.items[0].status, ItemStatus.IGNORED)
        self.assertIsNone(second_page.to_public()["next_offset"])
        empty_page = self.inventories.list_items(
            self.project.project_id, published.run_id, offset=500, limit=10
        )
        self.assertEqual(empty_page.items, ())
        self.assertIsNone(empty_page.to_public()["next_offset"])

        for offset, limit in ((-1, 10), (0, 0), (0, 201)):
            with self.subTest(offset=offset, limit=limit):
                with self.assertRaises(ProjectError) as invalid:
                    self.inventories.list_items(
                        self.project.project_id,
                        published.run_id,
                        offset=offset,
                        limit=limit,
                    )
                self.assertEqual(invalid.exception.code, "pagination_invalid")

    def test_failed_run_does_not_replace_the_last_successful_inventory(self) -> None:
        first = self.start("request-inventory-first")
        self.inventories.stage_item(
            self.project.project_id, first.run_id, self.usable_video()
        )
        published = self.inventories.publish(self.project.project_id, first.run_id)

        second = self.start("request-inventory-second")
        failed = self.inventories.transition(
            self.project.project_id,
            second.run_id,
            InventoryState.FAILED,
            phase="failed",
            error_code="probe_unavailable",
        )

        self.assertEqual(failed.state, InventoryState.FAILED)
        self.assertEqual(self.inventories.current_run(self.project.project_id), published)

    def test_detail_edits_keep_current_inventory_but_directory_edits_invalidate_it(self) -> None:
        running = self.start("request-inventory-current-validity")
        self.inventories.stage_item(
            self.project.project_id, running.run_id, self.usable_video()
        )
        published = self.inventories.publish(self.project.project_id, running.run_id)
        updated = self.projects.update_details(
            self.project.project_id,
            "request-inventory-detail-edit",
            self.project.revision,
            self.project.name,
            "新的剪輯需求",
        )
        self.assertEqual(self.inventories.current_run(self.project.project_id), published)

        reference = self.root / "reference"
        reference.mkdir()
        identity = reference.stat()
        self.projects.set_directory(
            self.project.project_id,
            "request-inventory-reference-edit",
            "e" * 64,
            updated.revision,
            DirectoryReference(
                DirectoryRole.REFERENCE,
                str(reference.resolve()),
                str(reference),
                "reference",
                identity.st_dev,
                identity.st_ino,
                "2026-09-01T00:00:01+00:00",
            ),
        )
        self.assertIsNone(self.inventories.current_run(self.project.project_id))

    def test_restart_marks_incomplete_runs_interrupted_without_replaying(self) -> None:
        running = self.start("request-inventory-restart")
        restarted = InventoryRepository(ProjectRepository(self.projects_root))
        self.assertEqual(restarted.interrupt_incomplete(self.project.project_id), 1)
        interrupted = restarted.get_run(self.project.project_id, running.run_id)
        self.assertEqual(interrupted.state, InventoryState.INTERRUPTED)
        self.assertEqual(interrupted.error_code, "controller_restarted")
        self.assertEqual(restarted.interrupt_incomplete(self.project.project_id), 0)

    def test_only_one_nonterminal_run_can_exist_for_a_project(self) -> None:
        first = self.inventories.create_run(
            self.project.project_id,
            "request-inventory-busy-a",
            self.project.revision,
        )
        with self.assertRaises(ProjectError) as busy:
            self.inventories.create_run(
                self.project.project_id,
                "request-inventory-busy-b",
                self.project.revision,
            )
        self.assertEqual(busy.exception.code, "inventory_busy")

        self.inventories.transition(
            self.project.project_id,
            first.run_id,
            InventoryState.CANCELLED,
            phase="cancelled",
        )
        second = self.inventories.create_run(
            self.project.project_id,
            "request-inventory-busy-b",
            self.project.revision,
        )
        self.assertEqual(second.state, InventoryState.QUEUED)

    def test_item_batches_update_progress_once_and_roll_back_atomically(self) -> None:
        running = self.start("request-inventory-batch")
        ignored = AssetCandidate(
            DirectoryRole.SOURCE,
            "camera/notes.txt",
            AssetKind.UNKNOWN,
            ItemStatus.IGNORED,
            "non_media",
            None,
            None,
            None,
            None,
        )
        staged = self.inventories.stage_items(
            self.project.project_id,
            running.run_id,
            (self.usable_video(), ignored),
        )
        self.assertEqual(staged.counts.discovered, 2)

        with self.assertRaises(ProjectError) as duplicate:
            self.inventories.stage_items(
                self.project.project_id,
                running.run_id,
                (
                    AssetCandidate(
                        DirectoryRole.SOURCE,
                        "camera/new.txt",
                        AssetKind.UNKNOWN,
                        ItemStatus.IGNORED,
                        "non_media",
                        None,
                        None,
                        None,
                        None,
                    ),
                    ignored,
                ),
            )
        self.assertEqual(duplicate.exception.code, "inventory_item_conflict")
        after = self.inventories.get_run(self.project.project_id, running.run_id)
        self.assertEqual(after.counts.discovered, 2)
        with self.projects.connect(self.project.project_id) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM assets WHERE relative_path = 'camera/new.txt'"
                ).fetchone()[0],
                0,
            )

    def test_v1_database_migrates_atomically_without_changing_project(self) -> None:
        before = self.projects.get_project(self.project.project_id)
        database = self.projects_root / self.project.project_id / "project.sqlite3"
        connection = sqlite3.connect(database)
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.executescript(
            """
            DROP TABLE current_preparation;
            DROP TABLE transcript_segments;
            DROP TABLE transcription_revisions;
            DROP TABLE evidence_observations;
                DROP TABLE evidence_results;
                DROP TABLE evidence_images;
                DROP TABLE evidence_cache;
                DROP TABLE evidence_runs;
                DROP TABLE preparation_chunks;
            DROP TABLE artifacts;
            DROP TABLE operation_events;
            DROP TABLE preparation_items;
            DROP TABLE preparation_command_receipts;
            DROP TABLE preparation_runs;
            DROP TABLE current_inventory;
            DROP TABLE media_streams;
            DROP TABLE inventory_items;
            DROP TABLE asset_versions;
            DROP TABLE assets;
            DROP TABLE inventory_command_receipts;
            DROP TABLE inventory_runs;
            PRAGMA user_version = 1;
            """
        )
        connection.close()

        migrated = ProjectRepository(self.projects_root)
        self.assertEqual(migrated.get_project(self.project.project_id), before)
        with migrated.connect(self.project.project_id) as checked:
            self.assertEqual(checked.execute("PRAGMA user_version").fetchone()[0], 7)
            tables = {
                row[0]
                for row in checked.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
        self.assertIn("inventory_runs", tables)
        self.assertIn("current_inventory", tables)
        self.assertIn("preparation_runs", tables)
        self.assertIn("artifacts", tables)

    def test_candidate_rejects_unsafe_relative_paths(self) -> None:
        for path in ("../outside.mov", "/absolute.mov", "folder\\file.mov"):
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    AssetCandidate(
                        DirectoryRole.SOURCE,
                        path,
                        AssetKind.VIDEO,
                        ItemStatus.NEEDS_ATTENTION,
                        "invalid_path",
                        None,
                        None,
                        None,
                        None,
                    )


if __name__ == "__main__":
    unittest.main()
