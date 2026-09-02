"""Transactional preparation operations bound to an immutable media inventory."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import hashlib
import json
import re
import sqlite3
from typing import Mapping, Any

from clipen.media import InventoryRoot
from clipen.projects.models import (
    DirectoryRole,
    ProjectError,
    validate_project_id,
    validate_request_id,
    validate_revision,
)
from clipen.projects.storage import ProjectRepository, command_digest, utc_now

from .audio import AudioPreparationInput, PreparedAudioChunk
from .models import (
    PreparationChunkStatus,
    PreparationCounts,
    PreparationItemStatus,
    PreparationRun,
    PreparationState,
    Rational,
    SourceSpan,
    TimelineSegment,
)
from .transcription import TranscriptionResult


_TERMINAL_STATES = frozenset(
    {
        PreparationState.SUCCEEDED,
        PreparationState.FAILED,
        PreparationState.CANCELLED,
        PreparationState.INTERRUPTED,
    }
)
_TRANSITIONS = {
    PreparationState.QUEUED: {
        PreparationState.RUNNING,
        PreparationState.CANCELLED,
        PreparationState.INTERRUPTED,
    },
    PreparationState.RUNNING: {
        PreparationState.CANCELLING,
        PreparationState.FAILED,
        PreparationState.INTERRUPTED,
    },
    PreparationState.CANCELLING: {
        PreparationState.CANCELLED,
        PreparationState.FAILED,
        PreparationState.INTERRUPTED,
    },
}


@dataclass(frozen=True)
class PreparationTarget:
    version_id: str
    role: DirectoryRole
    relative_path: str
    display_name: str
    stream_index: int
    audio: AudioPreparationInput | None
    initial_status: PreparationItemStatus
    issue_code: str | None = None

    def __post_init__(self) -> None:
        if self.stream_index < -1:
            raise ValueError("preparation target stream is invalid")
        if self.initial_status is PreparationItemStatus.PENDING:
            if self.audio is None or self.stream_index < 0 or self.issue_code is not None:
                raise ValueError("pending preparation target requires valid audio")
        elif self.initial_status is PreparationItemStatus.NO_AUDIO:
            if self.audio is not None or self.stream_index != -1 or self.issue_code is not None:
                raise ValueError("no-audio target is inconsistent")
        elif self.initial_status is PreparationItemStatus.FAILED:
            if self.audio is not None or self.issue_code is None:
                raise ValueError("failed preparation target requires an issue")
        else:
            raise ValueError("new preparation target has an invalid initial state")

    def to_public(self) -> dict[str, object]:
        return {
            "version_id": self.version_id,
            "role": self.role.value,
            "display_name": self.display_name,
            "stream_index": None if self.stream_index < 0 else self.stream_index,
            "status": self.initial_status.value,
            "issue_code": self.issue_code,
        }


@dataclass(frozen=True)
class PreparationScope:
    scope_id: str
    project_id: str
    project_revision: int
    inventory_run_id: str
    targets: tuple[PreparationTarget, ...]
    estimated_audio_bytes: int
    duration_seconds: str

    def __post_init__(self) -> None:
        if re.fullmatch(r"scope_[0-9a-f]{32}", self.scope_id) is None:
            raise ValueError("preparation scope ID is invalid")
        validate_project_id(self.project_id)
        if self.project_revision < 1 or not self.targets:
            raise ValueError("preparation scope is incomplete")
        if self.estimated_audio_bytes < 0:
            raise ValueError("preparation estimate is invalid")

    def to_public(self) -> dict[str, object]:
        pending = sum(
            target.initial_status is PreparationItemStatus.PENDING
            for target in self.targets
        )
        no_audio = sum(
            target.initial_status is PreparationItemStatus.NO_AUDIO
            for target in self.targets
        )
        issues = sum(
            target.initial_status is PreparationItemStatus.FAILED
            for target in self.targets
        )
        return {
            "scope_id": self.scope_id,
            "inventory_run_id": self.inventory_run_id,
            "project_revision": self.project_revision,
            "counts": {
                "audio_streams": pending,
                "no_audio": no_audio,
                "issues": issues,
            },
            "duration_seconds": self.duration_seconds,
            "estimated_audio_bytes": self.estimated_audio_bytes,
            "items": [target.to_public() for target in self.targets],
        }


@dataclass(frozen=True)
class PreparationWorkItem:
    target: PreparationTarget
    status: PreparationItemStatus
    audio_artifact_id: str | None
    transcript_revision_id: str | None
    error_code: str | None


@dataclass(frozen=True)
class PreparationChunk:
    ordinal: int
    start_sample: int
    end_sample: int
    source_span: SourceSpan
    status: PreparationChunkStatus
    transcript_artifact_id: str | None
    result: TranscriptionResult | None

    def __post_init__(self) -> None:
        if self.ordinal < 0 or self.start_sample < 0 or self.end_sample <= self.start_sample:
            raise ValueError("stored preparation chunk range is invalid")
        completed = self.status in {
            PreparationChunkStatus.TRANSCRIBED,
            PreparationChunkStatus.NO_SPEECH,
        }
        if completed != (self.transcript_artifact_id is not None and self.result is not None):
            raise ValueError("stored preparation chunk result is inconsistent")


class PreparationRepository:
    def __init__(self, projects: ProjectRepository) -> None:
        self.projects = projects

    def preflight(self, project_id: str) -> PreparationScope:
        validate_project_id(project_id)
        with self.projects.connect(project_id) as connection:
            project = connection.execute(
                "SELECT revision FROM project WHERE project_id = ?", (project_id,)
            ).fetchone()
            current = connection.execute(
                "SELECT i.run_id, r.roots_json FROM current_inventory i "
                "JOIN inventory_runs r ON r.run_id = i.run_id WHERE i.slot = 1"
            ).fetchone()
            if project is None:
                raise ProjectError("project_not_found", "找不到這個作品。", status_code=404)
            if current is None:
                raise ProjectError(
                    "inventory_required",
                    "請先完成素材檢查。",
                    status_code=409,
                )
            try:
                raw_roots = json.loads(current["roots_json"])
                roots = {
                    root.role: root
                    for root in (
                        InventoryRoot.from_storage(value) for value in raw_roots
                    )
                }
            except (json.JSONDecodeError, TypeError, ValueError, KeyError) as error:
                raise ProjectError(
                    "storage_corrupt", "Clipen 無法讀取素材範圍。", status_code=500
                ) from error
            rows = connection.execute(
                "SELECT a.role, a.relative_path, i.display_name, i.kind, i.version_id, "
                "v.size_bytes, v.mtime_ns, v.fingerprint, s.stream_index, s.details_json "
                "FROM inventory_items i JOIN assets a ON a.asset_id = i.asset_id "
                "JOIN asset_versions v ON v.version_id = i.version_id "
                "LEFT JOIN media_streams s ON s.version_id = i.version_id "
                "AND s.kind = 'audio' WHERE i.run_id = ? AND i.status = 'usable' "
                "AND i.kind IN ('video', 'audio') "
                "ORDER BY a.role, a.relative_path, s.stream_index",
                (current["run_id"],),
            ).fetchall()
        if not rows:
            raise ProjectError(
                "preparation_empty",
                "目前沒有可準備的影音素材。",
                status_code=409,
            )
        targets: list[PreparationTarget] = []
        duration = Fraction(0)
        seen_no_audio: set[str] = set()
        for row in rows:
            role = DirectoryRole(row["role"])
            root = roots.get(role)
            if root is None:
                raise ProjectError(
                    "storage_corrupt", "Clipen 無法讀取素材範圍。", status_code=500
                )
            if row["stream_index"] is None:
                if row["version_id"] not in seen_no_audio:
                    seen_no_audio.add(row["version_id"])
                    targets.append(
                        PreparationTarget(
                            row["version_id"],
                            role,
                            row["relative_path"],
                            row["display_name"],
                            -1,
                            None,
                            PreparationItemStatus.NO_AUDIO,
                        )
                    )
                continue
            try:
                details = json.loads(row["details_json"])
                span = _source_span(
                    row["version_id"], row["stream_index"], details
                )
                audio = AudioPreparationInput(
                    root,
                    row["relative_path"],
                    row["version_id"],
                    row["stream_index"],
                    row["size_bytes"],
                    row["mtime_ns"],
                    row["fingerprint"],
                    span,
                )
                duration += Fraction(
                    int(span.end_pts) - int(span.start_pts)
                ) * Fraction(span.time_base.numerator, span.time_base.denominator)
                targets.append(
                    PreparationTarget(
                        row["version_id"],
                        role,
                        row["relative_path"],
                        row["display_name"],
                        row["stream_index"],
                        audio,
                        PreparationItemStatus.PENDING,
                    )
                )
            except (json.JSONDecodeError, TypeError, ValueError, KeyError, ZeroDivisionError):
                targets.append(
                    PreparationTarget(
                        row["version_id"],
                        role,
                        row["relative_path"],
                        row["display_name"],
                        row["stream_index"],
                        None,
                        PreparationItemStatus.FAILED,
                        "audio_time_metadata_missing",
                    )
                )
        identity = {
            "project_revision": project["revision"],
            "inventory_run_id": current["run_id"],
            "targets": [
                {
                    "version_id": target.version_id,
                    "stream_index": target.stream_index,
                    "status": target.initial_status.value,
                    "issue_code": target.issue_code,
                }
                for target in targets
            ],
        }
        encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        scope_id = "scope_" + hashlib.sha256(encoded).hexdigest()[:32]
        estimated = math_ceil_fraction(duration * 16_000 * 2) + len(targets) * 4096
        return PreparationScope(
            scope_id,
            project_id,
            project["revision"],
            current["run_id"],
            tuple(targets),
            estimated,
            _fraction_decimal(duration),
        )

    def create_run_with_status(
        self,
        project_id: str,
        request_id: object,
        expected_revision: object,
        scope_id: object,
        *,
        recipe: Mapping[str, object],
        model: Mapping[str, object],
    ) -> tuple[PreparationRun, bool]:
        validate_project_id(project_id)
        normalized_request = validate_request_id(request_id)
        revision = validate_revision(expected_revision)
        if not isinstance(scope_id, str):
            raise ProjectError("scope_invalid", "分析範圍無效。", status_code=422)
        recipe_json = _bounded_json(recipe, "analysis recipe")
        model_json = _bounded_json(model, "ASR model")
        request_hash = command_digest(
            {
                "expected_revision": revision,
                "scope_id": scope_id,
                "recipe": json.loads(recipe_json),
                "model": json.loads(model_json),
            }
        )
        # A receipt is the durable response to an already accepted command. Read it
        # before recomputing the live scope so retries stay idempotent even after a
        # later project edit or inventory refresh.
        with self.projects.connect(project_id) as connection:
            receipt = connection.execute(
                "SELECT command_type, request_hash, run_id "
                "FROM preparation_command_receipts WHERE request_id = ?",
                (normalized_request,),
            ).fetchone()
            if receipt is not None:
                if (
                    receipt["command_type"] != "start_preparation"
                    or receipt["request_hash"] != request_hash
                ):
                    raise ProjectError(
                        "request_reused",
                        "這次操作的識別資訊已用於不同內容。",
                        status_code=409,
                    )
                return self._read_run(connection, receipt["run_id"]), False

        scope = self.preflight(project_id)
        if scope.scope_id != scope_id or scope.project_revision != revision:
            raise ProjectError(
                "scope_changed",
                "素材或作品內容已改變，請重新確認分析範圍。",
                status_code=409,
            )
        run_id = _derived_id("prep", project_id, normalized_request)
        operation_id = _derived_id("op", run_id, scope_id)
        attempt_id = _derived_id("att", operation_id, "1")
        input_fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "scope_id": scope_id,
                    "recipe": json.loads(recipe_json),
                    "model": json.loads(model_json),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        with self.projects.connect(project_id) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                receipt = connection.execute(
                    "SELECT command_type, request_hash, run_id "
                    "FROM preparation_command_receipts WHERE request_id = ?",
                    (normalized_request,),
                ).fetchone()
                if receipt is not None:
                    if (
                        receipt["command_type"] != "start_preparation"
                        or receipt["request_hash"] != request_hash
                    ):
                        raise ProjectError(
                            "request_reused",
                            "這次操作的識別資訊已用於不同內容。",
                            status_code=409,
                        )
                    replay = self._read_run(connection, receipt["run_id"])
                    connection.rollback()
                    return replay, False
                live = connection.execute(
                    "SELECT p.revision, c.run_id AS inventory_run_id FROM project p "
                    "LEFT JOIN current_inventory c ON c.slot = 1 "
                    "WHERE p.project_id = ?",
                    (project_id,),
                ).fetchone()
                if (
                    live is None
                    or live["revision"] != scope.project_revision
                    or live["inventory_run_id"] != scope.inventory_run_id
                ):
                    raise ProjectError(
                        "scope_changed",
                        "素材或作品內容已改變，請重新確認分析範圍。",
                        status_code=409,
                    )
                busy = connection.execute(
                    "SELECT run_id FROM preparation_runs "
                    "WHERE state IN ('queued', 'running', 'cancelling') LIMIT 1"
                ).fetchone()
                if busy is not None:
                    raise ProjectError(
                        "preparation_busy",
                        "已有一個素材分析正在進行。",
                        status_code=409,
                    )
                now = utc_now()
                initial_no_audio = sum(
                    target.initial_status is PreparationItemStatus.NO_AUDIO
                    for target in scope.targets
                )
                initial_issues = sum(
                    target.initial_status is PreparationItemStatus.FAILED
                    for target in scope.targets
                )
                connection.execute(
                    "INSERT INTO preparation_runs "
                    "(run_id, project_id, request_id, project_revision, inventory_run_id, "
                    "scope_id, operation_id, attempt_id, attempt_number, input_fingerprint, "
                    "recipe_json, model_json, state, phase, total_count, no_audio_count, "
                    "issue_count, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, "
                    "'queued', 'preparing', ?, ?, ?, ?)",
                    (
                        run_id,
                        project_id,
                        normalized_request,
                        revision,
                        scope.inventory_run_id,
                        scope_id,
                        operation_id,
                        attempt_id,
                        input_fingerprint,
                        recipe_json,
                        model_json,
                        len(scope.targets),
                        initial_no_audio,
                        initial_issues,
                        now,
                    ),
                )
                for target in scope.targets:
                    connection.execute(
                        "INSERT INTO preparation_items "
                        "(run_id, version_id, stream_index, status, error_code) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (
                            run_id,
                            target.version_id,
                            target.stream_index,
                            target.initial_status.value,
                            target.issue_code,
                        ),
                    )
                connection.execute(
                    "INSERT INTO preparation_command_receipts "
                    "(request_id, command_type, request_hash, run_id, created_at) "
                    "VALUES (?, 'start_preparation', ?, ?, ?)",
                    (normalized_request, request_hash, run_id, now),
                )
                self._append_event(
                    connection,
                    project_id,
                    operation_id,
                    "queued",
                    {"phase": "preparing"},
                    now,
                )
                created = self._read_run(connection, run_id)
                connection.commit()
                return created, True
            except ProjectError:
                connection.rollback()
                raise
            except sqlite3.Error as error:
                connection.rollback()
                raise ProjectError(
                    "storage_failed", "Clipen 無法建立素材分析。", status_code=500
                ) from error

    def get_run(self, project_id: str, run_id: str) -> PreparationRun:
        validate_project_id(project_id)
        with self.projects.connect(project_id) as connection:
            run = self._read_run(connection, run_id)
        if run.project_id != project_id:
            raise ProjectError("preparation_not_found", "找不到這次素材分析。", status_code=404)
        return run

    def latest_run(self, project_id: str) -> PreparationRun | None:
        validate_project_id(project_id)
        with self.projects.connect(project_id) as connection:
            row = connection.execute(
                "SELECT run_id FROM preparation_runs ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            return None if row is None else self._read_run(connection, row["run_id"])

    def current_run(self, project_id: str) -> PreparationRun | None:
        validate_project_id(project_id)
        with self.projects.connect(project_id) as connection:
            row = connection.execute(
                "SELECT run_id FROM current_preparation WHERE slot = 1"
            ).fetchone()
            return None if row is None else self._read_run(connection, row["run_id"])

    def list_events(
        self,
        project_id: str,
        run_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> tuple[dict[str, object], ...]:
        validate_project_id(project_id)
        if (
            isinstance(after_sequence, bool)
            or not isinstance(after_sequence, int)
            or after_sequence < 0
            or isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 500
        ):
            raise ValueError("operation event bounds are invalid")
        with self.projects.connect(project_id) as connection:
            run = self._read_run(connection, run_id)
            if run.project_id != project_id:
                raise ProjectError(
                    "preparation_not_found", "找不到這次素材分析。", status_code=404
                )
            rows = connection.execute(
                "SELECT sequence, event_type, payload_json, created_at "
                "FROM operation_events WHERE project_id = ? AND operation_id = ? "
                "AND sequence > ? ORDER BY sequence LIMIT ?",
                (project_id, run.operation_id, after_sequence, limit),
            ).fetchall()
        events = []
        try:
            for row in rows:
                payload = json.loads(row["payload_json"])
                if not isinstance(payload, dict):
                    raise ValueError("event payload is not an object")
                events.append(
                    {
                        "sequence": row["sequence"],
                        "type": row["event_type"],
                        "payload": payload,
                        "created_at": row["created_at"],
                    }
                )
        except (json.JSONDecodeError, TypeError, ValueError) as error:
            raise ProjectError(
                "storage_corrupt", "Clipen 無法讀取分析進度。", status_code=500
            ) from error
        return tuple(events)

    def list_work_items(
        self, project_id: str, run_id: str
    ) -> tuple[PreparationWorkItem, ...]:
        run = self.get_run(project_id, run_id)
        scope = self.preflight(project_id)
        if scope.scope_id != run.scope_id or scope.inventory_run_id != run.inventory_run_id:
            raise ProjectError(
                "scope_changed",
                "素材或作品內容已改變，無法繼續這次分析。",
                status_code=409,
            )
        targets = {
            (target.version_id, target.stream_index): target for target in scope.targets
        }
        with self.projects.connect(project_id) as connection:
            rows = connection.execute(
                "SELECT version_id, stream_index, status, audio_artifact_id, "
                "transcript_revision_id, error_code FROM preparation_items "
                "WHERE run_id = ? ORDER BY version_id, stream_index",
                (run_id,),
            ).fetchall()
        try:
            return tuple(
                PreparationWorkItem(
                    targets[(row["version_id"], row["stream_index"])],
                    PreparationItemStatus(row["status"]),
                    row["audio_artifact_id"],
                    row["transcript_revision_id"],
                    row["error_code"],
                )
                for row in rows
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ProjectError(
                "storage_corrupt", "Clipen 無法讀取分析項目。", status_code=500
            ) from error

    def register_chunks(
        self,
        project_id: str,
        run_id: str,
        version_id: str,
        stream_index: int,
        chunks: tuple[PreparedAudioChunk, ...],
    ) -> tuple[PreparationChunk, ...]:
        if not chunks:
            raise ValueError("preparation requires at least one audio chunk")
        with self.projects.connect(project_id) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                run = self._read_run(connection, run_id)
                if run.project_id != project_id or run.state is not PreparationState.RUNNING:
                    raise ProjectError(
                        "preparation_state_conflict",
                        "這次素材分析目前不能保存切塊。",
                        status_code=409,
                    )
                item = connection.execute(
                    "SELECT status FROM preparation_items WHERE run_id = ? "
                    "AND version_id = ? AND stream_index = ?",
                    (run_id, version_id, stream_index),
                ).fetchone()
                if item is None or item["status"] != PreparationItemStatus.PREPARED.value:
                    raise ProjectError(
                        "preparation_item_conflict",
                        "這個分析項目尚未準備好。",
                        status_code=409,
                    )
                existing = connection.execute(
                    "SELECT ordinal, start_sample, end_sample, source_span_json, status, "
                    "transcript_artifact_id, result_json FROM preparation_chunks "
                    "WHERE run_id = ? AND version_id = ? AND stream_index = ? "
                    "ORDER BY ordinal",
                    (run_id, version_id, stream_index),
                ).fetchall()
                if existing:
                    observed = tuple(
                        (row["start_sample"], row["end_sample"], row["source_span_json"])
                        for row in existing
                    )
                    requested = tuple(
                        (
                            chunk.start_sample,
                            chunk.end_sample,
                            json.dumps(
                                chunk.source_span.to_storage(),
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                        )
                        for chunk in chunks
                    )
                    if observed != requested:
                        raise ProjectError(
                            "chunk_recipe_changed",
                            "音訊切塊方式已改變，無法沿用先前進度。",
                            status_code=409,
                        )
                else:
                    for ordinal, chunk in enumerate(chunks):
                        connection.execute(
                            "INSERT INTO preparation_chunks "
                            "(run_id, version_id, stream_index, ordinal, start_sample, "
                            "end_sample, source_span_json, status) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')",
                            (
                                run_id,
                                version_id,
                                stream_index,
                                ordinal,
                                chunk.start_sample,
                                chunk.end_sample,
                                json.dumps(
                                    chunk.source_span.to_storage(),
                                    sort_keys=True,
                                    separators=(",", ":"),
                                ),
                            ),
                        )
                    existing = connection.execute(
                        "SELECT ordinal, start_sample, end_sample, source_span_json, status, "
                        "transcript_artifact_id, result_json FROM preparation_chunks "
                        "WHERE run_id = ? AND version_id = ? AND stream_index = ? "
                        "ORDER BY ordinal",
                        (run_id, version_id, stream_index),
                    ).fetchall()
                result = tuple(self._read_chunk(row) for row in existing)
                connection.commit()
                return result
            except ProjectError:
                connection.rollback()
                raise
            except sqlite3.Error as error:
                connection.rollback()
                raise ProjectError(
                    "storage_failed", "Clipen 無法保存音訊切塊。", status_code=500
                ) from error

    def settle_chunk(
        self,
        project_id: str,
        run_id: str,
        version_id: str,
        stream_index: int,
        ordinal: int,
        artifact_id: str,
        result: TranscriptionResult,
    ) -> PreparationChunk:
        status = (
            PreparationChunkStatus.NO_SPEECH
            if result.no_speech_detected
            else PreparationChunkStatus.TRANSCRIBED
        )
        result_json = _encoded_json(result.to_storage(), "transcript result", 32 * 1024 * 1024)
        with self.projects.connect(project_id) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                run = self._read_run(connection, run_id)
                if run.project_id != project_id or run.state is not PreparationState.RUNNING:
                    raise ProjectError(
                        "preparation_state_conflict",
                        "這次素材分析目前不能保存逐字稿。",
                        status_code=409,
                    )
                row = connection.execute(
                    "SELECT ordinal, start_sample, end_sample, source_span_json, status, "
                    "transcript_artifact_id, result_json FROM preparation_chunks "
                    "WHERE run_id = ? AND version_id = ? AND stream_index = ? AND ordinal = ?",
                    (run_id, version_id, stream_index, ordinal),
                ).fetchone()
                if row is None:
                    raise ProjectError(
                        "preparation_chunk_not_found", "找不到這個音訊切塊。", status_code=404
                    )
                if row["status"] != PreparationChunkStatus.PENDING.value:
                    completed = self._read_chunk(row)
                    if completed.transcript_artifact_id != artifact_id or completed.result != result:
                        raise ProjectError(
                            "preparation_chunk_conflict",
                            "這個音訊切塊已保存不同結果。",
                            status_code=409,
                        )
                    connection.rollback()
                    return completed
                connection.execute(
                    "UPDATE preparation_chunks SET status = ?, transcript_artifact_id = ?, "
                    "result_json = ? WHERE run_id = ? AND version_id = ? "
                    "AND stream_index = ? AND ordinal = ?",
                    (
                        status.value,
                        artifact_id,
                        result_json,
                        run_id,
                        version_id,
                        stream_index,
                        ordinal,
                    ),
                )
                stored = connection.execute(
                    "SELECT ordinal, start_sample, end_sample, source_span_json, status, "
                    "transcript_artifact_id, result_json FROM preparation_chunks "
                    "WHERE run_id = ? AND version_id = ? AND stream_index = ? AND ordinal = ?",
                    (run_id, version_id, stream_index, ordinal),
                ).fetchone()
                chunk = self._read_chunk(stored)
                connection.commit()
                return chunk
            except ProjectError:
                connection.rollback()
                raise
            except sqlite3.Error as error:
                connection.rollback()
                raise ProjectError(
                    "storage_failed", "Clipen 無法保存逐字稿切塊。", status_code=500
                ) from error

    def publish_transcript_revision(
        self,
        project_id: str,
        run_id: str,
        version_id: str,
        stream_index: int,
        *,
        artifact_id: str,
        language: str,
        model: Mapping[str, object],
        recipe_id: str,
        source_span: SourceSpan,
        segments: tuple[TimelineSegment, ...],
    ) -> str:
        if not re.fullmatch(r"[a-z]{2,3}(?:-[A-Z]{2})?", language):
            raise ValueError("transcript language is invalid")
        if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,127}", recipe_id):
            raise ValueError("transcript recipe is invalid")
        if any(
            segment.source_span.version_id != version_id
            or segment.source_span.stream_index != stream_index
            for segment in segments
        ):
            raise ValueError("timeline segment belongs to another source")
        model_json = _bounded_json(model, "ASR model")
        source_json = json.dumps(
            source_span.to_storage(), sort_keys=True, separators=(",", ":")
        )
        identity = {
            "run_id": run_id,
            "version_id": version_id,
            "stream_index": stream_index,
            "artifact_id": artifact_id,
            "language": language,
            "recipe_id": recipe_id,
        }
        revision_id = "trn_" + hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:32]
        with self.projects.connect(project_id) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                run = self._read_run(connection, run_id)
                if run.project_id != project_id or run.state is not PreparationState.RUNNING:
                    raise ProjectError(
                        "preparation_state_conflict",
                        "這次素材分析目前不能發布逐字稿。",
                        status_code=409,
                    )
                item = connection.execute(
                    "SELECT status, transcript_revision_id FROM preparation_items "
                    "WHERE run_id = ? AND version_id = ? AND stream_index = ?",
                    (run_id, version_id, stream_index),
                ).fetchone()
                if item is None:
                    raise ProjectError(
                        "preparation_item_not_found", "找不到這個分析項目。", status_code=404
                    )
                if item["transcript_revision_id"] is not None:
                    if item["transcript_revision_id"] != revision_id:
                        raise ProjectError(
                            "preparation_item_conflict",
                            "這個分析項目已發布不同逐字稿。",
                            status_code=409,
                        )
                    connection.rollback()
                    return revision_id
                if item["status"] != PreparationItemStatus.PREPARED.value:
                    raise ProjectError(
                        "preparation_item_conflict",
                        "這個分析項目尚未準備好。",
                        status_code=409,
                    )
                chunk_counts = connection.execute(
                    "SELECT COUNT(*) AS total, SUM(CASE WHEN status != 'pending' "
                    "THEN 1 ELSE 0 END) AS complete FROM preparation_chunks "
                    "WHERE run_id = ? AND version_id = ? AND stream_index = ?",
                    (run_id, version_id, stream_index),
                ).fetchone()
                if chunk_counts["total"] < 1 or chunk_counts["complete"] != chunk_counts["total"]:
                    raise ProjectError(
                        "preparation_incomplete",
                        "逐字稿仍有尚未完成的音訊切塊。",
                        status_code=409,
                    )
                artifact = connection.execute(
                    "SELECT project_id, kind, producer_operation_id FROM artifacts "
                    "WHERE artifact_id = ?",
                    (artifact_id,),
                ).fetchone()
                if (
                    artifact is None
                    or artifact["project_id"] != project_id
                    or artifact["kind"] != "transcript"
                    or artifact["producer_operation_id"] != run.operation_id
                ):
                    raise ProjectError(
                        "artifact_not_found", "找不到完整逐字稿成果。", status_code=409
                    )
                status = "no_speech_detected" if not segments else "transcribed"
                now = utc_now()
                connection.execute(
                    "INSERT INTO transcription_revisions "
                    "(revision_id, project_id, run_id, version_id, stream_index, "
                    "artifact_id, status, language, model_json, recipe_id, "
                    "source_span_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        revision_id,
                        project_id,
                        run_id,
                        version_id,
                        stream_index,
                        artifact_id,
                        status,
                        language,
                        model_json,
                        recipe_id,
                        source_json,
                        now,
                    ),
                )
                for ordinal, segment in enumerate(segments):
                    segment_identity = (
                        f"{revision_id}\0{ordinal}\0{segment.source_span.start_pts}\0"
                        f"{segment.source_span.end_pts}\0{segment.text}"
                    )
                    segment_id = "seg_" + hashlib.sha256(
                        segment_identity.encode("utf-8")
                    ).hexdigest()[:32]
                    connection.execute(
                        "INSERT INTO transcript_segments "
                        "(revision_id, segment_id, start_pts, end_pts, text, confidence, ordinal) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            revision_id,
                            segment_id,
                            segment.source_span.start_pts,
                            segment.source_span.end_pts,
                            segment.text,
                            segment.confidence,
                            ordinal,
                        ),
                    )
                item_status = (
                    PreparationItemStatus.NO_SPEECH
                    if not segments
                    else PreparationItemStatus.TRANSCRIBED
                )
                connection.execute(
                    "UPDATE preparation_items SET status = ?, transcript_revision_id = ?, "
                    "error_code = NULL WHERE run_id = ? AND version_id = ? AND stream_index = ?",
                    (
                        item_status.value,
                        revision_id,
                        run_id,
                        version_id,
                        stream_index,
                    ),
                )
                self._refresh_counts(connection, run_id)
                self._append_event(
                    connection,
                    project_id,
                    run.operation_id,
                    "progress",
                    {
                        "version_id": version_id,
                        "stream_index": stream_index,
                        "status": item_status.value,
                    },
                    now,
                )
                connection.commit()
                return revision_id
            except ProjectError:
                connection.rollback()
                raise
            except sqlite3.Error as error:
                connection.rollback()
                raise ProjectError(
                    "storage_failed", "Clipen 無法發布逐字稿。", status_code=500
                ) from error

    def transition(
        self,
        project_id: str,
        run_id: str,
        target: PreparationState,
        *,
        phase: str,
        error_code: str | None = None,
    ) -> PreparationRun:
        validate_project_id(project_id)
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", phase):
            raise ValueError("preparation phase is invalid")
        if error_code is not None and not re.fullmatch(
            r"[a-z][a-z0-9_]{0,63}", error_code
        ):
            raise ValueError("preparation error code is invalid")
        if target is PreparationState.SUCCEEDED:
            raise ValueError("use publish() to complete a preparation")
        with self.projects.connect(project_id) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                current = self._read_run(connection, run_id)
                if current.project_id != project_id:
                    raise ProjectError(
                        "preparation_not_found", "找不到這次素材分析。", status_code=404
                    )
                if target not in _TRANSITIONS.get(current.state, set()):
                    raise ProjectError(
                        "preparation_state_conflict",
                        "這次素材分析的狀態已經改變。",
                        status_code=409,
                    )
                now = utc_now()
                started = current.started_at or now
                completed = now if target in _TERMINAL_STATES else None
                connection.execute(
                    "UPDATE preparation_runs SET state = ?, phase = ?, error_code = ?, "
                    "started_at = ?, completed_at = ? WHERE run_id = ?",
                    (target.value, phase, error_code, started, completed, run_id),
                )
                self._append_event(
                    connection,
                    project_id,
                    current.operation_id,
                    target.value,
                    {"phase": phase, "error_code": error_code},
                    now,
                )
                updated = self._read_run(connection, run_id)
                connection.commit()
                return updated
            except ProjectError:
                connection.rollback()
                raise
            except sqlite3.Error as error:
                connection.rollback()
                raise ProjectError(
                    "storage_failed", "Clipen 無法更新素材分析。", status_code=500
                ) from error

    def request_cancel(self, project_id: str, run_id: str) -> PreparationRun:
        validate_project_id(project_id)
        with self.projects.connect(project_id) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                current = self._read_run(connection, run_id)
                if current.project_id != project_id:
                    raise ProjectError(
                        "preparation_not_found", "找不到這次素材分析。", status_code=404
                    )
                if current.state in _TERMINAL_STATES or current.state is PreparationState.CANCELLING:
                    connection.rollback()
                    return current
                now = utc_now()
                if current.state is PreparationState.QUEUED:
                    connection.execute(
                        "UPDATE preparation_runs SET state = 'cancelled', phase = 'cancelled', "
                        "started_at = ?, completed_at = ? WHERE run_id = ?",
                        (now, now, run_id),
                    )
                    event_type = "cancelled"
                else:
                    connection.execute(
                        "UPDATE preparation_runs SET state = 'cancelling', "
                        "phase = 'cancelling' WHERE run_id = ?",
                        (run_id,),
                    )
                    event_type = "cancel_requested"
                self._append_event(
                    connection,
                    project_id,
                    current.operation_id,
                    event_type,
                    {"phase": event_type},
                    now,
                )
                updated = self._read_run(connection, run_id)
                connection.commit()
                return updated
            except ProjectError:
                connection.rollback()
                raise
            except sqlite3.Error as error:
                connection.rollback()
                raise ProjectError(
                    "storage_failed", "Clipen 無法取消素材分析。", status_code=500
                ) from error

    def resume_interrupted(
        self, project_id: str, run_id: str, request_id: object
    ) -> tuple[PreparationRun, bool]:
        validate_project_id(project_id)
        normalized_request = validate_request_id(request_id)
        request_hash = command_digest({"run_id": run_id})
        with self.projects.connect(project_id) as connection:
            receipt = connection.execute(
                "SELECT command_type, request_hash, run_id "
                "FROM preparation_command_receipts WHERE request_id = ?",
                (normalized_request,),
            ).fetchone()
            if receipt is not None:
                if (
                    receipt["command_type"] != "resume_preparation"
                    or receipt["request_hash"] != request_hash
                ):
                    raise ProjectError(
                        "request_reused",
                        "這次操作的識別資訊已用於不同內容。",
                        status_code=409,
                    )
                return self._read_run(connection, receipt["run_id"]), False
        scope = self.preflight(project_id)
        with self.projects.connect(project_id) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                receipt = connection.execute(
                    "SELECT command_type, request_hash, run_id "
                    "FROM preparation_command_receipts WHERE request_id = ?",
                    (normalized_request,),
                ).fetchone()
                if receipt is not None:
                    if (
                        receipt["command_type"] != "resume_preparation"
                        or receipt["request_hash"] != request_hash
                    ):
                        raise ProjectError(
                            "request_reused",
                            "這次操作的識別資訊已用於不同內容。",
                            status_code=409,
                        )
                    replay = self._read_run(connection, receipt["run_id"])
                    connection.rollback()
                    return replay, False
                current = self._read_run(connection, run_id)
                if current.project_id != project_id:
                    raise ProjectError(
                        "preparation_not_found", "找不到這次素材分析。", status_code=404
                    )
                if current.state is not PreparationState.INTERRUPTED:
                    raise ProjectError(
                        "preparation_state_conflict",
                        "只有被中斷的素材分析可以接續。",
                        status_code=409,
                    )
                live = connection.execute(
                    "SELECT p.revision, c.run_id AS inventory_run_id FROM project p "
                    "LEFT JOIN current_inventory c ON c.slot = 1 "
                    "WHERE p.project_id = ?",
                    (project_id,),
                ).fetchone()
                if (
                    scope.scope_id != current.scope_id
                    or scope.inventory_run_id != current.inventory_run_id
                    or live is None
                    or live["revision"] != scope.project_revision
                    or live["inventory_run_id"] != scope.inventory_run_id
                ):
                    raise ProjectError(
                        "scope_changed",
                        "素材或作品內容已改變，無法接續這次分析。",
                        status_code=409,
                    )
                busy = connection.execute(
                    "SELECT run_id FROM preparation_runs WHERE run_id != ? "
                    "AND state IN ('queued', 'running', 'cancelling') LIMIT 1",
                    (run_id,),
                ).fetchone()
                if busy is not None:
                    raise ProjectError(
                        "preparation_busy",
                        "已有一個素材分析正在進行。",
                        status_code=409,
                    )
                attempt_number = current.attempt_number + 1
                attempt_id = _derived_id(
                    "att", current.operation_id, str(attempt_number)
                )
                now = utc_now()
                connection.execute(
                    "UPDATE preparation_runs SET attempt_id = ?, attempt_number = ?, "
                    "state = 'queued', phase = 'preparing', error_code = NULL, "
                    "started_at = NULL, completed_at = NULL WHERE run_id = ?",
                    (attempt_id, attempt_number, run_id),
                )
                connection.execute(
                    "INSERT INTO preparation_command_receipts "
                    "(request_id, command_type, request_hash, run_id, created_at) "
                    "VALUES (?, 'resume_preparation', ?, ?, ?)",
                    (normalized_request, request_hash, run_id, now),
                )
                self._append_event(
                    connection,
                    project_id,
                    current.operation_id,
                    "queued",
                    {"phase": "preparing", "attempt_number": attempt_number},
                    now,
                )
                resumed = self._read_run(connection, run_id)
                connection.commit()
                return resumed, True
            except ProjectError:
                connection.rollback()
                raise
            except sqlite3.Error as error:
                connection.rollback()
                raise ProjectError(
                    "storage_failed", "Clipen 無法接續素材分析。", status_code=500
                ) from error

    def settle_item(
        self,
        project_id: str,
        run_id: str,
        version_id: str,
        stream_index: int,
        status: PreparationItemStatus,
        *,
        audio_artifact_id: str | None = None,
        transcript_revision_id: str | None = None,
        error_code: str | None = None,
        checkpoint: Mapping[str, object] | None = None,
    ) -> PreparationRun:
        validate_project_id(project_id)
        if status not in {
            PreparationItemStatus.PREPARED,
            PreparationItemStatus.TRANSCRIBED,
            PreparationItemStatus.NO_SPEECH,
            PreparationItemStatus.FAILED,
        }:
            raise ValueError("item cannot settle to the requested status")
        if status is PreparationItemStatus.PREPARED and audio_artifact_id is None:
            raise ValueError("prepared item requires an audio artifact")
        if status in {PreparationItemStatus.TRANSCRIBED, PreparationItemStatus.NO_SPEECH}:
            if transcript_revision_id is None:
                raise ValueError("transcribed item requires a transcript revision")
        if status is PreparationItemStatus.FAILED and error_code is None:
            raise ValueError("failed item requires an error code")
        if error_code is not None and not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", error_code):
            raise ValueError("preparation item error code is invalid")
        checkpoint_json = None if checkpoint is None else _bounded_json(checkpoint, "checkpoint")
        with self.projects.connect(project_id) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                run = self._read_run(connection, run_id)
                if run.project_id != project_id or run.state is not PreparationState.RUNNING:
                    raise ProjectError(
                        "preparation_state_conflict",
                        "這次素材分析目前不能保存結果。",
                        status_code=409,
                    )
                item = connection.execute(
                    "SELECT status, audio_artifact_id FROM preparation_items "
                    "WHERE run_id = ? AND version_id = ? AND stream_index = ?",
                    (run_id, version_id, stream_index),
                ).fetchone()
                if item is None:
                    raise ProjectError(
                        "preparation_item_not_found", "找不到這個分析項目。", status_code=404
                    )
                current_status = PreparationItemStatus(item["status"])
                allowed = (
                    current_status is PreparationItemStatus.PENDING
                    and status in {PreparationItemStatus.PREPARED, PreparationItemStatus.FAILED}
                ) or (
                    current_status is PreparationItemStatus.PREPARED
                    and status
                    in {
                        PreparationItemStatus.TRANSCRIBED,
                        PreparationItemStatus.NO_SPEECH,
                        PreparationItemStatus.FAILED,
                    }
                )
                if not allowed:
                    raise ProjectError(
                        "preparation_item_conflict",
                        "這個分析項目的狀態已經改變。",
                        status_code=409,
                    )
                retained_audio = audio_artifact_id or item["audio_artifact_id"]
                connection.execute(
                    "UPDATE preparation_items SET status = ?, error_code = ?, "
                    "checkpoint_json = ?, audio_artifact_id = ?, transcript_revision_id = ? "
                    "WHERE run_id = ? AND version_id = ? AND stream_index = ?",
                    (
                        status.value,
                        error_code,
                        checkpoint_json,
                        retained_audio,
                        transcript_revision_id,
                        run_id,
                        version_id,
                        stream_index,
                    ),
                )
                self._refresh_counts(connection, run_id)
                now = utc_now()
                self._append_event(
                    connection,
                    project_id,
                    run.operation_id,
                    "progress",
                    {
                        "version_id": version_id,
                        "stream_index": stream_index,
                        "status": status.value,
                        "error_code": error_code,
                    },
                    now,
                )
                updated = self._read_run(connection, run_id)
                connection.commit()
                return updated
            except ProjectError:
                connection.rollback()
                raise
            except sqlite3.Error as error:
                connection.rollback()
                raise ProjectError(
                    "storage_failed", "Clipen 無法保存分析進度。", status_code=500
                ) from error

    def publish(self, project_id: str, run_id: str) -> PreparationRun:
        validate_project_id(project_id)
        with self.projects.connect(project_id) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                run = self._read_run(connection, run_id)
                if run.project_id != project_id or run.state is not PreparationState.RUNNING:
                    raise ProjectError(
                        "preparation_state_conflict",
                        "這次素材分析目前不能完成。",
                        status_code=409,
                    )
                incomplete = connection.execute(
                    "SELECT COUNT(*) FROM preparation_items WHERE run_id = ? "
                    "AND status IN ('pending', 'prepared')",
                    (run_id,),
                ).fetchone()[0]
                if incomplete:
                    raise ProjectError(
                        "preparation_incomplete",
                        "素材分析仍有尚未完成的項目。",
                        status_code=409,
                    )
                self._refresh_counts(connection, run_id)
                now = utc_now()
                connection.execute(
                    "UPDATE preparation_runs SET state = 'succeeded', phase = 'completed', "
                    "error_code = NULL, completed_at = ? WHERE run_id = ?",
                    (now, run_id),
                )
                connection.execute(
                    "INSERT INTO current_preparation (slot, run_id) VALUES (1, ?) "
                    "ON CONFLICT(slot) DO UPDATE SET run_id = excluded.run_id",
                    (run_id,),
                )
                self._append_event(
                    connection,
                    project_id,
                    run.operation_id,
                    "succeeded",
                    {"phase": "completed"},
                    now,
                )
                published = self._read_run(connection, run_id)
                connection.commit()
                return published
            except ProjectError:
                connection.rollback()
                raise
            except sqlite3.Error as error:
                connection.rollback()
                raise ProjectError(
                    "storage_failed", "Clipen 無法完成素材分析。", status_code=500
                ) from error

    def interrupt_incomplete(self, project_id: str) -> int:
        validate_project_id(project_id)
        with self.projects.connect(project_id) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                rows = connection.execute(
                    "SELECT run_id, operation_id, started_at FROM preparation_runs "
                    "WHERE state IN ('queued', 'running', 'cancelling')"
                ).fetchall()
                now = utc_now()
                for row in rows:
                    connection.execute(
                        "UPDATE preparation_runs SET state = 'interrupted', "
                        "phase = 'interrupted', error_code = 'controller_restarted', "
                        "started_at = ?, completed_at = ? WHERE run_id = ?",
                        (row["started_at"] or now, now, row["run_id"]),
                    )
                    self._append_event(
                        connection,
                        project_id,
                        row["operation_id"],
                        "interrupted",
                        {"phase": "interrupted", "error_code": "controller_restarted"},
                        now,
                    )
                connection.commit()
                return len(rows)
            except sqlite3.Error as error:
                connection.rollback()
                raise ProjectError(
                    "storage_failed", "Clipen 無法恢復素材分析狀態。", status_code=500
                ) from error

    @staticmethod
    def _refresh_counts(connection: sqlite3.Connection, run_id: str) -> None:
        counts = connection.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN status IN ('prepared', 'transcribed', 'no_speech') "
            "THEN 1 ELSE 0 END) AS prepared, "
            "SUM(CASE WHEN status = 'transcribed' THEN 1 ELSE 0 END) AS transcribed, "
            "SUM(CASE WHEN status = 'no_audio' THEN 1 ELSE 0 END) AS no_audio, "
            "SUM(CASE WHEN status = 'no_speech' THEN 1 ELSE 0 END) AS no_speech, "
            "SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS issues "
            "FROM preparation_items WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        connection.execute(
            "UPDATE preparation_runs SET total_count = ?, prepared_count = ?, "
            "transcribed_count = ?, no_audio_count = ?, no_speech_count = ?, "
            "issue_count = ? WHERE run_id = ?",
            (
                counts["total"],
                counts["prepared"] or 0,
                counts["transcribed"] or 0,
                counts["no_audio"] or 0,
                counts["no_speech"] or 0,
                counts["issues"] or 0,
                run_id,
            ),
        )

    @staticmethod
    def _append_event(
        connection: sqlite3.Connection,
        project_id: str,
        operation_id: str,
        event_type: str,
        payload: Mapping[str, object],
        created_at: str,
    ) -> None:
        seed = json.dumps(
            {
                "operation": operation_id,
                "type": event_type,
                "payload": payload,
                "created_at": created_at,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        event_id = "evt_" + hashlib.sha256(seed).hexdigest()[:32]
        connection.execute(
            "INSERT INTO operation_events "
            "(event_id, project_id, operation_id, event_type, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                event_id,
                project_id,
                operation_id,
                event_type,
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                created_at,
            ),
        )

    @staticmethod
    def _read_run(connection: sqlite3.Connection, run_id: object) -> PreparationRun:
        if not isinstance(run_id, str) or re.fullmatch(r"prep_[0-9a-f]{32}", run_id) is None:
            raise ProjectError("preparation_not_found", "找不到這次素材分析。", status_code=404)
        row = connection.execute(
            "SELECT run_id, project_id, project_revision, inventory_run_id, scope_id, "
            "operation_id, attempt_id, attempt_number, input_fingerprint, state, phase, "
            "total_count, prepared_count, transcribed_count, no_audio_count, "
            "no_speech_count, issue_count, error_code, created_at, started_at, completed_at "
            "FROM preparation_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise ProjectError("preparation_not_found", "找不到這次素材分析。", status_code=404)
        try:
            return PreparationRun(
                row["run_id"],
                row["project_id"],
                row["project_revision"],
                row["inventory_run_id"],
                row["scope_id"],
                row["operation_id"],
                row["attempt_id"],
                row["attempt_number"],
                row["input_fingerprint"],
                PreparationState(row["state"]),
                row["phase"],
                PreparationCounts(
                    row["total_count"],
                    row["prepared_count"],
                    row["transcribed_count"],
                    row["no_audio_count"],
                    row["no_speech_count"],
                    row["issue_count"],
                ),
                row["error_code"],
                row["created_at"],
                row["started_at"],
                row["completed_at"],
            )
        except (TypeError, ValueError) as error:
            raise ProjectError(
                "storage_corrupt", "Clipen 無法讀取素材分析。", status_code=500
            ) from error

    @staticmethod
    def _read_chunk(row: sqlite3.Row) -> PreparationChunk:
        try:
            raw_span = json.loads(row["source_span_json"])
            raw_result = (
                None if row["result_json"] is None else json.loads(row["result_json"])
            )
            return PreparationChunk(
                row["ordinal"],
                row["start_sample"],
                row["end_sample"],
                SourceSpan.from_storage(raw_span),
                PreparationChunkStatus(row["status"]),
                row["transcript_artifact_id"],
                None
                if raw_result is None
                else TranscriptionResult.from_storage(raw_result),
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            raise ProjectError(
                "storage_corrupt", "Clipen 無法讀取逐字稿切塊。", status_code=500
            ) from error


def _source_span(version_id: str, stream_index: int, details: Mapping[str, Any]) -> SourceSpan:
    time_base = details["time_base"]
    start_pts = details["start_pts"]
    duration = details["duration_ts"]
    if not isinstance(time_base, str) or "/" not in time_base:
        raise ValueError("audio time base is missing")
    numerator, denominator = (int(part) for part in time_base.split("/", 1))
    start = int(str(start_pts))
    end = start + int(str(duration))
    return SourceSpan(
        version_id,
        stream_index,
        str(start),
        str(end),
        Rational(numerator, denominator),
    )


def _derived_id(prefix: str, *values: str) -> str:
    digest = hashlib.sha256("\0".join((prefix, *values)).encode()).hexdigest()[:32]
    return f"{prefix}_{digest}"


def _bounded_json(value: Mapping[str, object], label: str) -> str:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return _encoded_json(value, label, 64 * 1024)


def _encoded_json(value: object, label: str, maximum_bytes: int) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} is invalid") from error
    if len(encoded.encode("utf-8")) > maximum_bytes:
        raise ValueError(f"{label} is too large")
    return encoded


def math_ceil_fraction(value: Fraction) -> int:
    return -(-value.numerator // value.denominator)


def _fraction_decimal(value: Fraction) -> str:
    if value.denominator == 1:
        return str(value.numerator)
    return format(value.numerator / value.denominator, ".6f").rstrip("0").rstrip(".")
