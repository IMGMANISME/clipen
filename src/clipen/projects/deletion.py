"""Crash-tolerant deletion of Clipen-managed project directories."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import threading
import uuid
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Mapping, Protocol

from clipen.filesystem import restrict_permissions

from .models import (
    ProjectError,
    ProjectSnapshot,
    validate_name,
    validate_project_id,
    validate_request_id,
    validate_revision,
)


class ProjectStorage(Protocol):
    root: Path

    def get_project(self, project_id: str) -> ProjectSnapshot: ...

    def connect(self, project_id: str) -> AbstractContextManager[sqlite3.Connection]: ...


class ProjectDeletion:
    """Own deletion receipts and remove only children of the managed root."""

    def __init__(self, storage: ProjectStorage) -> None:
        self.storage = storage
        self._lock = threading.Lock()
        self._cleanup_tombstones()

    def delete(
        self,
        project_id: str,
        request_id: object,
        expected_revision: object,
        confirmation_name: object,
    ) -> str:
        validate_project_id(project_id)
        normalized_request = validate_request_id(request_id)
        revision = validate_revision(expected_revision)
        confirmed_name = validate_name(confirmation_name)
        request_hash = self._command_hash(project_id, revision, confirmed_name)

        with self._lock:
            receipt = self._read_receipt(normalized_request)
            if receipt is not None:
                if receipt.get("request_hash") != request_hash:
                    raise ProjectError(
                        "request_reused",
                        "這次操作的識別資訊已用於不同內容。",
                        status_code=409,
                    )
                return str(receipt["project_id"])

            snapshot = self.storage.get_project(project_id)
            if snapshot.revision != revision:
                raise ProjectError(
                    "revision_conflict",
                    "作品已在其他分頁更新，請重新載入最新內容。",
                    status_code=409,
                )
            if snapshot.name != confirmed_name:
                raise ProjectError(
                    "confirmation_mismatch",
                    "輸入的作品名稱不一致，Clipen 沒有刪除作品。",
                    status_code=422,
                )
            self._reject_active_project(project_id)
            self._validate_managed_tree(project_id)
            return self._remove_managed_directory(
                project_id,
                normalized_request,
                request_hash,
            )

    def _validate_managed_tree(self, project_id: str) -> None:
        project = self.storage.root / project_id
        if project.parent != self.storage.root or project.is_symlink() or not project.is_dir():
            raise ProjectError("project_not_found", "找不到這個作品。", status_code=404)
        expected_owner = os.getuid() if hasattr(os, "getuid") else None
        for parent, directory_names, file_names in os.walk(project, followlinks=False):
            parent_path = Path(parent)
            if parent_path != project and os.path.ismount(parent_path):
                raise ProjectError(
                    "storage_unsafe",
                    "作品資料包含掛載點，Clipen 沒有進行刪除。",
                    status_code=409,
                )
            for name in (*directory_names, *file_names):
                path = parent_path / name
                info = path.lstat()
                wrong_owner = expected_owner is not None and info.st_uid != expected_owner
                if path.is_symlink() or wrong_owner:
                    raise ProjectError(
                        "storage_unsafe",
                        "作品資料包含不安全的檔案，Clipen 沒有進行刪除。",
                        status_code=409,
                    )
                if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
                    raise ProjectError(
                        "storage_unsafe",
                        "作品資料包含硬連結，Clipen 沒有進行刪除。",
                        status_code=409,
                    )

    @staticmethod
    def _command_hash(project_id: str, revision: int, name: str) -> str:
        payload = json.dumps(
            {
                "project_id": project_id,
                "expected_revision": revision,
                "confirmation_name": name,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _remove_managed_directory(
        self,
        project_id: str,
        request_id: str,
        request_hash: str,
    ) -> str:
        project_dir = self.storage.root / project_id
        if project_dir.parent != self.storage.root:
            raise ProjectError("project_not_found", "找不到這個作品。", status_code=404)
        deleting = self.storage.root / f".deleting-{project_id}-{uuid.uuid4().hex}"
        try:
            project_dir.rename(deleting)
            self._write_receipt(
                request_id,
                {
                    "request_hash": request_hash,
                    "project_id": project_id,
                },
            )
        except (OSError, ProjectError) as error:
            if deleting.exists() and not project_dir.exists():
                try:
                    deleting.rename(project_dir)
                except OSError:
                    pass
            if isinstance(error, ProjectError):
                raise
            raise ProjectError(
                "delete_failed",
                "Clipen 無法安全刪除作品；毛片沒有被修改。",
                status_code=500,
            ) from error

        try:
            shutil.rmtree(deleting)
        except OSError:
            # It is already hidden from the workspace; startup retries cleanup.
            pass
        return project_id

    def _reject_active_project(self, project_id: str) -> None:
        active_states = ("queued", "running", "cancelling")
        queries = (
            "SELECT 1 FROM inventory_runs WHERE state IN (?, ?, ?) LIMIT 1",
            "SELECT 1 FROM preparation_runs WHERE state IN (?, ?, ?) LIMIT 1",
        )
        with self.storage.connect(project_id) as connection:
            active = any(connection.execute(query, active_states).fetchone() is not None for query in queries)
            active = (
                active
                or connection.execute("SELECT 1 FROM evidence_runs WHERE state = 'running' LIMIT 1").fetchone()
                is not None
            )
            active = (
                active
                or connection.execute("SELECT 1 FROM evidence_observations WHERE state = 'running' LIMIT 1").fetchone()
                is not None
            )
        if active:
            raise ProjectError(
                "project_busy",
                "作品仍有分析或 AI 工作正在執行，請先停止後再刪除。",
                status_code=409,
            )

    def _receipts_root(self) -> Path:
        receipts = self.storage.root / ".delete-receipts"
        receipts.mkdir(mode=0o700, exist_ok=True)
        if receipts.is_symlink() or not receipts.is_dir():
            raise ProjectError("storage_unsafe", "Clipen 刪除紀錄位置不安全。", status_code=500)
        restrict_permissions(receipts, 0o700)
        return receipts

    def _receipt_path(self, request_id: str) -> Path:
        name = hashlib.sha256(request_id.encode("utf-8")).hexdigest() + ".json"
        return self._receipts_root() / name

    def _read_receipt(self, request_id: str) -> dict[str, object] | None:
        path = self._receipt_path(request_id)
        if not path.exists():
            return None
        if path.is_symlink() or not path.is_file():
            raise ProjectError("storage_unsafe", "Clipen 刪除紀錄不安全。", status_code=500)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ProjectError("storage_corrupt", "Clipen 無法讀取刪除紀錄。", status_code=500) from error
        if not isinstance(value, dict) or value.get("project_id") is None:
            raise ProjectError("storage_corrupt", "Clipen 無法讀取刪除紀錄。", status_code=500)
        return value

    def _write_receipt(self, request_id: str, value: Mapping[str, object]) -> None:
        path = self._receipt_path(request_id)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(value, output, ensure_ascii=False, separators=(",", ":"))
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _cleanup_tombstones(self) -> None:
        pattern = re.compile(r"\.deleting-prj_[0-9a-f]{32}-[0-9a-f]{32}\Z")
        try:
            entries = tuple(self.storage.root.iterdir())
        except OSError:
            return
        for entry in entries:
            if pattern.fullmatch(entry.name) is None or entry.is_symlink():
                continue
            if entry.is_dir():
                try:
                    shutil.rmtree(entry)
                except OSError:
                    pass
