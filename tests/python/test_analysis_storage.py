from pathlib import Path
import json
import sqlite3
import tempfile
import threading
import time
import unittest
import wave

from clipen.analysis import (
    ArtifactKind,
    ArtifactStore,
    PreparedAudioChunk,
    PreparationCoordinator,
    PreparationItemStatus,
    PreparationRepository,
    PreparationRecipe,
    PreparationState,
    TranscriptSegment,
    TranscriptWord,
    TranscriptionError,
    TranscriptionResult,
    TimelineSegment,
    project_samples_to_source,
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


class FakeAudioAdapter:
    def version(self) -> str:
        return "ffmpeg version test"

    def extract(self, source, output, cancel=None):
        del source, cancel
        with wave.open(str(output), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16_000)
            audio.writeframes(b"\0\0" * 16_000)
        from clipen.analysis import inspect_normalized_audio

        return inspect_normalized_audio(output)


class FakeTranscriber:
    requests: list[tuple[str, str | None]] = []

    def probe(self) -> str:
        return "mlx-whisper test"

    def transcribe(
        self, audio, output, *, language="zh", initial_prompt=None, cancel=None
    ):
        del audio, cancel
        self.requests.append((language, initial_prompt))
        output.write_text('{"test":true}', encoding="utf-8")
        return TranscriptionResult(
            language,
            "手沖咖啡",
            (
                TranscriptSegment(
                    0,
                    16_000,
                    "手沖咖啡",
                    "0.01",
                    (TranscriptWord(0, 16_000, "手沖咖啡", "0.99"),),
                ),
            ),
        )


class FailingTranscriber(FakeTranscriber):
    def transcribe(
        self, audio, output, *, language="zh", initial_prompt=None, cancel=None
    ):
        del audio, output, language, initial_prompt, cancel
        raise TranscriptionError("asr_failed")


class BlockingTranscriber(FakeTranscriber):
    entered = threading.Event()

    def transcribe(
        self, audio, output, *, language="zh", initial_prompt=None, cancel=None
    ):
        del audio, output, language, initial_prompt
        self.entered.set()
        assert cancel is not None
        cancel.wait(2)
        raise TranscriptionError("cancelled")


class PreparationStorageTests(unittest.TestCase):
    def test_default_recipe_requests_traditional_chinese_transcript(self) -> None:
        self.assertIsNone(PreparationRecipe().initial_prompt)
        self.assertIsNone(PreparationRecipe(vocabulary=("藍瓶咖啡",)).initial_prompt)
        self.assertIsNone(PreparationRecipe(language="ja").initial_prompt)

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

    def test_preflight_accepts_volume_remount_but_rejects_replaced_root(self) -> None:
        database = self.root / "projects" / self.project.project_id / "project.sqlite3"
        with sqlite3.connect(database) as connection:
            roots = json.loads(
                connection.execute(
                    "SELECT roots_json FROM inventory_runs WHERE run_id = ?",
                    (self.inventory.run_id,),
                ).fetchone()[0]
            )
            roots[0]["device"] += 1
            connection.execute(
                "UPDATE inventory_runs SET roots_json = ? WHERE run_id = ?",
                (json.dumps(roots), self.inventory.run_id),
            )

        remounted = self.repository.preflight(self.project.project_id)
        self.assertEqual(remounted.inventory_run_id, self.inventory.run_id)

        roots[0]["inode"] += 1
        with sqlite3.connect(database) as connection:
            connection.execute(
                "UPDATE inventory_runs SET roots_json = ? WHERE run_id = ?",
                (json.dumps(roots), self.inventory.run_id),
            )
        with self.assertRaises(ProjectError) as replaced:
            self.repository.preflight(self.project.project_id)
        self.assertEqual(replaced.exception.code, "root_changed")

    def test_create_run_is_durable_idempotent_and_private(self) -> None:
        scope = self.repository.preflight(self.project.project_id)
        created, was_created, retired = self.repository.create_run_with_status(
            self.project.project_id,
            "request-analysis-start",
            self.project.revision,
            scope.scope_id,
            recipe={"id": "audio.pcm16.v1", "chunk_seconds": 300},
            model={"id": "mlx-whisper-small", "revision": "test"},
        )
        replay, replay_created, replay_retired = PreparationRepository(
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
        self.assertEqual(retired, ())
        self.assertEqual(replay_retired, ())
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

        replay, created, retired = self.repository.create_run_with_status(
            self.project.project_id,
            "request-analysis-conflict",
            self.project.revision,
            scope.scope_id,
            recipe={"id": "audio.pcm16.v1"},
            model={"id": "small"},
        )
        self.assertFalse(created)
        self.assertEqual(retired, ())
        self.assertEqual(replay.project_revision, self.project.revision)

    def test_item_progress_and_publish_are_transactional(self) -> None:
        scope = self.repository.preflight(self.project.project_id)
        run, _, _ = self.repository.create_run_with_status(
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
        self.assertEqual(published.state, PreparationState.FAILED)
        self.assertEqual(published.error_code, "asr_failed")
        self.assertIsNone(self.repository.current_run(self.project.project_id))
        events = self.repository.list_events(self.project.project_id, run.run_id)
        self.assertEqual([event["type"] for event in events][0], "queued")
        self.assertEqual([event["type"] for event in events][-1], "failed")
        self.assertEqual(
            self.repository.list_events(
                self.project.project_id,
                run.run_id,
                after_sequence=events[-1]["sequence"],
            ),
            (),
        )
        self.assertNotIn(str(self.source), repr(events))

    def test_starting_new_analysis_atomically_replaces_old_managed_results(self) -> None:
        scope = self.repository.preflight(self.project.project_id)
        old, _, _ = self.repository.create_run_with_status(
            self.project.project_id,
            "request-analysis-old",
            self.project.revision,
            scope.scope_id,
            recipe={"id": "audio.pcm16.v1"},
            model={"id": "small"},
        )
        old = self.repository.transition(
            self.project.project_id,
            old.run_id,
            PreparationState.RUNNING,
            phase="extracting_audio",
        )
        target = next(item for item in scope.targets if item.audio is not None)
        store = ArtifactStore(self.projects)
        staging = store.create_staging_file(self.project.project_id, suffix=".wav")
        staging.write_bytes(b"replaceable analysis audio")
        artifact = store.publish(
            self.project.project_id,
            staging,
            kind=ArtifactKind.ANALYSIS_AUDIO,
            producer_operation_id=old.operation_id,
            producer_attempt_id=old.attempt_id,
            recipe_id="audio.pcm16.v1",
            lineage={"version_id": target.version_id},
            source_span=target.audio.source_span,
        )
        blobs = [store.path(artifact)]
        self.repository.settle_item(
            self.project.project_id,
            old.run_id,
            target.version_id,
            target.stream_index,
            PreparationItemStatus.PREPARED,
            audio_artifact_id=artifact.artifact_id,
        )
        chunk_span = project_samples_to_source(
            target.audio.source_span, 0, 16_000, 16_000
        )
        self.repository.register_chunks(
            self.project.project_id,
            old.run_id,
            target.version_id,
            target.stream_index,
            (PreparedAudioChunk(self.root / "retired.wav", chunk_span, 0, 16_000),),
        )
        result = TranscriptionResult(
            "zh",
            "舊逐字稿",
            (TranscriptSegment(0, 16_000, "舊逐字稿", "0.01", ()),),
        )
        chunk_staging = store.create_staging_file(
            self.project.project_id, suffix=".json"
        )
        chunk_staging.write_text('{"chunk":"old"}', encoding="utf-8")
        chunk_artifact = store.publish(
            self.project.project_id,
            chunk_staging,
            kind=ArtifactKind.TRANSCRIPT,
            producer_operation_id=old.operation_id,
            producer_attempt_id=old.attempt_id,
            recipe_id="asr.mlx-whisper.v3",
            lineage={"chunk": 0},
            source_span=chunk_span,
        )
        blobs.append(store.path(chunk_artifact))
        self.repository.settle_chunk(
            self.project.project_id,
            old.run_id,
            target.version_id,
            target.stream_index,
            0,
            chunk_artifact.artifact_id,
            result,
        )
        final_staging = store.create_staging_file(
            self.project.project_id, suffix=".json"
        )
        final_staging.write_text('{"text":"舊逐字稿"}', encoding="utf-8")
        final_artifact = store.publish(
            self.project.project_id,
            final_staging,
            kind=ArtifactKind.TRANSCRIPT,
            producer_operation_id=old.operation_id,
            producer_attempt_id=old.attempt_id,
            recipe_id="asr.mlx-whisper.v3",
            lineage={"chunks": [chunk_artifact.artifact_id]},
            source_span=target.audio.source_span,
        )
        blobs.append(store.path(final_artifact))
        self.repository.publish_transcript_revision(
            self.project.project_id,
            old.run_id,
            target.version_id,
            target.stream_index,
            artifact_id=final_artifact.artifact_id,
            language="zh",
            model={"id": "small"},
            recipe_id="asr.mlx-whisper.v3",
            source_span=target.audio.source_span,
            segments=(TimelineSegment(chunk_span, "舊逐字稿", "0.99"),),
        )
        old = self.repository.publish(self.project.project_id, old.run_id)
        self.assertEqual(self.repository.current_run(self.project.project_id), old)
        source_before = self.clip.read_bytes()

        with self.assertRaises(ProjectError) as invalid:
            self.repository.create_run_with_status(
                self.project.project_id,
                "request-analysis-invalid-replacement",
                self.project.revision,
                "scope_" + "0" * 32,
                recipe={"id": "audio.pcm16.v1"},
                model={"id": "small"},
            )
        self.assertEqual(invalid.exception.code, "scope_changed")
        self.assertEqual(self.repository.current_run(self.project.project_id), old)
        self.assertTrue(all(blob.exists() for blob in blobs))

        replacement, created, retired = self.repository.create_run_with_status(
            self.project.project_id,
            "request-analysis-replacement",
            self.project.revision,
            scope.scope_id,
            recipe={"id": "audio.pcm16.v1"},
            model={"id": "small"},
        )
        self.assertTrue(created)
        self.assertEqual(
            set(retired),
            {
                artifact.relative_path,
                chunk_artifact.relative_path,
                final_artifact.relative_path,
            },
        )
        self.assertIsNone(self.repository.current_run(self.project.project_id))
        self.assertEqual(self.repository.latest_run(self.project.project_id), replacement)
        with self.assertRaises(ProjectError) as missing:
            self.repository.get_run(self.project.project_id, old.run_id)
        self.assertEqual(missing.exception.code, "preparation_not_found")
        with self.assertRaises(ProjectError) as missing_artifact:
            store.get(self.project.project_id, artifact.artifact_id)
        self.assertEqual(missing_artifact.exception.code, "artifact_not_found")
        self.assertEqual(store.prune_unreferenced(self.project.project_id, retired), 3)
        self.assertFalse(any(blob.exists() for blob in blobs))
        self.assertEqual(self.clip.read_bytes(), source_before)
        with self.projects.connect(self.project.project_id) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM operation_events WHERE operation_id = ?",
                    (old.operation_id,),
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM transcription_revisions").fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM transcript_segments").fetchone()[0],
                0,
            )

    def test_chunk_checkpoints_are_validated_and_replayable(self) -> None:
        scope = self.repository.preflight(self.project.project_id)
        run, _, _ = self.repository.create_run_with_status(
            self.project.project_id,
            "request-analysis-chunks",
            self.project.revision,
            scope.scope_id,
            recipe={"id": "audio.pcm16.v1"},
            model={"id": "small"},
        )
        run = self.repository.transition(
            self.project.project_id,
            run.run_id,
            PreparationState.RUNNING,
            phase="extracting_audio",
        )
        target = next(item for item in scope.targets if item.audio is not None)
        store = ArtifactStore(self.projects)
        audio_staging = store.create_staging_file(self.project.project_id, suffix=".wav")
        audio_staging.write_bytes(b"normalized")
        audio = store.publish(
            self.project.project_id,
            audio_staging,
            kind=ArtifactKind.ANALYSIS_AUDIO,
            producer_operation_id=run.operation_id,
            producer_attempt_id=run.attempt_id,
            recipe_id="audio.pcm16.v1",
            lineage={"version_id": target.version_id},
            source_span=target.audio.source_span,
        )
        self.repository.settle_item(
            self.project.project_id,
            run.run_id,
            target.version_id,
            target.stream_index,
            PreparationItemStatus.PREPARED,
            audio_artifact_id=audio.artifact_id,
        )
        source_span = project_samples_to_source(
            target.audio.source_span, 0, 80_000, 16_000
        )
        chunk_path = self.root / "chunk.wav"
        chunks = (
            PreparedAudioChunk(chunk_path, source_span, 0, 80_000),
        )
        registered = self.repository.register_chunks(
            self.project.project_id,
            run.run_id,
            target.version_id,
            target.stream_index,
            chunks,
        )
        self.assertEqual(registered[0].status.value, "pending")
        self.assertEqual(
            self.repository.register_chunks(
                self.project.project_id,
                run.run_id,
                target.version_id,
                target.stream_index,
                chunks,
            ),
            registered,
        )

        result = TranscriptionResult(
            "zh",
            "手沖咖啡",
            (
                TranscriptSegment(
                    0,
                    16_000,
                    "手沖咖啡",
                    "0.01",
                    (TranscriptWord(0, 16_000, "手沖咖啡", "0.99"),),
                ),
            ),
        )
        transcript_staging = store.create_staging_file(
            self.project.project_id, suffix=".json"
        )
        transcript_staging.write_text("{}", encoding="utf-8")
        transcript = store.publish(
            self.project.project_id,
            transcript_staging,
            kind=ArtifactKind.TRANSCRIPT,
            producer_operation_id=run.operation_id,
            producer_attempt_id=run.attempt_id,
            recipe_id="asr.mlx-whisper.v1",
            lineage={"audio_artifact_id": audio.artifact_id, "chunk": 0},
            source_span=source_span,
        )
        completed = self.repository.settle_chunk(
            self.project.project_id,
            run.run_id,
            target.version_id,
            target.stream_index,
            0,
            transcript.artifact_id,
            result,
        )
        self.assertEqual(completed.result, result)
        self.assertEqual(
            self.repository.settle_chunk(
                self.project.project_id,
                run.run_id,
                target.version_id,
                target.stream_index,
                0,
                transcript.artifact_id,
                result,
            ),
            completed,
        )
        final_staging = store.create_staging_file(
            self.project.project_id, suffix=".json"
        )
        final_staging.write_text('{"text":"手沖咖啡"}', encoding="utf-8")
        final_artifact = store.publish(
            self.project.project_id,
            final_staging,
            kind=ArtifactKind.TRANSCRIPT,
            producer_operation_id=run.operation_id,
            producer_attempt_id=run.attempt_id,
            recipe_id="asr.mlx-whisper.v1",
            lineage={"chunks": [transcript.artifact_id]},
            source_span=target.audio.source_span,
        )
        segment_span = project_samples_to_source(source_span, 0, 16_000, 16_000)
        revision_id = self.repository.publish_transcript_revision(
            self.project.project_id,
            run.run_id,
            target.version_id,
            target.stream_index,
            artifact_id=final_artifact.artifact_id,
            language="zh",
            model={"id": "small"},
            recipe_id="asr.mlx-whisper.v1",
            source_span=target.audio.source_span,
            segments=(TimelineSegment(segment_span, "手沖咖啡", "0.99"),),
        )
        self.assertTrue(revision_id.startswith("trn_"))
        self.assertEqual(
            self.repository.publish_transcript_revision(
                self.project.project_id,
                run.run_id,
                target.version_id,
                target.stream_index,
                artifact_id=final_artifact.artifact_id,
                language="zh",
                model={"id": "small"},
                recipe_id="asr.mlx-whisper.v1",
                source_span=target.audio.source_span,
                segments=(TimelineSegment(segment_span, "手沖咖啡", "0.99"),),
            ),
            revision_id,
        )
        work = self.repository.list_work_items(self.project.project_id, run.run_id)
        self.assertEqual(len(work), 2)
        transcribed = next(item for item in work if item.target.audio is not None)
        self.assertEqual(transcribed.status, PreparationItemStatus.TRANSCRIBED)
        self.assertEqual(transcribed.transcript_revision_id, revision_id)
        self.assertNotIn(str(self.source), repr(work[0].target.to_public()))

    def test_cancel_and_restart_recovery_are_idempotent(self) -> None:
        scope = self.repository.preflight(self.project.project_id)
        queued, _, _ = self.repository.create_run_with_status(
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

        active, _, _ = self.repository.create_run_with_status(
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
        resumed, created = self.repository.resume_interrupted(
            self.project.project_id,
            active.run_id,
            "request-analysis-resume",
        )
        replay, replay_created = self.repository.resume_interrupted(
            self.project.project_id,
            active.run_id,
            "request-analysis-resume",
        )
        self.assertTrue(created)
        self.assertFalse(replay_created)
        self.assertEqual(replay, resumed)
        self.assertEqual(resumed.state, PreparationState.QUEUED)
        self.assertEqual(resumed.attempt_number, 2)
        self.assertNotEqual(resumed.attempt_id, active.attempt_id)
        self.repository.request_cancel(self.project.project_id, active.run_id)

    def test_preflight_requires_a_successful_inventory(self) -> None:
        other = self.projects.create_project("request-analysis-empty", "空作品")
        with self.assertRaises(ProjectError) as context:
            self.repository.preflight(other.project_id)
        self.assertEqual(context.exception.code, "inventory_required")

    def test_v3_analysis_skeleton_migrates_to_checkpoint_schema(self) -> None:
        database = (
            self.root
            / "projects"
            / self.project.project_id
            / "project.sqlite3"
        )
        run_id = "prep_" + "7" * 32
        with sqlite3.connect(database) as connection:
            version_id = connection.execute(
                "SELECT version_id FROM inventory_items WHERE run_id = ? "
                "AND display_name = 'clip.mov'",
                (self.inventory.run_id,),
            ).fetchone()[0]
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.executescript(
                """
                DROP TABLE evidence_observations;
                DROP TABLE evidence_results;
                DROP TABLE evidence_images;
                DROP TABLE evidence_cache;
                DROP TABLE evidence_runs;
                DROP TABLE preparation_chunks;
                DROP INDEX preparation_runs_operation;
                DROP INDEX preparation_runs_attempt;
                DROP TABLE preparation_items;
                CREATE TABLE preparation_items (
                    run_id TEXT NOT NULL REFERENCES preparation_runs(run_id)
                        ON DELETE CASCADE,
                    version_id TEXT NOT NULL REFERENCES asset_versions(version_id),
                    stream_index INTEGER,
                    status TEXT NOT NULL,
                    error_code TEXT,
                    checkpoint_json TEXT,
                    PRIMARY KEY(run_id, version_id)
                );
                CREATE INDEX preparation_items_run_status
                    ON preparation_items(run_id, status, version_id);
                ALTER TABLE preparation_runs DROP COLUMN attempt_number;
                ALTER TABLE preparation_runs DROP COLUMN attempt_id;
                ALTER TABLE preparation_runs DROP COLUMN operation_id;
                ALTER TABLE preparation_runs DROP COLUMN scope_id;
                ALTER TABLE preparation_runs DROP COLUMN project_revision;
                PRAGMA user_version = 3;
                """
            )
            connection.execute(
                "INSERT INTO preparation_runs "
                "(run_id, project_id, request_id, inventory_run_id, input_fingerprint, "
                "recipe_json, model_json, state, phase, created_at) "
                "VALUES (?, ?, 'legacy-request', ?, ?, '{}', '{}', 'queued', "
                "'preparing', '2026-09-02T00:00:00+00:00')",
                (run_id, self.project.project_id, self.inventory.run_id, "d" * 64),
            )
            connection.execute(
                "INSERT INTO preparation_items "
                "(run_id, version_id, stream_index, status) VALUES (?, ?, 1, 'pending')",
                (run_id, version_id),
            )

        migrated = ProjectRepository(self.root / "projects")
        with migrated.connect(self.project.project_id) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 10)
            run = connection.execute(
                "SELECT project_revision, scope_id, operation_id, attempt_id, "
                "attempt_number FROM preparation_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            item = connection.execute(
                "SELECT stream_index, audio_artifact_id FROM preparation_items "
                "WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            chunks_exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'preparation_chunks'"
            ).fetchone()
        self.assertEqual(run["project_revision"], self.project.revision)
        self.assertTrue(run["scope_id"].startswith("scope_"))
        self.assertTrue(run["operation_id"].startswith("op_"))
        self.assertTrue(run["attempt_id"].startswith("att_"))
        self.assertEqual(run["attempt_number"], 1)
        self.assertEqual(item["stream_index"], 1)
        self.assertIsNone(item["audio_artifact_id"])
        self.assertIsNotNone(chunks_exists)

    def test_coordinator_runs_audio_to_source_timed_transcript(self) -> None:
        FakeTranscriber.requests.clear()
        repository = PreparationRepository(self.projects)
        coordinator = PreparationCoordinator(
            repository,
            ArtifactStore(self.projects),
            transcriber_factory=FakeTranscriber,
            model={"id": "mlx-whisper-small", "revision": "test"},
            audio_factory=FakeAudioAdapter,
            recipe=PreparationRecipe(chunk_seconds=10),
        )
        scope = coordinator.preflight(self.project.project_id)
        before = self.clip.read_bytes()
        run = coordinator.start(
            self.project.project_id,
            "request-analysis-coordinator",
            self.project.revision,
            scope.scope_id,
            "ja",
            ["浅草", "スカイツリー"],
        )
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            run = repository.get_run(self.project.project_id, run.run_id)
            if run.state in {
                PreparationState.SUCCEEDED,
                PreparationState.FAILED,
                PreparationState.CANCELLED,
                PreparationState.INTERRUPTED,
            }:
                break
            time.sleep(0.01)
        coordinator.close()

        self.assertEqual(run.state, PreparationState.SUCCEEDED)
        self.assertEqual(run.counts.transcribed, 1)
        self.assertEqual(run.counts.no_audio, 1)
        self.assertEqual(
            FakeTranscriber.requests,
            [("ja", None)],
        )
        self.assertEqual(
            repository.recipe(self.project.project_id, run.run_id)["vocabulary"],
            ["浅草", "スカイツリー"],
        )
        self.assertEqual(self.clip.read_bytes(), before)
        with self.projects.connect(self.project.project_id) as connection:
            segment = connection.execute(
                "SELECT start_pts, end_pts, text FROM transcript_segments"
            ).fetchone()
        self.assertEqual(segment["start_pts"], "48000")
        self.assertEqual(segment["end_pts"], "96000")
        self.assertEqual(segment["text"], "手沖咖啡")

        results = repository.result_items(
            self.project.project_id, run.run_id, offset=0, limit=25
        )
        self.assertEqual(results["total"], 2)
        self.assertIsNone(results["next_offset"])
        by_status = {item["status"]: item for item in results["items"]}
        self.assertEqual(by_status["transcribed"]["display_name"], "clip.mov")
        self.assertEqual(by_status["transcribed"]["preview_text"], "手沖咖啡")
        self.assertEqual(by_status["transcribed"]["preview_start_pts"], "48000")
        self.assertEqual(by_status["transcribed"]["segment_count"], 1)
        self.assertEqual(
            by_status["transcribed"]["source"]["time_base"],
            {"num": 1, "den": 48000},
        )
        self.assertEqual(by_status["no_audio"]["display_name"], "silent.mov")
        self.assertNotIn(str(self.source), repr(results))

        transcribed = by_status["transcribed"]
        transcript = repository.transcript_segments(
            self.project.project_id,
            run.run_id,
            transcribed["version_id"],
            transcribed["stream_index"],
        )
        self.assertEqual(transcript["total"], 1)
        self.assertEqual(transcript["segments"][0]["start_pts"], "48000")
        self.assertEqual(transcript["segments"][0]["text"], "手沖咖啡")
        self.assertNotIn(str(self.source), repr(transcript))

    def test_coordinator_isolates_asr_failure_to_one_item(self) -> None:
        repository = PreparationRepository(self.projects)
        coordinator = PreparationCoordinator(
            repository,
            ArtifactStore(self.projects),
            transcriber_factory=FailingTranscriber,
            model={"id": "mlx-whisper-small", "revision": "test"},
            audio_factory=FakeAudioAdapter,
            recipe=PreparationRecipe(chunk_seconds=10),
        )
        scope = coordinator.preflight(self.project.project_id)
        run = coordinator.start(
            self.project.project_id,
            "request-analysis-failure",
            self.project.revision,
            scope.scope_id,
        )
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            run = repository.get_run(self.project.project_id, run.run_id)
            if run.state in {PreparationState.SUCCEEDED, PreparationState.FAILED}:
                break
            time.sleep(0.01)
        coordinator.close()
        self.assertEqual(run.state, PreparationState.FAILED)
        self.assertEqual(run.error_code, "asr_failed")
        self.assertEqual(run.counts.issues, 1)
        self.assertEqual(run.counts.no_audio, 1)
        results = repository.result_items(self.project.project_id, run.run_id)
        self.assertEqual(results["items"][0]["status"], "failed")
        self.assertEqual(results["items"][0]["issue_code"], "asr_failed")

    def test_coordinator_cancellation_reaches_adapter_and_settles(self) -> None:
        BlockingTranscriber.entered.clear()
        repository = PreparationRepository(self.projects)
        coordinator = PreparationCoordinator(
            repository,
            ArtifactStore(self.projects),
            transcriber_factory=BlockingTranscriber,
            model={"id": "mlx-whisper-small", "revision": "test"},
            audio_factory=FakeAudioAdapter,
            recipe=PreparationRecipe(chunk_seconds=10),
        )
        scope = coordinator.preflight(self.project.project_id)
        run = coordinator.start(
            self.project.project_id,
            "request-analysis-cancel-running",
            self.project.revision,
            scope.scope_id,
        )
        self.assertTrue(BlockingTranscriber.entered.wait(2))
        coordinator.cancel(self.project.project_id, run.run_id)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            run = repository.get_run(self.project.project_id, run.run_id)
            if run.state is PreparationState.CANCELLED:
                break
            time.sleep(0.01)
        coordinator.close()
        self.assertEqual(run.state, PreparationState.CANCELLED)


if __name__ == "__main__":
    unittest.main()
