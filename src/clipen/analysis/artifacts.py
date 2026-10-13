"""Content-addressed artifacts confined to Clipen-managed project storage."""

from __future__ import annotations

import hashlib
from functools import wraps
import json
import os
from pathlib import Path
import re
import secrets
import stat
import threading
from typing import Callable, Iterable, Mapping, TypeVar

from clipen.filesystem import restrict_permissions
from clipen.projects.models import ProjectError, validate_project_id
from clipen.projects.storage import ProjectRepository, utc_now

from .models import (
    ArtifactKind,
    ArtifactRecord,
    SourceSpan,
    validate_attempt_id,
    validate_operation_id,
    validate_recipe_id,
)


_READ_SIZE = 1024 * 1024
_BLOB_PATH = re.compile(r"artifacts/sha256/([0-9a-f]{2})/([0-9a-f]{64})\Z")
_STORE_LOCKS_GUARD = threading.Lock()
_STORE_LOCKS: dict[Path, threading.RLock] = {}
_Return = TypeVar("_Return")


def _store_lock(root: Path) -> threading.RLock:
    with _STORE_LOCKS_GUARD:
        return _STORE_LOCKS.setdefault(root, threading.RLock())


def _serialized(method: Callable[..., _Return]) -> Callable[..., _Return]:
    @wraps(method)
    def locked(self: "ArtifactStore", *args: object, **kwargs: object) -> _Return:
        with self._lock:
            return method(self, *args, **kwargs)

    return locked


