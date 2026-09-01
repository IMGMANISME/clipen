"""Transactional storage and publish semantics for media inventories."""

from __future__ import annotations

import hashlib
import json
import sqlite3

from clipen.projects.models import (
    DirectoryRole,
    ProjectError,
    validate_project_id,
    validate_request_id,
    validate_revision,
)
from clipen.projects.storage import ProjectRepository, command_digest, utc_now

from .models import (
    AssetCandidate,
    InventoryCounts,
    InventoryRoot,
    InventoryRun,
    InventoryState,
    make_run_id,
    validate_run_id,
)


_TERMINAL_STATES = frozenset(
    {
        InventoryState.SUCCEEDED,
        InventoryState.FAILED,
        InventoryState.CANCELLED,
        InventoryState.INTERRUPTED,
    }
)
_TRANSITIONS = {
    InventoryState.QUEUED: {
        InventoryState.RUNNING,
        InventoryState.CANCELLED,
        InventoryState.INTERRUPTED,
    },
    InventoryState.RUNNING: {
        InventoryState.CANCELLING,
        InventoryState.SUCCEEDED,
        InventoryState.FAILED,
        InventoryState.CANCELLED,
        InventoryState.INTERRUPTED,
    },
    InventoryState.CANCELLING: {
        InventoryState.CANCELLED,
        InventoryState.FAILED,
        InventoryState.INTERRUPTED,
    },
}


