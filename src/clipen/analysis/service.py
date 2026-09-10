"""Single-worker orchestration for local, recoverable media preparation."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import queue
import threading
from typing import Callable, Iterable, Mapping, Protocol

from clipen.projects.models import ProjectError

from .artifacts import ArtifactStore
from .audio import (
    AudioPreparationError,
    FFmpegAudioAdapter,
    PCMChunker,
    PreparedAudio,
    inspect_normalized_audio,
    project_samples_to_source,
)
from .models import (
    ArtifactKind,
    PreparationChunkStatus,
    PreparationItemStatus,
    PreparationRun,
    PreparationState,
    TimelineSegment,
)
from .storage import PreparationRepository, PreparationScope, PreparationWorkItem
from .transcription import TranscriptionError, TranscriptionResult


class _AudioAdapter(Protocol):
    def version(self) -> str: ...

    def extract(
        self, source, output: Path, cancel: threading.Event | None = None
    ) -> PreparedAudio: ...


class _Transcriber(Protocol):
    def probe(self) -> str: ...

    def transcribe(
        self,
        audio: PreparedAudio,
        output: Path,
        *,
        language: str = "zh",
        cancel: threading.Event | None = None,
    ) -> TranscriptionResult: ...


@dataclass(frozen=True)
class PreparationRecipe:
    audio_recipe_id: str = "audio.pcm16-16khz-mono.v1"
    transcript_recipe_id: str = "asr.mlx-whisper.v1"
    chunk_seconds: int = 5 * 60
    language: str = "zh"

    def __post_init__(self) -> None:
        PCMChunker(chunk_seconds=self.chunk_seconds, overlap_seconds=0)
        if not self.audio_recipe_id or not self.transcript_recipe_id:
            raise ValueError("preparation recipe IDs are required")

    def to_storage(self) -> dict[str, object]:
        return {
            "audio_recipe_id": self.audio_recipe_id,
            "transcript_recipe_id": self.transcript_recipe_id,
            "chunk_seconds": self.chunk_seconds,
            "overlap_seconds": 0,
            "language": self.language,
        }


@dataclass(frozen=True)
class _Job:
    project_id: str
    run_id: str


class PreparationCoordinator:
    """Executes one heavy preparation at a time and checkpoints every boundary."""

    def __init__(
        self,
        repository: PreparationRepository,
        artifacts: ArtifactStore,
        *,
        transcriber_factory: Callable[[], _Transcriber],
        model: Mapping[str, object],
        audio_factory: Callable[[], _AudioAdapter] = FFmpegAudioAdapter,
        recipe: PreparationRecipe = PreparationRecipe(),
    ) -> None:
        self.repository = repository
        self.artifacts = artifacts
        self._transcriber_factory = transcriber_factory
        self._model = dict(model)
        self._audio_factory = audio_factory
        self.recipe = recipe
        self._queue: queue.Queue[_Job | None] = queue.Queue()
        self._lock = threading.Lock()
        self._cancellations: dict[str, threading.Event] = {}
        self._projects: set[str] = set()
        self._worker: threading.Thread | None = None
        self._shutdown = threading.Event()

    def preflight(self, project_id: str) -> PreparationScope:
        return self.repository.preflight(project_id)

    def recover_incomplete(self, project_ids: Iterable[str]) -> int:
        recovered = 0
        for project_id in project_ids:
            recovered += self.repository.interrupt_incomplete(project_id)
        return recovered

    def start(
        self,
        project_id: str,
        request_id: object,
        expected_revision: object,
        scope_id: object,
    ) -> PreparationRun:
        run, created, retired_artifact_paths = self.repository.create_run_with_status(
            project_id,
            request_id,
            expected_revision,
            scope_id,
            recipe=self.recipe.to_storage(),
            model=self._model,
        )
        if not created:
            return run
        try:
            self.artifacts.prune_unreferenced(project_id, retired_artifact_paths)
        except ProjectError:
            # The replacement run is already durable. Cleanup can be retried later
            # without preventing the new analysis from entering the worker queue.
            pass
        with self._lock:
            if self._shutdown.is_set():
                return self.repository.transition(
                    project_id,
                    run.run_id,
                    PreparationState.INTERRUPTED,
                    phase="interrupted",
                    error_code="controller_stopped",
                )
            self._projects.add(project_id)
            self._cancellations[run.run_id] = threading.Event()
            self._ensure_worker_locked()
            self._queue.put(_Job(project_id, run.run_id))
        return run

    def cancel(self, project_id: str, run_id: str) -> PreparationRun:
        run = self.repository.request_cancel(project_id, run_id)
        if run.state in {PreparationState.CANCELLING, PreparationState.CANCELLED}:
            with self._lock:
                event = self._cancellations.get(run.run_id)
                if event is not None:
                    event.set()
        return run

    def resume(self, project_id: str, run_id: str, request_id: object) -> PreparationRun:
        run, resumed = self.repository.resume_interrupted(project_id, run_id, request_id)
        if not resumed:
            return run
        with self._lock:
            if self._shutdown.is_set():
                return self.repository.transition(
                    project_id,
                    run.run_id,
                    PreparationState.INTERRUPTED,
                    phase="interrupted",
                    error_code="controller_stopped",
                )
            self._projects.add(project_id)
            self._cancellations[run.run_id] = threading.Event()
            self._ensure_worker_locked()
            self._queue.put(_Job(project_id, run.run_id))
        return run

    def close(self, timeout: float = 3) -> None:
        with self._lock:
            if self._shutdown.is_set():
                return
            self._shutdown.set()
            for event in self._cancellations.values():
                event.set()
            worker = self._worker
            self._queue.put(None)
        if worker is not None:
            worker.join(max(0, timeout))
        for project_id in tuple(self._projects):
            self.repository.interrupt_incomplete(project_id)

    def _ensure_worker_locked(self) -> None:
        if self._worker is None:
            self._worker = threading.Thread(
                target=self._work_loop,
                name="clipen-media-preparation",
                daemon=True,
            )
            self._worker.start()

    def _work_loop(self) -> None:
        while True:
            job = self._queue.get()
            try:
                if job is None:
                    return
                self._execute(job)
            finally:
                self._queue.task_done()

    def _execute(self, job: _Job) -> None:
        with self._lock:
            cancel = self._cancellations.get(job.run_id)
        if cancel is None:
            return
        try:
            queued = self.repository.get_run(job.project_id, job.run_id)
            if queued.state is not PreparationState.QUEUED:
                return
            if self._shutdown.is_set():
                self._interrupt(job, "controller_stopped")
                return
            run = self.repository.transition(
                job.project_id,
                job.run_id,
                PreparationState.RUNNING,
                phase="extracting_audio",
            )
            audio_adapter = self._audio_factory()
            transcriber = self._transcriber_factory()
            audio_adapter.version()
            transcriber.probe()
            for item in self.repository.list_work_items(job.project_id, job.run_id):
                if cancel.is_set():
                    self._finish_cancel_or_interrupt(job)
                    return
                if item.status in {
                    PreparationItemStatus.NO_AUDIO,
                    PreparationItemStatus.FAILED,
                    PreparationItemStatus.TRANSCRIBED,
                    PreparationItemStatus.NO_SPEECH,
                }:
                    continue
                self._process_item(job, run, item, audio_adapter, transcriber, cancel)
            if cancel.is_set():
                self._finish_cancel_or_interrupt(job)
                return
            self.repository.publish(job.project_id, job.run_id)
        except (AudioPreparationError, TranscriptionError) as error:
            if error.code == "cancelled":
                self._finish_cancel_or_interrupt(job)
            else:
                self._fail_run(job, error.code)
        except ProjectError as error:
            self._settle_conflict(job, error.code)
        except Exception:
            self._fail_run(job, "preparation_internal_error")
        finally:
            with self._lock:
                self._cancellations.pop(job.run_id, None)

    def _process_item(
        self,
        job: _Job,
        run: PreparationRun,
        item: PreparationWorkItem,
        audio_adapter: _AudioAdapter,
        transcriber: _Transcriber,
        cancel: threading.Event,
    ) -> None:
        target = item.target
        if target.audio is None:
            return
        try:
            artifact_id = item.audio_artifact_id
            if item.status is PreparationItemStatus.PENDING:
                staging = self.artifacts.create_staging_file(job.project_id, suffix=".wav")
                try:
                    audio_adapter.extract(target.audio, staging, cancel)
                    artifact = self.artifacts.publish(
                        job.project_id,
                        staging,
                        kind=ArtifactKind.ANALYSIS_AUDIO,
                        producer_operation_id=run.operation_id,
                        producer_attempt_id=run.attempt_id,
                        recipe_id=self.recipe.audio_recipe_id,
                        lineage={
                            "inventory_run_id": run.inventory_run_id,
                            "version_id": target.version_id,
                            "stream_index": target.stream_index,
                        },
                        source_span=target.audio.source_span,
                    )
                finally:
                    staging.unlink(missing_ok=True)
                artifact_id = artifact.artifact_id
                self.repository.settle_item(
                    job.project_id,
                    job.run_id,
                    target.version_id,
                    target.stream_index,
                    PreparationItemStatus.PREPARED,
                    audio_artifact_id=artifact_id,
                    checkpoint={"audio_ready": True},
                )
            if artifact_id is None:
                raise ProjectError(
                    "storage_corrupt", "Clipen 找不到已準備的音訊。", status_code=500
                )
            audio_record = self.artifacts.get(job.project_id, artifact_id)
            prepared = inspect_normalized_audio(self.artifacts.path(audio_record))
            chunker = PCMChunker(
                chunk_seconds=self.recipe.chunk_seconds, overlap_seconds=0
            )
            chunk_paths: list[Path] = []

            def make_chunk() -> Path:
                path = self.artifacts.create_staging_file(job.project_id, suffix=".wav")
                chunk_paths.append(path)
                return path

            chunks = chunker.split(prepared, target.audio.source_span, make_chunk)
            try:
                stored = self.repository.register_chunks(
                    job.project_id,
                    job.run_id,
                    target.version_id,
                    target.stream_index,
                    chunks,
                )
                completed = []
                for chunk_file, checkpoint in zip(chunks, stored, strict=True):
                    if cancel.is_set():
                        raise TranscriptionError("cancelled")
                    if checkpoint.status is PreparationChunkStatus.PENDING:
                        chunk_audio = inspect_normalized_audio(chunk_file.path)
                        output = self.artifacts.create_staging_file(
                            job.project_id, suffix=".json"
                        )
                        try:
                            result = transcriber.transcribe(
                                chunk_audio,
                                output,
                                language=self.recipe.language,
                                cancel=cancel,
                            )
                            transcript = self.artifacts.publish(
                                job.project_id,
                                output,
                                kind=ArtifactKind.TRANSCRIPT,
                                producer_operation_id=run.operation_id,
                                producer_attempt_id=run.attempt_id,
                                recipe_id=self.recipe.transcript_recipe_id,
                                lineage={
                                    "audio_artifact_id": artifact_id,
                                    "chunk_ordinal": checkpoint.ordinal,
                                },
                                source_span=checkpoint.source_span,
                            )
                        finally:
                            output.unlink(missing_ok=True)
                        checkpoint = self.repository.settle_chunk(
                            job.project_id,
                            job.run_id,
                            target.version_id,
                            target.stream_index,
                            checkpoint.ordinal,
                            transcript.artifact_id,
                            result,
                        )
                    completed.append(checkpoint)
                self._publish_transcript(job, run, item, artifact_id, tuple(completed))
            finally:
                for path in chunk_paths:
                    path.unlink(missing_ok=True)
        except (AudioPreparationError, TranscriptionError) as error:
            if error.code == "cancelled":
                raise
            self._fail_item(job, target, error.code)
        except ProjectError:
            raise
        except Exception:
            self._fail_item(job, target, "preparation_item_internal_error")

    def _publish_transcript(self, job, run, item, audio_artifact_id, chunks) -> None:
        target = item.target
        timeline: list[TimelineSegment] = []
        payload_segments: list[dict[str, object]] = []
        texts: list[str] = []
        chunk_artifacts: list[str] = []
        for chunk in chunks:
            if chunk.result is None or chunk.transcript_artifact_id is None:
                raise ProjectError(
                    "storage_corrupt", "Clipen 找不到逐字稿切塊。", status_code=500
                )
            chunk_artifacts.append(chunk.transcript_artifact_id)
            if chunk.result.text.strip():
                texts.append(chunk.result.text.strip())
            for segment in chunk.result.segments:
                span = project_samples_to_source(
                    target.audio.source_span,
                    chunk.start_sample + segment.start_sample,
                    chunk.start_sample + segment.end_sample,
                    16_000,
                )
                timeline.append(TimelineSegment(span, segment.text))
                words = []
                for word in segment.words:
                    word_span = project_samples_to_source(
                        target.audio.source_span,
                        chunk.start_sample + word.start_sample,
                        chunk.start_sample + word.end_sample,
                        16_000,
                    )
                    words.append(
                        {
                            "source_span": word_span.to_storage(),
                            "text": word.text,
                            "probability": word.probability,
                        }
                    )
                payload_segments.append(
                    {
                        "source_span": span.to_storage(),
                        "text": segment.text,
                        "no_speech_probability": segment.no_speech_probability,
                        "words": words,
                    }
                )
        output = self.artifacts.create_staging_file(job.project_id, suffix=".json")
        try:
            output.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "language": self.recipe.language,
                        "text": "\n".join(texts),
                        "segments": payload_segments,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            artifact = self.artifacts.publish(
                job.project_id,
                output,
                kind=ArtifactKind.TRANSCRIPT,
                producer_operation_id=run.operation_id,
                producer_attempt_id=run.attempt_id,
                recipe_id=self.recipe.transcript_recipe_id,
                lineage={
                    "audio_artifact_id": audio_artifact_id,
                    "chunk_artifact_ids": chunk_artifacts,
                },
                source_span=target.audio.source_span,
            )
        finally:
            output.unlink(missing_ok=True)
        self.repository.publish_transcript_revision(
            job.project_id,
            job.run_id,
            target.version_id,
            target.stream_index,
            artifact_id=artifact.artifact_id,
            language=self.recipe.language,
            model=self._model,
            recipe_id=self.recipe.transcript_recipe_id,
            source_span=target.audio.source_span,
            segments=tuple(timeline),
        )

    def _fail_item(self, job, target, code: str) -> None:
        current = self.repository.get_run(job.project_id, job.run_id)
        if current.state is not PreparationState.RUNNING:
            return
        self.repository.settle_item(
            job.project_id,
            job.run_id,
            target.version_id,
            target.stream_index,
            PreparationItemStatus.FAILED,
            error_code=code,
        )

    def _finish_cancel_or_interrupt(self, job: _Job) -> None:
        current = self.repository.get_run(job.project_id, job.run_id)
        if current.state is PreparationState.CANCELLING:
            self.repository.transition(
                job.project_id,
                job.run_id,
                PreparationState.CANCELLED,
                phase="cancelled",
            )
        elif current.state in {PreparationState.QUEUED, PreparationState.RUNNING}:
            self._interrupt(job, "controller_stopped")

    def _interrupt(self, job: _Job, code: str) -> None:
        try:
            self.repository.transition(
                job.project_id,
                job.run_id,
                PreparationState.INTERRUPTED,
                phase="interrupted",
                error_code=code,
            )
        except ProjectError:
            pass

    def _fail_run(self, job: _Job, code: str) -> None:
        try:
            current = self.repository.get_run(job.project_id, job.run_id)
            if current.state in {PreparationState.RUNNING, PreparationState.CANCELLING}:
                self.repository.transition(
                    job.project_id,
                    job.run_id,
                    PreparationState.FAILED,
                    phase="failed",
                    error_code=code,
                )
        except ProjectError:
            pass

    def _settle_conflict(self, job: _Job, code: str) -> None:
        current = self.repository.get_run(job.project_id, job.run_id)
        if current.state is PreparationState.CANCELLING:
            self._finish_cancel_or_interrupt(job)
        elif current.state is PreparationState.RUNNING:
            self._fail_run(job, code)
