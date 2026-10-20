"""Crash-resilient SQLite storage rooted entirely in Clipen-managed directories."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import uuid
from typing import Iterator, Mapping, Any

from clipen.filesystem import restrict_permissions

from .models import (
    DirectoryReference,
    DirectoryRole,
    ProjectError,
    ProjectSnapshot,
    validate_brief,
    validate_name,
    validate_project_id,
    validate_request_id,
    validate_revision,
)
from .evidence_schema import EVIDENCE_SCHEMA, OBSERVATION_SCHEMA
from .validation_schema import VALIDATION_WORKFLOW_SCHEMA
from .planning_schema import PLANNING_SCHEMA
from .timeline_schema import TIMELINE_SCHEMA
from .preview_schema import PREVIEW_SCHEMA
from .finishing_schema import FINISHING_SCHEMA, FINISHING_RUNS_SCHEMA
from .annotation_schema import ANNOTATION_SCHEMA
from .agent_schema import EDIT_AGENT_SCHEMA, PRODUCTION_SESSION_SCHEMA
from .deletion import ProjectDeletion


PROJECT_SCHEMA_VERSION = 18
_PROJECT_DIRECTORY = re.compile(r"prj_[0-9a-f]{32}\Z")

_INVENTORY_SCHEMA = """
CREATE TABLE inventory_runs (
    run_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES project(project_id),
    request_id TEXT NOT NULL UNIQUE,
    project_revision INTEGER NOT NULL CHECK (project_revision >= 1),
    input_fingerprint TEXT NOT NULL,
    roots_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN ('queued', 'running', 'cancelling', 'succeeded', 'failed',
                  'cancelled', 'interrupted')
    ),
    phase TEXT NOT NULL,
    discovered_count INTEGER NOT NULL DEFAULT 0 CHECK (discovered_count >= 0),
    probed_count INTEGER NOT NULL DEFAULT 0 CHECK (probed_count >= 0),
    usable_count INTEGER NOT NULL DEFAULT 0 CHECK (usable_count >= 0),
    issue_count INTEGER NOT NULL DEFAULT 0 CHECK (issue_count >= 0),
    ignored_count INTEGER NOT NULL DEFAULT 0 CHECK (ignored_count >= 0),
    error_code TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT
);
CREATE TABLE inventory_command_receipts (
    request_id TEXT PRIMARY KEY,
    command_type TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    run_id TEXT NOT NULL REFERENCES inventory_runs(run_id),
    created_at TEXT NOT NULL
);
CREATE TABLE assets (
    asset_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES project(project_id),
    role TEXT NOT NULL CHECK (role IN ('source', 'reference')),
    relative_path TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    UNIQUE(project_id, role, relative_path)
);
CREATE TABLE asset_versions (
    version_id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL REFERENCES assets(asset_id),
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    mtime_ns INTEGER NOT NULL CHECK (mtime_ns >= 0),
    fingerprint TEXT NOT NULL,
    probe_status TEXT NOT NULL CHECK (
        probe_status IN ('pending', 'ready', 'unsupported', 'unreadable',
                         'timeout', 'changed', 'internal_error')
    ),
    probe_json TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE inventory_items (
    run_id TEXT NOT NULL REFERENCES inventory_runs(run_id) ON DELETE CASCADE,
    asset_id TEXT NOT NULL REFERENCES assets(asset_id),
    version_id TEXT REFERENCES asset_versions(version_id),
    display_name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('video', 'audio', 'image', 'unknown')),
    status TEXT NOT NULL CHECK (status IN ('usable', 'needs_attention', 'ignored')),
    issue_code TEXT,
    PRIMARY KEY(run_id, asset_id)
);
CREATE TABLE media_streams (
    version_id TEXT NOT NULL REFERENCES asset_versions(version_id) ON DELETE CASCADE,
    stream_index INTEGER NOT NULL CHECK (stream_index >= 0),
    kind TEXT NOT NULL CHECK (kind IN ('video', 'audio')),
    codec TEXT NOT NULL,
    details_json TEXT NOT NULL,
    PRIMARY KEY(version_id, stream_index)
);
CREATE TABLE current_inventory (
    slot INTEGER PRIMARY KEY CHECK (slot = 1),
    run_id TEXT NOT NULL REFERENCES inventory_runs(run_id)
);
CREATE INDEX inventory_runs_project_created
    ON inventory_runs(project_id, created_at DESC);
CREATE INDEX inventory_items_run_status
    ON inventory_items(run_id, status, display_name);
