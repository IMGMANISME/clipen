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


_SCHEMA_VERSION = 1
_PROJECT_DIRECTORY = re.compile(r"prj_[0-9a-f]{32}\Z")


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
        os.chmod(self.root, 0o700)

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
        with self._connect(project_id) as connection:
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
        with self._connect(project_id) as connection:
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
        return self._mutate(
            project_id,
            request_id,
            "set_directory",
            request_hash,
            expected_revision,
            lambda connection, now: connection.execute(
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
            ),
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
        return self._mutate(
            project_id,
            normalized_request,
            "remove_directory",
            command_hash,
            revision,
            lambda connection, _: connection.execute(
                "DELETE FROM project_directories WHERE role = ?", (role.value,)
            ),
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
        with self._connect(project_id) as connection:
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
                PRAGMA user_version = 1;
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
        os.chmod(database, 0o600)
        return snapshot

    @contextmanager
    def _connect(self, project_id: str) -> Iterator[sqlite3.Connection]:
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
            if version != _SCHEMA_VERSION:
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