class ArtifactStore:
    """Publishes only files created below a project's private staging root."""

    def __init__(
        self,
        projects: ProjectRepository,
        *,
        max_artifact_bytes: int = 16 * 1024**3,
    ) -> None:
        if (
            isinstance(max_artifact_bytes, bool)
            or not isinstance(max_artifact_bytes, int)
            or not 1 <= max_artifact_bytes <= 64 * 1024**3
        ):
            raise ValueError("artifact size limit is invalid")
        self.projects = projects
        self.max_artifact_bytes = max_artifact_bytes
        self._lock = _store_lock(projects.root)

    def create_staging_file(self, project_id: str, *, suffix: str = "") -> Path:
        root = self._managed_directory(project_id, "staging")
        if suffix and (
            len(suffix) > 24
            or not suffix.startswith(".")
            or any(character not in ".abcdefghijklmnopqrstuvwxyz0123456789" for character in suffix)
        ):
            raise ValueError("artifact staging suffix is invalid")
        path = root / f"artifact-{secrets.token_hex(16)}{suffix}"
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        os.close(descriptor)
        return path

    @_serialized
    def publish(
        self,
        project_id: str,
        staging_path: Path,
        *,
        kind: ArtifactKind,
        producer_operation_id: str,
        producer_attempt_id: str,
        recipe_id: str,
        lineage: Mapping[str, object],
        source_span: SourceSpan | None = None,
    ) -> ArtifactRecord:
        validate_project_id(project_id)
        validate_operation_id(producer_operation_id)
        validate_attempt_id(producer_attempt_id)
        validate_recipe_id(recipe_id)
        lineage_json = self._bounded_json(lineage, "artifact lineage")
        staging_root = self._managed_directory(project_id, "staging")
        candidate = Path(staging_path)
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as error:
            raise ProjectError(
                "artifact_staging_missing",
                "Clipen 找不到待保存的分析成果。",
                status_code=409,
            ) from error
        if resolved.parent != staging_root or candidate.is_symlink():
            raise ProjectError(
                "artifact_staging_unsafe",
                "Clipen 拒絕保存不安全的分析成果。",
                status_code=409,
            )
        info = os.lstat(resolved)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ProjectError(
                "artifact_staging_unsafe",
                "Clipen 拒絕保存不安全的分析成果。",
                status_code=409,
            )
        if info.st_size > self.max_artifact_bytes:
            raise ProjectError(
                "artifact_too_large",
                "分析成果超過這次工作的安全容量。",
                status_code=413,
            )
        digest = self._sha256(resolved)
        identity = json.dumps(
            {
                "kind": kind.value,
                "sha256": digest,
                "operation": producer_operation_id,
                "attempt": producer_attempt_id,
                "recipe": recipe_id,
                "source_span": None if source_span is None else source_span.to_storage(),
                "lineage": json.loads(lineage_json),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        artifact_id = "art_" + hashlib.sha256(identity).hexdigest()[:32]
        artifacts_root = self._managed_directory(project_id, "artifacts")
        digest_root = self._managed_child_directory(artifacts_root, "sha256")
        blob_root = self._managed_child_directory(digest_root, digest[:2])
        destination = blob_root / digest
        if destination.exists() or destination.is_symlink():
            self._verify_existing_blob(destination, digest, info.st_size)
            resolved.unlink()
        else:
            os.replace(resolved, destination)
            restrict_permissions(destination, 0o600)
        relative_path = destination.relative_to(self._project_root(project_id)).as_posix()
        created_at = utc_now()
        source_json = (
            None
            if source_span is None
            else json.dumps(
                source_span.to_storage(),
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        with self.projects.connect(project_id) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT INTO artifacts "
                    "(artifact_id, project_id, kind, relative_path, sha256, size_bytes, "
                    "producer_operation_id, producer_attempt_id, recipe_id, "
                    "source_span_json, lineage_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(artifact_id) DO NOTHING",
                    (
                        artifact_id,
                        project_id,
                        kind.value,
                        relative_path,
                        digest,
                        info.st_size,
                        producer_operation_id,
                        producer_attempt_id,
                        recipe_id,
                        source_json,
                        lineage_json,
                        created_at,
                    ),
                )
                record = self._read(connection, artifact_id)
                connection.commit()
            except ProjectError:
                connection.rollback()
                raise
            except Exception as error:
                connection.rollback()
                raise ProjectError(
                    "artifact_storage_failed",
                    "Clipen 無法保存分析成果。",
                    status_code=500,
                ) from error
        return record

    @_serialized
    def prune_unreferenced(
        self,
        project_id: str,
        relative_paths: Iterable[str],
    ) -> int:
        """Delete retired content blobs only when no database record still uses them."""
        validate_project_id(project_id)
        candidates: set[str] = set()
        for relative_path in relative_paths:
            if not isinstance(relative_path, str):
                continue
            matched = _BLOB_PATH.fullmatch(relative_path)
            if matched is None or matched.group(2)[:2] != matched.group(1):
                continue
            candidates.add(relative_path)
        if not candidates:
            return 0
        with self.projects.connect(project_id) as connection:
            live = {
                row["relative_path"]
                for row in connection.execute(
                    "SELECT DISTINCT relative_path FROM artifacts"
                ).fetchall()
            }
        project_root = self._project_root(project_id)
        removed = 0
        for relative_path in sorted(candidates - live):
            candidate = project_root.joinpath(*relative_path.split("/"))
            try:
                parent = candidate.parent.resolve(strict=True)
                expected_parent = project_root / "artifacts" / "sha256" / candidate.parent.name
                if (
                    parent != expected_parent
                    or candidate.is_symlink()
                ):
                    continue
                info = os.lstat(candidate)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    continue
                candidate.unlink()
                removed += 1
                try:
                    parent.rmdir()
                except OSError:
                    pass
            except (FileNotFoundError, OSError):
                continue
        return removed

    def get(self, project_id: str, artifact_id: str) -> ArtifactRecord:
        validate_project_id(project_id)
        with self.projects.connect(project_id) as connection:
            record = self._read(connection, artifact_id)
        if record.project_id != project_id:
            raise ProjectError("artifact_not_found", "找不到分析成果。", status_code=404)
        return record

    def path(self, record: ArtifactRecord) -> Path:
        root = self._project_root(record.project_id)
        candidate = root.joinpath(*record.relative_path.split("/"))
        resolved = candidate.resolve(strict=True)
        if root not in resolved.parents or candidate.is_symlink():
            raise ProjectError(
                "artifact_store_unsafe",
                "Clipen 的分析成果儲存位置不安全。",
                status_code=500,
            )
        self._verify_existing_blob(resolved, record.sha256, record.size_bytes)
        return resolved

    def _project_root(self, project_id: str) -> Path:
        validate_project_id(project_id)
        root = self.projects.root / project_id
        if root.is_symlink() or not root.is_dir():
            raise ProjectError("project_not_found", "找不到這個作品。", status_code=404)
        return root.resolve(strict=True)

    def _managed_directory(self, project_id: str, name: str) -> Path:
        root = self._project_root(project_id)
        path = root / name
        path.mkdir(mode=0o700, exist_ok=True)
        if path.is_symlink() or not path.is_dir() or path.resolve(strict=True).parent != root:
            raise ProjectError(
                "artifact_store_unsafe",
                "Clipen 的分析成果儲存位置不安全。",
                status_code=500,
            )
        restrict_permissions(path, 0o700)
        return path.resolve(strict=True)

    @staticmethod
    def _managed_child_directory(parent: Path, name: str) -> Path:
        path = parent / name
        path.mkdir(mode=0o700, exist_ok=True)
        if path.is_symlink() or not path.is_dir():
            raise ProjectError(
                "artifact_store_unsafe",
                "Clipen 的分析成果儲存位置不安全。",
                status_code=500,
            )
        resolved = path.resolve(strict=True)
        if resolved.parent != parent:
            raise ProjectError(
                "artifact_store_unsafe",
                "Clipen 的分析成果儲存位置不安全。",
                status_code=500,
            )
        restrict_permissions(resolved, 0o700)
        return resolved

    def _sha256(self, path: Path) -> str:
        digest = hashlib.sha256()
        total = 0
        with path.open("rb") as source:
            while True:
                block = source.read(_READ_SIZE)
                if not block:
                    break
                total += len(block)
                if total > self.max_artifact_bytes:
                    raise ProjectError(
                        "artifact_too_large",
                        "分析成果超過這次工作的安全容量。",
                        status_code=413,
                    )
                digest.update(block)
        return digest.hexdigest()

    def _verify_existing_blob(self, path: Path, digest: str, size: int) -> None:
        if path.is_symlink():
            raise ProjectError(
                "artifact_store_unsafe",
                "Clipen 的分析成果儲存位置不安全。",
                status_code=500,
            )
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != size:
            raise ProjectError(
                "artifact_store_corrupt",
                "Clipen 偵測到損壞的分析成果。",
                status_code=500,
            )
        observed = self._sha256(path)
        if observed != digest:
            raise ProjectError(
                "artifact_store_corrupt",
                "Clipen 偵測到損壞的分析成果。",
                status_code=500,
            )

    @staticmethod
    def _bounded_json(value: Mapping[str, object], label: str) -> str:
        if not isinstance(value, Mapping):
            raise ValueError(f"{label} must be an object")
        try:
            encoded = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError) as error:
            raise ValueError(f"{label} is not JSON serializable") from error
        if len(encoded.encode("utf-8")) > 64 * 1024:
            raise ValueError(f"{label} is too large")
        return encoded

    @staticmethod
    def _read(connection, artifact_id: str) -> ArtifactRecord:
        if not isinstance(artifact_id, str):
            raise ProjectError("artifact_not_found", "找不到分析成果。", status_code=404)
        row = connection.execute(
            "SELECT artifact_id, project_id, kind, relative_path, sha256, size_bytes, "
            "producer_operation_id, producer_attempt_id, recipe_id, source_span_json, "
            "lineage_json, created_at FROM artifacts WHERE artifact_id = ?",
            (artifact_id,),
        ).fetchone()
        if row is None:
            raise ProjectError("artifact_not_found", "找不到分析成果。", status_code=404)
        try:
            raw_span = None if row["source_span_json"] is None else json.loads(row["source_span_json"])
            if raw_span is not None and not isinstance(raw_span, dict):
                raise ValueError("stored source span is invalid")
            lineage = json.loads(row["lineage_json"])
            if not isinstance(lineage, dict):
                raise ValueError("stored lineage is invalid")
            return ArtifactRecord(
                row["artifact_id"],
                row["project_id"],
                ArtifactKind(row["kind"]),
                row["relative_path"],
                row["sha256"],
                row["size_bytes"],
                row["producer_operation_id"],
                row["producer_attempt_id"],
                row["recipe_id"],
                lineage,
                row["created_at"],
                None if raw_span is None else SourceSpan.from_storage(raw_span),
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            raise ProjectError(
                "artifact_store_corrupt",
                "Clipen 無法讀取分析成果。",
                status_code=500,
            ) from error