"""

_ANALYSIS_SCHEMA = """
CREATE TABLE preparation_runs (
    run_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES project(project_id),
    request_id TEXT NOT NULL UNIQUE,
    project_revision INTEGER NOT NULL CHECK (project_revision >= 1),
    inventory_run_id TEXT NOT NULL REFERENCES inventory_runs(run_id),
    scope_id TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    attempt_number INTEGER NOT NULL DEFAULT 1 CHECK (attempt_number >= 1),
    input_fingerprint TEXT NOT NULL,
    recipe_json TEXT NOT NULL,
    model_json TEXT,
    state TEXT NOT NULL CHECK (
        state IN ('queued', 'running', 'cancelling', 'succeeded', 'failed',
                  'cancelled', 'interrupted')
    ),
    phase TEXT NOT NULL,
    total_count INTEGER NOT NULL DEFAULT 0 CHECK (total_count >= 0),
    prepared_count INTEGER NOT NULL DEFAULT 0 CHECK (prepared_count >= 0),
    transcribed_count INTEGER NOT NULL DEFAULT 0 CHECK (transcribed_count >= 0),
    no_audio_count INTEGER NOT NULL DEFAULT 0 CHECK (no_audio_count >= 0),
    no_speech_count INTEGER NOT NULL DEFAULT 0 CHECK (no_speech_count >= 0),
    issue_count INTEGER NOT NULL DEFAULT 0 CHECK (issue_count >= 0),
    error_code TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT
);
CREATE TABLE preparation_command_receipts (
    request_id TEXT PRIMARY KEY,
    command_type TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    run_id TEXT NOT NULL REFERENCES preparation_runs(run_id),
    created_at TEXT NOT NULL
);
CREATE TABLE preparation_items (
    run_id TEXT NOT NULL REFERENCES preparation_runs(run_id) ON DELETE CASCADE,
    version_id TEXT NOT NULL REFERENCES asset_versions(version_id),
    stream_index INTEGER NOT NULL CHECK (stream_index >= -1),
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'prepared', 'transcribed', 'no_audio',
                   'no_speech', 'failed')
    ),
    error_code TEXT,
    checkpoint_json TEXT,
    audio_artifact_id TEXT REFERENCES artifacts(artifact_id),
    transcript_revision_id TEXT REFERENCES transcription_revisions(revision_id),
    review_required INTEGER NOT NULL DEFAULT 0 CHECK (review_required IN (0, 1)),
    PRIMARY KEY(run_id, version_id, stream_index)
);
CREATE TABLE operation_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    project_id TEXT NOT NULL REFERENCES project(project_id),
    operation_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE artifacts (
    artifact_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES project(project_id),
    kind TEXT NOT NULL CHECK (
        kind IN ('analysis_audio', 'transcript', 'coverage_manifest', 'checkpoint')
    ),
    relative_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    producer_operation_id TEXT NOT NULL,
    producer_attempt_id TEXT NOT NULL,
    recipe_id TEXT NOT NULL,
    source_span_json TEXT,
    lineage_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(project_id, relative_path, artifact_id)
);
CREATE TABLE preparation_chunks (
    run_id TEXT NOT NULL,
    version_id TEXT NOT NULL,
    stream_index INTEGER NOT NULL CHECK (stream_index >= 0),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    start_sample INTEGER NOT NULL CHECK (start_sample >= 0),
    end_sample INTEGER NOT NULL CHECK (end_sample > start_sample),
    source_span_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'transcribed', 'no_speech')),
    transcript_artifact_id TEXT REFERENCES artifacts(artifact_id),
    result_json TEXT,
    PRIMARY KEY(run_id, version_id, stream_index, ordinal),
    FOREIGN KEY(run_id, version_id, stream_index)
        REFERENCES preparation_items(run_id, version_id, stream_index)
        ON DELETE CASCADE
);
CREATE TABLE transcription_revisions (
    revision_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES project(project_id),
    run_id TEXT NOT NULL REFERENCES preparation_runs(run_id),
    version_id TEXT NOT NULL REFERENCES asset_versions(version_id),
    stream_index INTEGER NOT NULL CHECK (stream_index >= 0),
    artifact_id TEXT REFERENCES artifacts(artifact_id),
    parent_revision_id TEXT REFERENCES transcription_revisions(revision_id),
    status TEXT NOT NULL CHECK (
        status IN ('transcribed', 'no_speech_detected', 'failed', 'needs_review')
    ),
    language TEXT,
    model_json TEXT NOT NULL,
    recipe_id TEXT NOT NULL,
    source_span_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE transcript_segments (
    revision_id TEXT NOT NULL REFERENCES transcription_revisions(revision_id)
        ON DELETE CASCADE,
    segment_id TEXT NOT NULL,
    start_pts TEXT NOT NULL,
    end_pts TEXT NOT NULL,
    text TEXT NOT NULL,
    speaker_id TEXT,
    confidence TEXT,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    PRIMARY KEY(revision_id, segment_id),
    UNIQUE(revision_id, ordinal)
);
CREATE TABLE current_preparation (
    slot INTEGER PRIMARY KEY CHECK (slot = 1),
    run_id TEXT NOT NULL REFERENCES preparation_runs(run_id)
);
CREATE INDEX preparation_runs_project_created
    ON preparation_runs(project_id, created_at DESC);
CREATE UNIQUE INDEX preparation_runs_operation
    ON preparation_runs(operation_id);
CREATE UNIQUE INDEX preparation_runs_attempt
    ON preparation_runs(attempt_id);
CREATE INDEX preparation_items_run_status
    ON preparation_items(run_id, status, version_id);
CREATE INDEX operation_events_project_sequence
    ON operation_events(project_id, sequence);
CREATE INDEX artifacts_project_kind
    ON artifacts(project_id, kind, created_at);
CREATE INDEX preparation_chunks_run_status
    ON preparation_chunks(run_id, status, version_id, stream_index, ordinal);
CREATE INDEX transcription_revisions_version_created
    ON transcription_revisions(version_id, created_at DESC);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def command_digest(command: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        command,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class ProjectRepository:
    """One SQLite database per project, discovered only below a managed root."""

    def __init__(self, projects_root: Path) -> None:
        projects_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if projects_root.is_symlink():
            raise ProjectError("storage_unsafe", "Clipen 作品儲存位置不安全。", status_code=500)
        self.root = projects_root.resolve(strict=True)
        if not self.root.is_dir():
            raise ProjectError("storage_unavailable", "Clipen 無法使用作品儲存位置。", status_code=500)
        restrict_permissions(self.root, 0o700)
        self._deletion = ProjectDeletion(self)

    def create_project(self, request_id: object, name: object) -> ProjectSnapshot:
        normalized_request = validate_request_id(request_id)
        normalized_name = validate_name(name)
        command_hash = command_digest({"name": normalized_name})
        project_id = "prj_" + hashlib.sha256(
            f"clipen:create-project:v1:{normalized_request}".encode("utf-8")
        ).hexdigest()[:32]
        project_dir = self._project_dir(project_id)

        if project_dir.exists():
            replay = self.replay_receipt(
                project_id, normalized_request, "create_project", command_hash
            )
            if replay is None:
                raise ProjectError(
                    "request_conflict",
                    "這次建立作品的操作與既有資料衝突。",
                    status_code=409,
                )
            return replay

        staging = self.root / f".staging-{project_id}-{uuid.uuid4().hex}"
        staging.mkdir(mode=0o700)
        try:
            snapshot = self._initialize_project(
                staging / "project.sqlite3",
                project_id,
                normalized_name,
                normalized_request,
                command_hash,
            )
            try:
                staging.rename(project_dir)
            except FileExistsError:
                self._remove_staging(staging)
                replay = self.replay_receipt(
                    project_id, normalized_request, "create_project", command_hash
                )
                if replay is None:
                    raise ProjectError(
                        "request_conflict",
                        "這次建立作品的操作與既有資料衝突。",
                        status_code=409,
                    )
                return replay
            return snapshot
        except Exception:
            if staging.exists() and not staging.is_symlink():
                self._remove_staging(staging)
            raise

    def list_projects(self) -> tuple[ProjectSnapshot, ...]:
        projects: list[ProjectSnapshot] = []
        try:
            entries = tuple(self.root.iterdir())
        except OSError as error:
            raise ProjectError(
                "storage_unavailable", "Clipen 無法讀取作品列表。", status_code=500
            ) from error
        for entry in entries:
            if _PROJECT_DIRECTORY.fullmatch(entry.name) is None:
                continue
            if entry.is_symlink() or not entry.is_dir():
                raise ProjectError(
                    "storage_unsafe", "Clipen 偵測到不安全的作品資料。", status_code=500
                )
            projects.append(self.get_project(entry.name))
        projects.sort(key=lambda project: project.updated_at, reverse=True)
        return tuple(projects)

    def get_project(self, project_id: str) -> ProjectSnapshot:
        validate_project_id(project_id)
        with self.connect(project_id) as connection:
            snapshot = self._read_snapshot(connection)
        if snapshot.project_id != project_id:
            raise ProjectError(
                "storage_corrupt", "Clipen 無法讀取作品資料。", status_code=500
            )
        return snapshot

    def replay_receipt(
        self,
        project_id: str,
        request_id: str,
        command_type: str,
        request_hash: str,
    ) -> ProjectSnapshot | None:
        validate_project_id(project_id)
        validate_request_id(request_id)
        with self.connect(project_id) as connection:
            row = connection.execute(
                "SELECT command_type, request_hash, response_json FROM command_receipts "
                "WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        if row is None:
            return None
        if row["command_type"] != command_type or row["request_hash"] != request_hash:
            raise ProjectError(
                "request_reused",
                "這次操作的識別資訊已用於不同內容。",
                status_code=409,
            )
        try:
            payload = json.loads(row["response_json"])
            if not isinstance(payload, dict):
                raise ValueError("receipt response must be an object")
            return ProjectSnapshot.from_storage(payload)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            raise ProjectError(
                "storage_corrupt", "Clipen 無法讀取作品操作紀錄。", status_code=500
            ) from error

    def update_details(
        self,
        project_id: str,
        request_id: object,
        expected_revision: object,
        name: object,
        brief: object,
    ) -> ProjectSnapshot:
        normalized_request = validate_request_id(request_id)
        revision = validate_revision(expected_revision)
        normalized_name = validate_name(name)
        normalized_brief = validate_brief(brief)
        command = {
            "expected_revision": revision,
            "name": normalized_name,
            "brief": normalized_brief,
        }
        return self._mutate(
            project_id,
            normalized_request,
            "update_details",
            command_digest(command),
            revision,
            lambda connection, now: connection.execute(
                "UPDATE project SET name = ?, brief = ?, revision = revision + 1, "
                "updated_at = ? WHERE project_id = ?",
                (normalized_name, normalized_brief, now, project_id),
            ),
        )

    def set_directory(
        self,
        project_id: str,
        request_id: str,
        request_hash: str,
        expected_revision: int,
        reference: DirectoryReference,
    ) -> ProjectSnapshot:
        def mutation(connection: sqlite3.Connection, now: str) -> None:
            connection.execute(
                "INSERT INTO project_directories "
                "(role, canonical_path, display_path, display_name, device, inode, saved_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(role) DO UPDATE SET canonical_path = excluded.canonical_path, "
                "display_path = excluded.display_path, display_name = excluded.display_name, "
                "device = excluded.device, inode = excluded.inode, saved_at = excluded.saved_at",
                (
                    reference.role.value,
                    reference.canonical_path,
                    reference.display_path,
                    reference.display_name,
                    reference.device,
                    reference.inode,
                    reference.saved_at,
                ),
            )
            connection.execute("DELETE FROM current_inventory")

        return self._mutate(
            project_id,
            request_id,
            "set_directory",
            request_hash,
            expected_revision,
            mutation,
        )

    def remove_directory(
        self,
        project_id: str,
        request_id: object,
        expected_revision: object,
        role: DirectoryRole,
    ) -> ProjectSnapshot:
        normalized_request = validate_request_id(request_id)
        revision = validate_revision(expected_revision)
        command_hash = command_digest(
            {"expected_revision": revision, "role": role.value}
        )

        def mutation(connection: sqlite3.Connection, _: str) -> None:
            connection.execute(
                "DELETE FROM project_directories WHERE role = ?", (role.value,)
            )
            connection.execute("DELETE FROM current_inventory")

        return self._mutate(
            project_id,
            normalized_request,
            "remove_directory",
            command_hash,
            revision,
            mutation,
        )

    def delete_project(
        self,
        project_id: str,
        request_id: object,
        expected_revision: object,
        confirmation_name: object,
    ) -> str:
        return self._deletion.delete(
            project_id,
            request_id,
            expected_revision,
            confirmation_name,
        )

    def _mutate(
        self,
        project_id: str,
        request_id: str,
        command_type: str,
        request_hash: str,
        expected_revision: int,
        mutation,
    ) -> ProjectSnapshot:
        validate_project_id(project_id)
        validate_request_id(request_id)
        validate_revision(expected_revision)
        with self.connect(project_id) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                receipt = connection.execute(
                    "SELECT command_type, request_hash, response_json FROM command_receipts "
                    "WHERE request_id = ?",
                    (request_id,),
                ).fetchone()
                if receipt is not None:
                    if (
                        receipt["command_type"] != command_type
                        or receipt["request_hash"] != request_hash
                    ):
                        raise ProjectError(
                            "request_reused",
                            "這次操作的識別資訊已用於不同內容。",
                            status_code=409,
                        )
                    response = json.loads(receipt["response_json"])
                    connection.rollback()
                    return ProjectSnapshot.from_storage(response)

                current = connection.execute(
                    "SELECT revision FROM project WHERE project_id = ?", (project_id,)
                ).fetchone()
                if current is None:
                    raise ProjectError("project_not_found", "找不到這個作品。", status_code=404)
                if current["revision"] != expected_revision:
                    raise ProjectError(
                        "revision_conflict",
                        "作品已在其他分頁更新，請重新載入最新內容。",
                        status_code=409,
                    )
                now = utc_now()
                mutation(connection, now)
                if command_type != "update_details":
                    connection.execute(
                        "UPDATE project SET revision = revision + 1, updated_at = ? "
                        "WHERE project_id = ?",
                        (now, project_id),
                    )
                snapshot = self._read_snapshot(connection)
                response_json = json.dumps(
                    snapshot.to_storage(), ensure_ascii=False, separators=(",", ":")
                )
                connection.execute(
                    "INSERT INTO command_receipts "
                    "(request_id, command_type, request_hash, response_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (request_id, command_type, request_hash, response_json, now),
                )
                connection.commit()
                return snapshot
            except ProjectError:
                connection.rollback()
                raise
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
                connection.rollback()
                raise ProjectError(
                    "storage_corrupt", "Clipen 無法讀取作品資料。", status_code=500
                ) from error
            except sqlite3.Error as error:
                connection.rollback()
                raise ProjectError(
                    "storage_failed", "Clipen 無法保存作品。", status_code=500
                ) from error

    def _initialize_project(
        self,
        database: Path,
        project_id: str,
        name: str,
        request_id: str,
        command_hash: str,
    ) -> ProjectSnapshot:
        now = utc_now()
        connection = sqlite3.connect(database, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            self._configure(connection)
            connection.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE project (
                    project_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    brief TEXT NOT NULL,
                    stage TEXT NOT NULL CHECK (stage = 'materials'),
                    revision INTEGER NOT NULL CHECK (revision >= 1),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE project_directories (
                    role TEXT PRIMARY KEY CHECK (role IN ('source', 'reference')),
                    canonical_path TEXT NOT NULL,
                    display_path TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    device INTEGER NOT NULL,
                    inode INTEGER NOT NULL,
                    saved_at TEXT NOT NULL
                );
                CREATE TABLE command_receipts (
                    request_id TEXT PRIMARY KEY,
                    command_type TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
                + _INVENTORY_SCHEMA
                + _ANALYSIS_SCHEMA
                + EVIDENCE_SCHEMA
                + OBSERVATION_SCHEMA
                + VALIDATION_WORKFLOW_SCHEMA
                + PLANNING_SCHEMA
                + TIMELINE_SCHEMA
                + ANNOTATION_SCHEMA
                + PREVIEW_SCHEMA
                + EDIT_AGENT_SCHEMA
                + PRODUCTION_SESSION_SCHEMA
                + FINISHING_SCHEMA
                + """
                PRAGMA user_version = 18;
                """
            )
            connection.execute(
                "INSERT INTO project "
                "(project_id, name, brief, stage, revision, created_at, updated_at) "
                "VALUES (?, ?, '', 'materials', 1, ?, ?)",
                (project_id, name, now, now),
            )
            snapshot = self._read_snapshot(connection)
            connection.execute(
                "INSERT INTO command_receipts "
                "(request_id, command_type, request_hash, response_json, created_at) "
                "VALUES (?, 'create_project', ?, ?, ?)",
                (
                    request_id,
                    command_hash,
                    json.dumps(
                        snapshot.to_storage(), ensure_ascii=False, separators=(",", ":")
                    ),
                    now,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        restrict_permissions(database, 0o600)
        return snapshot

    @contextmanager
    def connect(self, project_id: str) -> Iterator[sqlite3.Connection]:
        project_dir = self._project_dir(project_id)
        if project_dir.is_symlink() or not project_dir.is_dir():
            raise ProjectError("project_not_found", "找不到這個作品。", status_code=404)
        database = project_dir / "project.sqlite3"
        if database.is_symlink() or not database.is_file():
            raise ProjectError(
                "storage_corrupt", "Clipen 無法讀取作品資料。", status_code=500
            )
        try:
            connection = sqlite3.connect(database, timeout=5, isolation_level=None)
            connection.row_factory = sqlite3.Row
            self._configure(connection)
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 1:
                self._migrate_v1_to_v2(connection)
                version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 2:
                self._migrate_v2_to_v3(connection)
                version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 3:
                self._migrate_v3_to_v4(connection)
                version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 4:
                self._migrate_v4_to_v5(connection)
                version = 5
            if version == 5:
                connection.executescript(
                    "BEGIN IMMEDIATE;\n"
                    + EVIDENCE_SCHEMA
                    + "\nPRAGMA user_version = 6;\nCOMMIT;"
                )
                version = 6
            if version == 6:
                self._migrate_v6_to_v7(connection)
                version = 7
            if version == 7:
                self._migrate_v7_to_v8(connection)
                version = 8
            if version == 8:
                self._migrate_v8_to_v9(connection)
                version = 9
            if version == 9:
                self._migrate_v9_to_v10(connection)
                version = 10
            if version == 10:
                self._migrate_v10_to_v11(connection)
                version = 11
            if version == 11:
                self._migrate_v11_to_v12(connection)
                version = 12
            if version == 12:
                self._migrate_v12_to_v13(connection)
                version = 13
            if version == 13:
                self._migrate_v13_to_v14(connection)
                version = 14
            if version == 14:
                self._migrate_v14_to_v15(connection)
                version = 15
            if version == 15:
                self._migrate_v15_to_v16(connection)
                version = 16
            if version == 16:
                self._migrate_v16_to_v17(connection)
                version = 17
            if version == 17:
                self._migrate_v17_to_v18(connection)
                version = 18
            if version != PROJECT_SCHEMA_VERSION:
                connection.close()
                raise ProjectError(
                    "migration_required",
                    "這個作品需要其他版本的 Clipen。",
                    status_code=409,
                )
            yield connection
        except sqlite3.Error as error:
            raise ProjectError(
                "storage_corrupt", "Clipen 無法讀取作品資料。", status_code=500
            ) from error
        finally:
            if "connection" in locals():
                connection.close()

    @staticmethod
    def _migrate_v17_to_v18(connection: sqlite3.Connection) -> None:
        # Replace the parent table without renaming it: SQLite would otherwise
        # rewrite revision/event FKs to a temporary table name. All child rows
        # and legacy provenance survive; fresh sessions have no base timeline.
        create = EDIT_AGENT_SCHEMA.split("CREATE TABLE edit_agent_revisions", 1)[0]
        create = create.replace("CREATE TABLE edit_agent_runs", "CREATE TABLE edit_agent_runs_v18", 1)
        connection.execute("PRAGMA foreign_keys=OFF")
        try:
            prior_violations = {tuple(row) for row in connection.execute("PRAGMA foreign_key_check")}
            connection.executescript(
                "BEGIN IMMEDIATE;\n" + create
                + "INSERT INTO edit_agent_runs_v18 SELECT * FROM edit_agent_runs;\n"
                "DROP TABLE edit_agent_runs;\n"
                "ALTER TABLE edit_agent_runs_v18 RENAME TO edit_agent_runs;\n"
                "CREATE INDEX edit_agent_runs_project_created ON edit_agent_runs(project_id,created_at DESC);\n"
                + PRODUCTION_SESSION_SCHEMA.replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ")
            )
            if {tuple(row) for row in connection.execute("PRAGMA foreign_key_check")} - prior_violations:
                raise sqlite3.DatabaseError("production migration broke foreign keys")
            connection.execute("PRAGMA user_version=18")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.execute("PRAGMA foreign_keys=ON")

    @staticmethod
    def _migrate_v1_to_v2(connection: sqlite3.Connection) -> None:
        try:
            connection.executescript(
                "BEGIN IMMEDIATE;\n"
                + _INVENTORY_SCHEMA
                + "\nPRAGMA user_version = 2;\nCOMMIT;"
            )
        except sqlite3.Error:
            connection.rollback()
            raise

    @staticmethod
    def _migrate_v6_to_v7(connection: sqlite3.Connection) -> None:
        """Finish an interrupted observation migration without deleting data."""

        existing = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' "
            "AND name = 'evidence_observations'"
        ).fetchone()
        migration_sql = OBSERVATION_SCHEMA
        if existing is not None:
            observed_sql = existing["sql"]
            expected = " ".join(OBSERVATION_SCHEMA.rstrip(";\n ").split())
            observed = (
                " ".join(observed_sql.rstrip(";\n ").split())
                if isinstance(observed_sql, str)
                else ""
            )
            if observed != expected:
                raise sqlite3.DatabaseError("unexpected evidence observation schema")
            migration_sql = ""
        connection.executescript(
            "BEGIN IMMEDIATE;\n"
            + migration_sql
            + "\nPRAGMA user_version = 7;\nCOMMIT;"
        )

    @staticmethod
    def _migrate_v7_to_v8(connection: sqlite3.Connection) -> None:
        """Create or verify the validation workflow after an interrupted migration."""

        expected_columns = {
            "timeline_validation_reports": (
                "report_id",
                "project_id",
                "timeline_id",
                "capability_profile_id",
                "capability_content_hash",
                "report_digest",
                "status",
                "report_json",
                "created_at",
            ),
            "current_timeline_validation": ("slot", "report_id"),
            "timeline_warning_acceptances": (
                "request_id",
                "report_id",
                "report_digest",
                "request_hash",
                "accepted_at",
            ),
        }
        observed = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name IN (?, ?, ?)",
                tuple(expected_columns),
            )
        }
        if observed and observed != set(expected_columns):
            raise sqlite3.DatabaseError("incomplete validation workflow schema")
        if observed:
            for table, expected in expected_columns.items():
                columns = tuple(
                    row["name"]
                    for row in connection.execute(f"PRAGMA table_info({table})")
                )
                if columns != expected:
                    raise sqlite3.DatabaseError("unexpected validation workflow schema")
            indexes = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                )
            }
            if "timeline_validation_reports_project_created" not in indexes:
                raise sqlite3.DatabaseError("incomplete validation workflow indexes")
            migration_sql = ""
        else:
            migration_sql = VALIDATION_WORKFLOW_SCHEMA
        connection.executescript(
            "BEGIN IMMEDIATE;\n"
            + migration_sql
            + "\nPRAGMA user_version = 8;\nCOMMIT;"
        )

    @staticmethod
    def _migrate_v8_to_v9(connection: sqlite3.Connection) -> None:
        """Create or verify planning tables after an interrupted migration."""

        expected_columns = {
            "planning_runs": (
                "run_id", "project_id", "request_id", "project_revision",
                "editing_skill_id", "system_skill_release", "context_hash",
                "context_json", "estimated_input_tokens", "state", "phase",
                "plan_id", "plan_revision", "usage_json", "error_code",
                "created_at", "started_at", "completed_at",
            ),
            "plan_revisions": (
                "plan_id", "revision", "project_id", "status", "content_hash",
                "material_snapshot_id", "analysis_snapshot_id",
                "system_skill_release", "payload_json", "source_run_id", "created_at",
            ),
            "current_plan": ("slot", "plan_id", "revision"),
            "plan_command_receipts": (
                "request_id", "command_type", "request_hash", "response_json", "created_at",
            ),
        }
        observed = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name IN (?, ?, ?, ?)",
                tuple(expected_columns),
            )
        }
        if observed and observed != set(expected_columns):
            raise sqlite3.DatabaseError("incomplete planning schema")
        if observed:
            for table, expected in expected_columns.items():
                columns = tuple(
                    row["name"]
                    for row in connection.execute(f"PRAGMA table_info({table})")
                )
                if columns != expected:
                    raise sqlite3.DatabaseError("unexpected planning schema")
            indexes = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                )
            }
            if not {
                "planning_runs_project_created",
                "plan_revisions_project_created",
            }.issubset(indexes):
                raise sqlite3.DatabaseError("incomplete planning indexes")
            migration_sql = ""
        else:
            migration_sql = PLANNING_SCHEMA
        connection.executescript(
            "BEGIN IMMEDIATE;\n"
            + migration_sql
            + "\nPRAGMA user_version = 9;\nCOMMIT;"
        )

    @staticmethod
    def _migrate_v9_to_v10(connection: sqlite3.Connection) -> None:
        """Create or verify immutable timeline tables after interruption."""

        expected_columns = {
            "edit_timelines": (
                "timeline_id", "project_id", "plan_id", "plan_revision",
                "plan_content_hash", "output_preset", "frame_rate",
                "payload_digest", "payload_json", "created_at",
            ),
            "current_timeline": ("slot", "timeline_id"),
            "timeline_command_receipts": (
                "request_id", "command_type", "request_hash", "timeline_id",
                "created_at",
            ),
        }
        observed = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name IN (?, ?, ?)",
                tuple(expected_columns),
            )
        }
        if observed and observed != set(expected_columns):
            raise sqlite3.DatabaseError("incomplete timeline schema")
        if observed:
            for table, expected in expected_columns.items():
                columns = tuple(
                    row["name"]
                    for row in connection.execute(f"PRAGMA table_info({table})")
                )
                if columns != expected:
                    raise sqlite3.DatabaseError("unexpected timeline schema")
            indexes = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                )
            }
            if "edit_timelines_project_created" not in indexes:
                raise sqlite3.DatabaseError("incomplete timeline indexes")
            migration_sql = ""
        else:
            migration_sql = TIMELINE_SCHEMA
        connection.executescript(
            "BEGIN IMMEDIATE;\n"
            + migration_sql
            + "\nPRAGMA user_version = 10;\nCOMMIT;"
        )

    @staticmethod
    def _migrate_v10_to_v11(connection: sqlite3.Connection) -> None:
        """Create or verify user annotation tables after interruption."""

        expected_columns = {
            "material_annotations": (
                "version_id", "revision", "people_json", "location", "trip_day",
                "day_part", "note", "usage", "updated_at",
            ),
            "transcript_annotations": (
                "version_id", "stream_index", "source_revision_id", "revision",
                "confirmed", "corrections_json", "updated_at",
            ),
            "annotation_command_receipts": (
                "request_id", "command_type", "request_hash", "response_json",
                "created_at",
            ),
        }
        observed = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name IN (?, ?, ?)",
                tuple(expected_columns),
            )
        }
        if observed and observed != set(expected_columns):
            raise sqlite3.DatabaseError("incomplete annotation schema")
        if observed:
            for table, expected in expected_columns.items():
                columns = tuple(
                    row["name"]
                    for row in connection.execute(f"PRAGMA table_info({table})")
                )
                allowed = {expected}
                if table == "transcript_annotations":
                    allowed.add((
                        "version_id", "stream_index", "source_revision_id", "revision",
                        "confirmed", "discarded", "corrections_json", "updated_at",
                    ))
                    allowed.add((
                        "version_id", "stream_index", "source_revision_id", "revision",
                        "confirmed", "corrections_json", "updated_at", "discarded",
                    ))
                if columns not in allowed:
                    raise sqlite3.DatabaseError("unexpected annotation schema")
            indexes = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                )
            }
            if "material_annotations_updated" not in indexes:
                raise sqlite3.DatabaseError("incomplete annotation indexes")
            migration_sql = ""
        else:
            migration_sql = ANNOTATION_SCHEMA
        connection.executescript(
            "BEGIN IMMEDIATE;\n"
            + migration_sql
            + "\nPRAGMA user_version = 11;\nCOMMIT;"
        )

    @staticmethod
    def _migrate_v11_to_v12(connection: sqlite3.Connection) -> None:
        """Add reversible transcript removal without deleting ASR evidence."""

        columns = tuple(
            row["name"]
            for row in connection.execute("PRAGMA table_info(transcript_annotations)")
        )
        previous = (
            "version_id", "stream_index", "source_revision_id", "revision",
            "confirmed", "corrections_json", "updated_at",
        )
        current = (
            "version_id", "stream_index", "source_revision_id", "revision",
            "confirmed", "discarded", "corrections_json", "updated_at",
        )
        migrated = (
            "version_id", "stream_index", "source_revision_id", "revision",
            "confirmed", "corrections_json", "updated_at", "discarded",
        )
        if columns == previous:
            migration_sql = (
                "ALTER TABLE transcript_annotations ADD COLUMN discarded INTEGER NOT NULL "
                "DEFAULT 0 CHECK (discarded IN (0, 1));"
            )
        elif columns in {current, migrated}:
            migration_sql = ""
        else:
            raise sqlite3.DatabaseError("unexpected transcript annotation schema")
        connection.executescript(
            "BEGIN IMMEDIATE;\n"
            + migration_sql
            + "\nPRAGMA user_version = 12;\nCOMMIT;"
        )

    @staticmethod
    def _migrate_v12_to_v13(connection: sqlite3.Connection) -> None:
        """Create durable rough-cut preview render records."""

        observed = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('preview_renders','current_preview_render')"
            )
        }
        if observed and observed != {"preview_renders", "current_preview_render"}:
            raise sqlite3.DatabaseError("incomplete preview render schema")
        connection.executescript(
            "BEGIN IMMEDIATE;\n"
            + ("" if observed else PREVIEW_SCHEMA)
            + "\nPRAGMA user_version = 13;\nCOMMIT;"
        )

    @staticmethod
    def _migrate_v13_to_v14(connection: sqlite3.Connection) -> None:
        """Create or verify durable AI editing session tables."""

        expected_columns = {
            "edit_agent_runs": (
                "run_id", "project_id", "request_id", "base_timeline_id", "state",
                "phase", "max_tool_calls", "tool_calls", "max_preview_renders",
                "preview_renders", "current_revision_id", "final_revision_id",
                "provider", "provider_attempt_id", "usage_json", "error_code",
                "created_at", "started_at", "completed_at",
            ),
            "edit_agent_revisions": (
                "revision_id", "run_id", "ordinal", "parent_revision_id",
                "payload_digest", "payload_json", "summary", "created_at",
            ),
            "edit_agent_events": (
                "event_id", "run_id", "sequence", "event_type", "payload_json",
                "created_at",
            ),
            "edit_agent_command_receipts": (
                "request_id", "command_type", "request_hash", "response_json",
                "created_at",
            ),
        }
        observed = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('edit_agent_runs','edit_agent_revisions',"
                "'edit_agent_events','edit_agent_command_receipts')"
            )
        }
        if observed and observed != set(expected_columns):
            raise sqlite3.DatabaseError("incomplete edit agent schema")
        if observed:
            for table, expected in expected_columns.items():
                columns = tuple(
                    row["name"]
                    for row in connection.execute(f"PRAGMA table_info({table})")
                )
                if columns != expected:
                    raise sqlite3.DatabaseError("unexpected edit agent schema")
            indexes = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                )
            }
            if not {
                "edit_agent_runs_project_created",
                "edit_agent_revisions_run_ordinal",
                "edit_agent_events_run_sequence",
            }.issubset(indexes):
                raise sqlite3.DatabaseError("incomplete edit agent indexes")
        connection.executescript(
            "BEGIN IMMEDIATE;\n"
            + ("" if observed else EDIT_AGENT_SCHEMA)
            + "\nPRAGMA user_version = 14;\nCOMMIT;"
        )

    @staticmethod
    def _migrate_v16_to_v17(connection: sqlite3.Connection) -> None:
        connection.executescript(
            "BEGIN IMMEDIATE;\n"
            "DROP INDEX finishing_runs_created;\n"
            "ALTER TABLE finishing_runs RENAME TO finishing_runs_v16;\n"
            + FINISHING_RUNS_SCHEMA
            + "INSERT INTO finishing_runs SELECT * FROM finishing_runs_v16;\n"
            "DROP TABLE finishing_runs_v16;\n"
            "CREATE INDEX finishing_runs_created ON finishing_runs(created_at DESC);\n"
            "PRAGMA user_version=17;\nCOMMIT;"
        )

    @staticmethod
    def _migrate_v15_to_v16(connection: sqlite3.Connection) -> None:
        expected = {
            "finishing_library": ("role", "directory_json", "assets_json", "issues_json"),
            "finishing_runs": ("run_id", "request_id", "request_hash", "kind", "state", "phase", "progress", "context_json", "result_json", "usage_json", "error_code", "created_at", "completed_at"),
            "finishing_draft": ("slot", "revision", "context_json", "payload_json"),
        }
        observed = {row["name"] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'finishing_%'"
        )}
        if observed and observed != set(expected):
            raise sqlite3.DatabaseError("incomplete finishing schema")
        for table in observed:
            if tuple(row["name"] for row in connection.execute(f"PRAGMA table_info({table})")) != expected[table]:
                raise sqlite3.DatabaseError("unexpected finishing schema")
        connection.executescript(
            "BEGIN IMMEDIATE;\n" + ("" if observed else FINISHING_SCHEMA)
            + "\nPRAGMA user_version = 16;\nCOMMIT;"
        )

    @staticmethod
    def _migrate_v14_to_v15(connection: sqlite3.Connection) -> None:
        """Allow previews to reference an immutable AI edit revision.

        Existing rows and the current-render pointer are copied exactly. The
        referenced payload remains integrity-checked by either TimelineRepository
        or EditAgentRepository before a render can start.
        """

        columns = tuple(
            row["name"] for row in connection.execute("PRAGMA table_info(preview_renders)")
        )
        expected = (
            "render_id", "project_id", "timeline_id", "request_id", "state",
            "progress", "output_relative_path", "output_size_bytes", "error_code",
            "created_at", "started_at", "completed_at",
        )
        if columns != expected:
            raise sqlite3.DatabaseError("unexpected preview render schema")
        current = connection.execute(
            "SELECT render_id FROM current_preview_render WHERE slot=1"
        ).fetchone()
        connection.execute("PRAGMA foreign_keys=OFF")
        try:
            connection.executescript(
                "BEGIN IMMEDIATE;\n"
                "DROP TABLE current_preview_render;\n"
                "DROP INDEX preview_renders_project_created;\n"
                "ALTER TABLE preview_renders RENAME TO preview_renders_v14;\n"
                + PREVIEW_SCHEMA
                + "\nINSERT INTO preview_renders SELECT * FROM preview_renders_v14;\n"
                "DROP TABLE preview_renders_v14;\n"
            )
            if current is not None:
                connection.execute(
                    "INSERT INTO current_preview_render(slot,render_id) VALUES(1,?)",
                    (current["render_id"],),
                )
            connection.execute("PRAGMA user_version=15")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.execute("PRAGMA foreign_keys=ON")

    @staticmethod
    def _migrate_v2_to_v3(connection: sqlite3.Connection) -> None:
        try:
            connection.executescript(
                "BEGIN IMMEDIATE;\n"
                + _ANALYSIS_SCHEMA
                + "\nPRAGMA user_version = 4;\nCOMMIT;"
            )
        except sqlite3.Error:
            connection.rollback()
            raise

    @staticmethod
    def _migrate_v3_to_v4(connection: sqlite3.Connection) -> None:
        """Upgrade the unreleased v3 analysis skeleton without losing its rows."""
        try:
            connection.executescript(
                """
                BEGIN IMMEDIATE;
                ALTER TABLE preparation_runs ADD COLUMN project_revision INTEGER;
                ALTER TABLE preparation_runs ADD COLUMN scope_id TEXT;
                ALTER TABLE preparation_runs ADD COLUMN operation_id TEXT;
                ALTER TABLE preparation_runs ADD COLUMN attempt_id TEXT;
                ALTER TABLE preparation_runs ADD COLUMN attempt_number INTEGER DEFAULT 1;
                UPDATE preparation_runs SET
                    project_revision = COALESCE(
                        (SELECT project_revision FROM inventory_runs
                         WHERE inventory_runs.run_id = preparation_runs.inventory_run_id),
                        1
                    ),
                    scope_id = 'scope_' || substr(run_id, 6, 32),
                    operation_id = 'op_' || substr(run_id, 6, 32),
                    attempt_id = 'att_' || substr(run_id, 6, 32),
                    attempt_number = 1;
                CREATE UNIQUE INDEX preparation_runs_operation
                    ON preparation_runs(operation_id);
                CREATE UNIQUE INDEX preparation_runs_attempt
                    ON preparation_runs(attempt_id);

                CREATE TABLE preparation_items_v4 (
                    run_id TEXT NOT NULL REFERENCES preparation_runs(run_id)
                        ON DELETE CASCADE,
                    version_id TEXT NOT NULL REFERENCES asset_versions(version_id),
                    stream_index INTEGER NOT NULL CHECK (stream_index >= -1),
                    status TEXT NOT NULL CHECK (
                        status IN ('pending', 'prepared', 'transcribed', 'no_audio',
                                   'no_speech', 'failed')
                    ),
                    error_code TEXT,
                    checkpoint_json TEXT,
                    audio_artifact_id TEXT REFERENCES artifacts(artifact_id),
                    transcript_revision_id TEXT
                        REFERENCES transcription_revisions(revision_id),
                    PRIMARY KEY(run_id, version_id, stream_index)
                );
                INSERT INTO preparation_items_v4
                    (run_id, version_id, stream_index, status, error_code, checkpoint_json)
                SELECT run_id, version_id, COALESCE(stream_index, -1), status,
                       error_code, checkpoint_json
                FROM preparation_items;
                DROP TABLE preparation_items;
                ALTER TABLE preparation_items_v4 RENAME TO preparation_items;

                CREATE TABLE preparation_chunks (
                    run_id TEXT NOT NULL,
                    version_id TEXT NOT NULL,
                    stream_index INTEGER NOT NULL CHECK (stream_index >= 0),
                    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
                    start_sample INTEGER NOT NULL CHECK (start_sample >= 0),
                    end_sample INTEGER NOT NULL CHECK (end_sample > start_sample),
                    source_span_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('pending', 'transcribed', 'no_speech')
                    ),
                    transcript_artifact_id TEXT REFERENCES artifacts(artifact_id),
                    result_json TEXT,
                    PRIMARY KEY(run_id, version_id, stream_index, ordinal),
                    FOREIGN KEY(run_id, version_id, stream_index)
                        REFERENCES preparation_items(run_id, version_id, stream_index)
                        ON DELETE CASCADE
                );
                CREATE INDEX preparation_items_run_status
                    ON preparation_items(run_id, status, version_id);
                CREATE INDEX preparation_chunks_run_status
                    ON preparation_chunks(
                        run_id, status, version_id, stream_index, ordinal
                    );
                PRAGMA user_version = 4;
                COMMIT;
                """
            )
        except sqlite3.Error:
            connection.rollback()
            raise

    @staticmethod
    def _migrate_v4_to_v5(connection: sqlite3.Connection) -> None:
        """Keep old transcripts while making their unverified quality explicit."""
        try:
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(preparation_items)"
                ).fetchall()
            }
            add_review_column = (
                "" if "review_required" in columns else
                "ALTER TABLE preparation_items ADD COLUMN review_required INTEGER "
                "NOT NULL DEFAULT 0 CHECK (review_required IN (0, 1));"
            )
            connection.executescript(
                "BEGIN IMMEDIATE;\n"
                + add_review_column
                + """
                UPDATE preparation_items
                SET review_required = 1,
                    error_code = 'transcript_quality_unverified'
                WHERE status = 'transcribed'
                  AND transcript_revision_id IN (
                      SELECT revision_id FROM transcription_revisions
                      WHERE recipe_id = 'asr.mlx-whisper.v1'
                  );
                UPDATE transcription_revisions
                SET status = 'needs_review'
                WHERE status = 'transcribed'
                  AND recipe_id = 'asr.mlx-whisper.v1';
                UPDATE preparation_runs
                SET prepared_count = (
                        SELECT COUNT(*) FROM preparation_items
                        WHERE preparation_items.run_id = preparation_runs.run_id
                          AND status IN ('prepared', 'transcribed', 'no_speech')
                    ),
                    transcribed_count = (
                        SELECT COUNT(*) FROM preparation_items
                        WHERE preparation_items.run_id = preparation_runs.run_id
                          AND status = 'transcribed' AND review_required = 0
                    ),
                    issue_count = (
                        SELECT COUNT(*) FROM preparation_items
                        WHERE preparation_items.run_id = preparation_runs.run_id
                          AND (status = 'failed' OR review_required = 1)
                    );
                PRAGMA user_version = 5;
                COMMIT;
                """
            )
        except sqlite3.Error:
            connection.rollback()
            raise

    @staticmethod
    def _configure(connection: sqlite3.Connection) -> None:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA busy_timeout = 5000")

    @staticmethod
    def _read_snapshot(connection: sqlite3.Connection) -> ProjectSnapshot:
        row = connection.execute(
            "SELECT project_id, name, brief, stage, revision, created_at, updated_at "
            "FROM project LIMIT 1"
        ).fetchone()
        if row is None:
            raise ProjectError(
                "storage_corrupt", "Clipen 無法讀取作品資料。", status_code=500
            )
        directories = connection.execute(
            "SELECT role, canonical_path, display_path, display_name, device, inode, saved_at "
            "FROM project_directories ORDER BY role"
        ).fetchall()
        return ProjectSnapshot(
            project_id=row["project_id"],
            name=row["name"],
            brief=row["brief"],
            stage=row["stage"],
            revision=row["revision"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            directories=tuple(
                DirectoryReference(
                    role=DirectoryRole(item["role"]),
                    canonical_path=item["canonical_path"],
                    display_path=item["display_path"],
                    display_name=item["display_name"],
                    device=item["device"],
                    inode=item["inode"],
                    saved_at=item["saved_at"],
                )
                for item in directories
            ),
        )

    def _project_dir(self, project_id: str) -> Path:
        validate_project_id(project_id)
        path = self.root / project_id
        if path.parent != self.root:
            raise ProjectError("project_not_found", "找不到這個作品。", status_code=404)
        return path

    @staticmethod
    def _remove_staging(staging: Path) -> None:
        for name in ("project.sqlite3-wal", "project.sqlite3-shm", "project.sqlite3"):
            candidate = staging / name
            if candidate.exists() and not candidate.is_symlink():
                candidate.unlink()
        staging.rmdir()
