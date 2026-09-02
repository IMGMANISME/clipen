from pathlib import Path
import tempfile
import unittest

from clipen.analysis import (
    ArtifactKind,
    ArtifactStore,
    PreparationItemStatus,
    PreparationRepository,
    PreparationState,
)
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


class PreparationStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.clip = self.source / "clip.mov"
        self.clip.write_bytes(b"source")
        self.silent = self.source / "silent.mov"
        self.silent.write_bytes(b"silent")
        self.projects = ProjectRepository(self.root / "projects")
        created = self.projects.create_project("request-analysis-project", "分析測試")
        identity = self.source.stat()
        self.project = self.projects.set_directory(
            created.project_id,
            "request-analysis-directory",
            "a" * 64,
            created.revision,
            DirectoryReference(
                DirectoryRole.SOURCE,
                str(self.source.resolve()),
                str(self.source),
                "source",
                identity.st_dev,
                identity.st_ino,
                "2026-09-02T00:00:00+00:00",
            ),
        )
        inventories = InventoryRepository(self.projects)
        inventory = inventories.create_run(
            self.project.project_id,
            "request-analysis-inventory",
            self.project.revision,
        )
        inventory = inventories.transition(
            self.project.project_id,
            inventory.run_id,
            InventoryState.RUNNING,
            phase="inspecting",
        )
        clip_stat = self.clip.stat()
        silent_stat = self.silent.stat()
        inventories.stage_items(
            self.project.project_id,
            inventory.run_id,
            (
                AssetCandidate(
                    DirectoryRole.SOURCE,
                    "clip.mov",
                    AssetKind.VIDEO,
                    ItemStatus.USABLE,
                    None,
                    clip_stat.st_size,
                    clip_stat.st_mtime_ns,
                    "b" * 64,
                    ProbeStatus.READY,
                    {"duration_seconds": "5"},
                    (
                        MediaStream(
                            0,
                            "video",
                            "h264",
                            {
                                "time_base": "1/90000",
                                "start_pts": "0",
                                "duration_ts": "450000",
                                "width": 1920,
                                "height": 1080,
                            },
                        ),
                        MediaStream(
                            1,
                            "audio",
                            "aac",
                            {
                                "time_base": "1/48000",
                                "start_pts": "48000",
                                "duration_ts": "240000",
                                "sample_rate": 48000,
                                "channels": 2,
                            },
                        ),
                    ),
                ),
                AssetCandidate(
                    DirectoryRole.SOURCE,
                    "silent.mov",
                    AssetKind.VIDEO,
                    ItemStatus.USABLE,
                    None,
                    silent_stat.st_size,
                    silent_stat.st_mtime_ns,
                    "c" * 64,
                    ProbeStatus.READY,
                    {"duration_seconds": "2"},
                    (
                        MediaStream(
                            0,
                            "video",
                            "h264",
                            {
                                "time_base": "1/90000",
                                "start_pts": "0",
                                "duration_ts": "180000",
                                "width": 1280,
                                "height": 720,
                            },
                        ),
                    ),
                ),
            ),
        )
        self.inventory = inventories.publish(self.project.project_id, inventory.run_id)
        self.repository = PreparationRepository(self.projects)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_preflight_is_bound_to_inventory_without_exposing_paths(self) -> None:
        scope = self.repository.preflight(self.project.project_id)

        self.assertEqual(scope.inventory_run_id, self.inventory.run_id)
        self.assertEqual(scope.duration_seconds, "5")
        self.assertEqual(len(scope.targets), 2)
        audio = next(target for target in scope.targets if target.audio is not None)
        no_audio = next(
            target
            for target in scope.targets
            if target.initial_status is PreparationItemStatus.NO_AUDIO
        )
        self.assertEqual(audio.audio.source_span.start_pts, "48000")
        self.assertEqual(audio.audio.source_span.end_pts, "288000")
        self.assertEqual(no_audio.stream_index, -1)
        public = scope.to_public()
        self.assertEqual(public["counts"], {"audio_streams": 1, "no_audio": 1, "issues": 0})
        self.assertNotIn(str(self.source), repr(public))
        self.assertNotIn("relative_path", repr(public))

    def test_create_run_is_durable_idempotent_and_private(self) -> None:
        scope = self.repository.preflight(self.project.project_id)
        created, was_created = self.repository.create_run_with_status(
            self.project.project_id,
            "request-analysis-start",
            self.project.revision,
            scope.scope_id,
            recipe={"id": "audio.pcm16.v1", "chunk_seconds": 300},
            model={"id": "mlx-whisper-small", "revision": "test"},
        )
        replay, replay_created = PreparationRepository(
            ProjectRepository(self.root / "projects")
        ).create_run_with_status(
            self.project.project_id,
            "request-analysis-start",
            self.project.revision,
            scope.scope_id,
            recipe={"id": "audio.pcm16.v1", "chunk_seconds": 300},
            model={"id": "mlx-whisper-small", "revision": "test"},
        )

        self.assertTrue(was_created)
        self.assertFalse(replay_created)
        self.assertEqual(replay, created)
        self.assertEqual(created.state, PreparationState.QUEUED)
        self.assertEqual(created.counts.total, 2)
        self.assertEqual(created.counts.no_audio, 1)
        self.assertEqual(self.repository.latest_run(self.project.project_id), created)
        public = created.to_public()
        self.assertNotIn("operation_id", public)
        self.assertNotIn("attempt_id", public)
        self.assertNotIn("input_fingerprint", public)
        self.assertNotIn(str(self.source), repr(public))
        with self.projects.connect(self.project.project_id) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM operation_events WHERE operation_id = ?",
                    (created.operation_id,),
                ).fetchone()[0],
                1,
            )

    def test_scope_change_and_request_reuse_fail_closed(self) -> None:
        scope = self.repository.preflight(self.project.project_id)
        self.repository.create_run_with_status(
            self.project.project_id,
            "request-analysis-conflict",
            self.project.revision,
            scope.scope_id,
            recipe={"id": "audio.pcm16.v1"},
            model={"id": "small"},
        )
        with self.assertRaises(ProjectError) as reused:
            self.repository.create_run_with_status(
                self.project.project_id,
                "request-analysis-conflict",
                self.project.revision,
                scope.scope_id,
                recipe={"id": "audio.pcm16.v2"},
                model={"id": "small"},
            )
        self.assertEqual(reused.exception.code, "request_reused")

        updated = self.projects.update_details(
            self.project.project_id,
            "request-analysis-edit",
            self.project.revision,
            self.project.name,
            "新的需求",
        )
        with self.assertRaises(ProjectError) as changed:
            self.repository.create_run_with_status(
                self.project.project_id,
                "request-analysis-stale",
                updated.revision,
                scope.scope_id,
                recipe={"id": "audio.pcm16.v1"},
                model={"id": "small"},
            )
        self.assertEqual(changed.exception.code, "scope_changed")

        replay, created = self.repository.create_run_with_status(
            self.project.project_id,
            "request-analysis-conflict",
            self.project.revision,
            scope.scope_id,
            recipe={"id": "audio.pcm16.v1"},
            model={"id": "small"},
        )
        self.assertFalse(created)
        self.assertEqual(replay.project_revision, self.project.revision)

    def test_item_progress_and_publish_are_transactional(self) -> None:
        scope = self.repository.preflight(self.project.project_id)
        run, _ = self.repository.create_run_with_status(
            self.project.project_id,
            "request-analysis-progress",
            self.project.revision,
            scope.scope_id,
            recipe={"id": "audio.pcm16.v1"},
            model={"id": "small"},
        )
        running = self.repository.transition(
            self.project.project_id,
            run.run_id,
            PreparationState.RUNNING,
            phase="extracting_audio",
        )
        target = next(item for item in scope.targets if item.audio is not None)
        store = ArtifactStore(self.projects)
        staging = store.create_staging_file(self.project.project_id, suffix=".wav")
        staging.write_bytes(b"normalized")
        artifact = store.publish(
            self.project.project_id,
            staging,
            kind=ArtifactKind.ANALYSIS_AUDIO,
            producer_operation_id=running.operation_id,
            producer_attempt_id=running.attempt_id,
            recipe_id="audio.pcm16.v1",
            lineage={"version_id": target.version_id},
            source_span=target.audio.source_span,
        )
        prepared = self.repository.settle_item(
            self.project.project_id,
            run.run_id,
            target.version_id,
            target.stream_index,
            PreparationItemStatus.PREPARED,
            audio_artifact_id=artifact.artifact_id,
            checkpoint={"audio_ready": True},
        )
        self.assertEqual(prepared.counts.prepared, 1)
        with self.assertRaises(ProjectError) as incomplete:
            self.repository.publish(self.project.project_id, run.run_id)
        self.assertEqual(incomplete.exception.code, "preparation_incomplete")

        settled = self.repository.settle_item(
            self.project.project_id,
            run.run_id,
            target.version_id,
            target.stream_index,
            PreparationItemStatus.FAILED,
            error_code="asr_failed",
        )
        self.assertEqual(settled.counts.prepared, 0)
        self.assertEqual(settled.counts.issues, 1)
        published = self.repository.publish(self.project.project_id, run.run_id)
        self.assertEqual(published.state, PreparationState.SUCCEEDED)
        self.assertEqual(self.repository.current_run(self.project.project_id), published)

    def test_cancel_and_restart_recovery_are_idempotent(self) -> None:
        scope = self.repository.preflight(self.project.project_id)
        queued, _ = self.repository.create_run_with_status(
            self.project.project_id,
            "request-analysis-cancel",
            self.project.revision,
            scope.scope_id,
            recipe={"id": "audio.pcm16.v1"},
            model={"id": "small"},
        )
        cancelled = self.repository.request_cancel(self.project.project_id, queued.run_id)
        self.assertEqual(cancelled.state, PreparationState.CANCELLED)
        self.assertEqual(
            self.repository.request_cancel(self.project.project_id, queued.run_id),
            cancelled,
        )

        active, _ = self.repository.create_run_with_status(
            self.project.project_id,
            "request-analysis-interrupt",
            self.project.revision,
            scope.scope_id,
            recipe={"id": "audio.pcm16.v1"},
            model={"id": "small"},
        )
        self.repository.transition(
            self.project.project_id,
            active.run_id,
            PreparationState.RUNNING,
            phase="extracting_audio",
        )
        self.assertEqual(self.repository.interrupt_incomplete(self.project.project_id), 1)
        interrupted = self.repository.get_run(self.project.project_id, active.run_id)
        self.assertEqual(interrupted.state, PreparationState.INTERRUPTED)
        self.assertEqual(self.repository.interrupt_incomplete(self.project.project_id), 0)

    def test_preflight_requires_a_successful_inventory(self) -> None:
        other = self.projects.create_project("request-analysis-empty", "空作品")
        with self.assertRaises(ProjectError) as context:
            self.repository.preflight(other.project_id)
        self.assertEqual(context.exception.code, "inventory_required")


if __name__ == "__main__":
    unittest.main()