class InventoryRepository:
    def __init__(self, projects: ProjectRepository) -> None:
        self.projects = projects

    def create_run(
        self,
        project_id: str,
        request_id: object,
        expected_revision: object,
    ) -> InventoryRun:
        validate_project_id(project_id)
        normalized_request = validate_request_id(request_id)
        revision = validate_revision(expected_revision)
        request_hash = command_digest({"expected_revision": revision})
        run_id = make_run_id(project_id, normalized_request)
        with self.projects.connect(project_id) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                receipt = connection.execute(
                    "SELECT command_type, request_hash, run_id "
                    "FROM inventory_command_receipts WHERE request_id = ?",
                    (normalized_request,),
                ).fetchone()
                if receipt is not None:
                    if (
                        receipt["command_type"] != "start_inventory"
                        or receipt["request_hash"] != request_hash
                    ):
                        raise ProjectError(
                            "request_reused",
                            "這次操作的識別資訊已用於不同內容。",
                            status_code=409,
                        )
                    replay = self._read_run(connection, receipt["run_id"])
                    connection.rollback()
                    return replay

                project = connection.execute(
                    "SELECT revision FROM project WHERE project_id = ?", (project_id,)
                ).fetchone()
                if project is None:
                    raise ProjectError("project_not_found", "找不到這個作品。", status_code=404)
                if project["revision"] != revision:
                    raise ProjectError(
                        "revision_conflict",
                        "作品已在其他分頁更新，請重新載入最新內容。",
                        status_code=409,
                    )
                rows = connection.execute(
                    "SELECT role, canonical_path, device, inode, saved_at "
                    "FROM project_directories ORDER BY role"
                ).fetchall()
                roots = tuple(
                    InventoryRoot(
                        DirectoryRole(row["role"]),
                        row["canonical_path"],
                        row["device"],
                        row["inode"],
                        row["saved_at"],
                    )
                    for row in rows
                )
                if DirectoryRole.SOURCE not in {root.role for root in roots}:
                    raise ProjectError(
                        "source_required",
                        "請先選擇毛片資料夾。",
                        status_code=409,
                    )
                incomplete = connection.execute(
                    "SELECT run_id FROM inventory_runs "
                    "WHERE state IN ('queued', 'running', 'cancelling') LIMIT 1"
                ).fetchone()
                if incomplete is not None:
                    raise ProjectError(
                        "inventory_busy",
                        "已有一個素材檢查正在進行。",
                        status_code=409,
                    )
                roots_json = json.dumps(
                    [root.to_storage() for root in roots],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                fingerprint = hashlib.sha256(
                    json.dumps(
                        {
                            "project_revision": revision,
                            "roots": json.loads(roots_json),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                now = utc_now()
                connection.execute(
                    "INSERT INTO inventory_runs "
                    "(run_id, project_id, request_id, project_revision, input_fingerprint, "
                    "roots_json, state, phase, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'queued', 'preparing', ?)",
                    (
                        run_id,
                        project_id,
                        normalized_request,
                        revision,
                        fingerprint,
                        roots_json,
                        now,
                    ),
                )
                connection.execute(
                    "INSERT INTO inventory_command_receipts "
                    "(request_id, command_type, request_hash, run_id, created_at) "
                    "VALUES (?, 'start_inventory', ?, ?, ?)",
                    (normalized_request, request_hash, run_id, now),
                )
                created = self._read_run(connection, run_id)
                connection.commit()
                return created
            except ProjectError:
                connection.rollback()
                raise
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
                connection.rollback()
                raise ProjectError(
                    "storage_corrupt", "Clipen 無法讀取素材檢查紀錄。", status_code=500
                ) from error
            except sqlite3.Error as error:
                connection.rollback()
                raise ProjectError(
                    "storage_failed", "Clipen 無法建立素材檢查。", status_code=500
                ) from error

    def get_run(self, project_id: str, run_id: object) -> InventoryRun:
        validate_project_id(project_id)
        normalized_run = validate_run_id(run_id)
        with self.projects.connect(project_id) as connection:
            run = self._read_run(connection, normalized_run)
        if run.project_id != project_id:
            raise ProjectError("inventory_not_found", "找不到這次素材檢查。", status_code=404)
        return run

    def current_run(self, project_id: str) -> InventoryRun | None:
        validate_project_id(project_id)
        with self.projects.connect(project_id) as connection:
            row = connection.execute(
                "SELECT run_id FROM current_inventory WHERE slot = 1"
            ).fetchone()
            return None if row is None else self._read_run(connection, row["run_id"])

    def transition(
        self,
        project_id: str,
        run_id: object,
        target: InventoryState,
        *,
        phase: str,
        error_code: str | None = None,
    ) -> InventoryRun:
        validate_project_id(project_id)
        normalized_run = validate_run_id(run_id)
        if not phase or len(phase) > 40 or not phase.replace("_", "a").isalnum():
            raise ValueError("inventory phase is invalid")
        if target is InventoryState.SUCCEEDED:
            raise ValueError("use publish() to complete a successful inventory")
        with self.projects.connect(project_id) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                current = self._read_run(connection, normalized_run)
                if current.project_id != project_id:
                    raise ProjectError(
                        "inventory_not_found", "找不到這次素材檢查。", status_code=404
                    )
                if target not in _TRANSITIONS.get(current.state, set()):
                    raise ProjectError(
                        "inventory_state_conflict",
                        "這次素材檢查的狀態已經改變。",
                        status_code=409,
                    )
                now = utc_now()
                started = current.started_at or now
                completed = now if target in _TERMINAL_STATES else None
                connection.execute(
                    "UPDATE inventory_runs SET state = ?, phase = ?, error_code = ?, "
                    "started_at = ?, completed_at = ? WHERE run_id = ?",
                    (target.value, phase, error_code, started, completed, normalized_run),
                )
                updated = self._read_run(connection, normalized_run)
                connection.commit()
                return updated
            except ProjectError:
                connection.rollback()
                raise
            except sqlite3.Error as error:
                connection.rollback()
                raise ProjectError(
                    "storage_failed", "Clipen 無法更新素材檢查。", status_code=500
                ) from error

    def publish(self, project_id: str, run_id: object) -> InventoryRun:
        validate_project_id(project_id)
        normalized_run = validate_run_id(run_id)
        with self.projects.connect(project_id) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                current = self._read_run(connection, normalized_run)
                if current.project_id != project_id or current.state is not InventoryState.RUNNING:
                    raise ProjectError(
                        "inventory_state_conflict",
                        "這次素材檢查目前不能完成。",
                        status_code=409,
                    )
                counts = connection.execute(
                    "SELECT COUNT(*) AS discovered, "
                    "SUM(CASE WHEN v.probe_status IS NOT NULL "
                    "              AND v.probe_status != 'pending' THEN 1 ELSE 0 END) AS probed, "
                    "SUM(CASE WHEN i.status = 'usable' THEN 1 ELSE 0 END) AS usable, "
                    "SUM(CASE WHEN i.status = 'needs_attention' THEN 1 ELSE 0 END) AS issues, "
                    "SUM(CASE WHEN i.status = 'ignored' THEN 1 ELSE 0 END) AS ignored "
                    "FROM inventory_items i LEFT JOIN asset_versions v "
                    "ON v.version_id = i.version_id WHERE i.run_id = ?",
                    (normalized_run,),
                ).fetchone()
                observed = InventoryCounts(
                    counts["discovered"],
                    counts["probed"] or 0,
                    counts["usable"] or 0,
                    counts["issues"] or 0,
                    counts["ignored"] or 0,
                )
                if observed != current.counts:
                    raise ProjectError(
                        "inventory_incomplete",
                        "素材檢查結果尚未整理完成。",
                        status_code=409,
                    )
                now = utc_now()
                connection.execute(
                    "UPDATE inventory_runs SET state = 'succeeded', phase = 'completed', "
                    "error_code = NULL, completed_at = ? WHERE run_id = ?",
                    (now, normalized_run),
                )
                connection.execute(
                    "INSERT INTO current_inventory (slot, run_id) VALUES (1, ?) "
                    "ON CONFLICT(slot) DO UPDATE SET run_id = excluded.run_id",
                    (normalized_run,),
                )
                published = self._read_run(connection, normalized_run)
                connection.commit()
                return published
            except ProjectError:
                connection.rollback()
                raise
            except sqlite3.Error as error:
                connection.rollback()
                raise ProjectError(
                    "storage_failed", "Clipen 無法發布素材檢查結果。", status_code=500
                ) from error

    def stage_item(
        self,
        project_id: str,
        run_id: object,
        candidate: AssetCandidate,
    ) -> InventoryRun:
        validate_project_id(project_id)
        normalized_run = validate_run_id(run_id)
        asset_id = candidate.asset_id(project_id)
        version_id = candidate.version_id(project_id)
        with self.projects.connect(project_id) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                run = self._read_run(connection, normalized_run)
                if run.project_id != project_id or run.state is not InventoryState.RUNNING:
                    raise ProjectError(
                        "inventory_state_conflict",
                        "這次素材檢查目前不能保存結果。",
                        status_code=409,
                    )
                existing = connection.execute(
                    "SELECT 1 FROM inventory_items WHERE run_id = ? AND asset_id = ?",
                    (normalized_run, asset_id),
                ).fetchone()
                if existing is not None:
                    raise ProjectError(
                        "inventory_item_conflict",
                        "素材檢查出現重複項目。",
                        status_code=409,
                    )
                now = utc_now()
                connection.execute(
                    "INSERT INTO assets (asset_id, project_id, role, relative_path, first_seen_at) "
                    "VALUES (?, ?, ?, ?, ?) ON CONFLICT(asset_id) DO NOTHING",
                    (asset_id, project_id, candidate.role.value, candidate.relative_path, now),
                )
                if version_id is not None:
                    probe_json = (
                        None
                        if candidate.probe is None
                        else json.dumps(
                            candidate.probe,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                    )
                    connection.execute(
                        "INSERT INTO asset_versions "
                        "(version_id, asset_id, size_bytes, mtime_ns, fingerprint, "
                        "probe_status, probe_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(version_id) DO NOTHING",
                        (
                            version_id,
                            asset_id,
                            candidate.size_bytes,
                            candidate.mtime_ns,
                            candidate.fingerprint,
                            candidate.probe_status.value,
                            probe_json,
                            now,
                        ),
                    )
                    for stream in candidate.streams:
                        connection.execute(
                            "INSERT INTO media_streams "
                            "(version_id, stream_index, kind, codec, details_json) "
                            "VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                            (
                                version_id,
                                stream.stream_index,
                                stream.kind,
                                stream.codec,
                                json.dumps(
                                    stream.details,
                                    ensure_ascii=False,
                                    sort_keys=True,
                                    separators=(",", ":"),
                                ),
                            ),
                        )
                connection.execute(
                    "INSERT INTO inventory_items "
                    "(run_id, asset_id, version_id, display_name, kind, status, issue_code) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        normalized_run,
                        asset_id,
                        version_id,
                        candidate.display_name,
                        candidate.kind.value,
                        candidate.status.value,
                        candidate.issue_code,
                    ),
                )
                probed_increment = int(
                    candidate.probe_status is not None
                    and candidate.probe_status.value != "pending"
                )
                connection.execute(
                    "UPDATE inventory_runs SET discovered_count = discovered_count + 1, "
                    "probed_count = probed_count + ?, usable_count = usable_count + ?, "
                    "issue_count = issue_count + ?, ignored_count = ignored_count + ? "
                    "WHERE run_id = ?",
                    (
                        probed_increment,
                        int(candidate.status.value == "usable"),
                        int(candidate.status.value == "needs_attention"),
                        int(candidate.status.value == "ignored"),
                        normalized_run,
                    ),
                )
                updated = self._read_run(connection, normalized_run)
                connection.commit()
                return updated
            except ProjectError:
                connection.rollback()
                raise
            except (TypeError, ValueError) as error:
                connection.rollback()
                raise ProjectError(
                    "inventory_item_invalid", "Clipen 無法保存這個素材項目。", status_code=500
                ) from error
            except sqlite3.Error as error:
                connection.rollback()
                raise ProjectError(
                    "storage_failed", "Clipen 無法保存素材檢查結果。", status_code=500
                ) from error

    def interrupt_incomplete(self, project_id: str) -> int:
        validate_project_id(project_id)
        with self.projects.connect(project_id) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                now = utc_now()
                cursor = connection.execute(
                    "UPDATE inventory_runs SET state = 'interrupted', phase = 'interrupted', "
                    "error_code = 'controller_restarted', "
                    "started_at = COALESCE(started_at, ?), completed_at = ? "
                    "WHERE state IN ('queued', 'running', 'cancelling')",
                    (now, now),
                )
                connection.commit()
                return cursor.rowcount
            except sqlite3.Error as error:
                connection.rollback()
                raise ProjectError(
                    "storage_failed", "Clipen 無法恢復素材檢查。", status_code=500
                ) from error

    @staticmethod
    def _read_run(connection: sqlite3.Connection, run_id: str) -> InventoryRun:
        row = connection.execute(
            "SELECT run_id, project_id, project_revision, input_fingerprint, roots_json, "
            "state, phase, discovered_count, probed_count, usable_count, issue_count, "
            "ignored_count, error_code, created_at, started_at, completed_at "
            "FROM inventory_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise ProjectError("inventory_not_found", "找不到這次素材檢查。", status_code=404)
        try:
            roots_value = json.loads(row["roots_json"])
            if not isinstance(roots_value, list):
                raise ValueError("inventory roots must be a list")
            return InventoryRun(
                run_id=row["run_id"],
                project_id=row["project_id"],
                project_revision=row["project_revision"],
                input_fingerprint=row["input_fingerprint"],
                roots=tuple(InventoryRoot.from_storage(value) for value in roots_value),
                state=InventoryState(row["state"]),
                phase=row["phase"],
                counts=InventoryCounts(
                    row["discovered_count"],
                    row["probed_count"],
                    row["usable_count"],
                    row["issue_count"],
                    row["ignored_count"],
                ),
                error_code=row["error_code"],
                created_at=row["created_at"],
                started_at=row["started_at"],
                completed_at=row["completed_at"],
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            raise ProjectError(
                "storage_corrupt", "Clipen 無法讀取素材檢查紀錄。", status_code=500
            ) from error
