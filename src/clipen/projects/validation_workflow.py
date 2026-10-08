"""Persistence and execution gating for timeline validation reports."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import sqlite3
from typing import Any, Mapping

from clipen.domain import ValidationReport, ValidationStatus

from .models import ProjectError, validate_request_id
from .storage import ProjectRepository, command_digest, utc_now


def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _report_digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TimelineValidationState:
    """Public workflow state derived from a report and its exact acceptance."""

    state: str
    can_execute: bool
    report: Mapping[str, Any] | None = None
    warnings_accepted: bool = False
    accepted_at: str | None = None

    def to_public(self) -> dict[str, object]:
        return {
            "state": self.state,
            "can_execute": self.can_execute,
            "report": None if self.report is None else dict(self.report),
            "warnings_accepted": self.warnings_accepted,
            "accepted_at": self.accepted_at,
        }


class TimelineValidationRepository:
    """Store trusted reports and explicit acceptance without weakening the gate."""

    def __init__(self, projects: ProjectRepository) -> None:
        self.projects = projects

    def publish(self, project_id: str, report: ValidationReport) -> TimelineValidationState:
        if not isinstance(report, ValidationReport):
            raise TypeError("report must be a ValidationReport")
        payload = report.to_payload()
        encoded = _canonical_json(payload)
        digest = _report_digest(payload)
        now = utc_now()
        try:
            with self.projects.connect(project_id) as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT report_digest FROM timeline_validation_reports WHERE report_id = ?",
                    (report.report_id,),
                ).fetchone()
                if existing is not None and existing["report_digest"] != digest:
                    raise ProjectError(
                        "validation_report_conflict",
                        "這份剪輯檢查紀錄與既有內容衝突。",
                        status_code=409,
                    )
                if existing is None:
                    connection.execute(
                        "INSERT INTO timeline_validation_reports "
                        "(report_id, project_id, timeline_id, capability_profile_id, "
                        "capability_content_hash, report_digest, status, report_json, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            report.report_id,
                            project_id,
                            report.timeline_id,
                            report.capability_profile_id,
                            report.capability_content_hash,
                            digest,
                            report.status.value,
                            encoded,
                            now,
                        ),
                    )
                connection.execute(
                    "INSERT INTO current_timeline_validation (slot, report_id) VALUES (1, ?) "
                    "ON CONFLICT(slot) DO UPDATE SET report_id = excluded.report_id",
                    (report.report_id,),
                )
                connection.commit()
            return self.get_state(project_id)
        except ProjectError:
            raise
        except sqlite3.Error as error:
            raise ProjectError(
                "storage_failed", "Clipen 無法保存剪輯檢查結果。", status_code=500
            ) from error

    def get_state(self, project_id: str) -> TimelineValidationState:
        try:
            with self.projects.connect(project_id) as connection:
                row = connection.execute(
                    "SELECT reports.report_id, reports.report_digest, reports.status, "
                    "reports.report_json, acceptance.accepted_at "
                    "FROM current_timeline_validation AS current "
                    "JOIN timeline_validation_reports AS reports "
                    "ON reports.report_id = current.report_id "
                    "LEFT JOIN timeline_warning_acceptances AS acceptance "
                    "ON acceptance.report_id = reports.report_id "
                    "AND acceptance.report_digest = reports.report_digest "
                    "WHERE current.slot = 1 "
                    "ORDER BY acceptance.accepted_at DESC LIMIT 1"
                ).fetchone()
        except sqlite3.Error as error:
            raise ProjectError(
                "storage_corrupt", "Clipen 無法讀取剪輯檢查結果。", status_code=500
            ) from error
        if row is None:
            return TimelineValidationState("not_ready", False)
        try:
            payload = json.loads(row["report_json"])
            if not isinstance(payload, dict):
                raise ValueError("report payload is not an object")
            status = ValidationStatus(row["status"])
            if (
                _report_digest(payload) != row["report_digest"]
                or payload.get("report_id") != row["report_id"]
                or payload.get("status") != status.value
            ):
                raise ValueError("report integrity check failed")
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            raise ProjectError(
                "storage_corrupt", "Clipen 無法讀取剪輯檢查結果。", status_code=500
            ) from error
        accepted_at = row["accepted_at"]
        accepted = status is ValidationStatus.WARNING and accepted_at is not None
        state = {
            ValidationStatus.PASS: "ready",
            ValidationStatus.WARNING: "ready" if accepted else "needs_confirmation",
            ValidationStatus.BLOCKING: "blocked",
        }[status]
        return TimelineValidationState(
            state,
            status is ValidationStatus.PASS or accepted,
            payload,
            accepted,
            accepted_at,
        )

    def accept_current_warnings(
        self, project_id: str, request_id: object, report_id: object
    ) -> TimelineValidationState:
        normalized_request = validate_request_id(request_id)
        if not isinstance(report_id, str):
            raise ProjectError(
                "validation_report_invalid", "剪輯檢查版本無效。", status_code=422
            )
        request_hash = command_digest({"report_id": report_id})
        now = utc_now()
        try:
            with self.projects.connect(project_id) as connection:
                connection.execute("BEGIN IMMEDIATE")
                current = connection.execute(
                    "SELECT reports.report_id, reports.report_digest, reports.status "
                    "FROM current_timeline_validation AS current "
                    "JOIN timeline_validation_reports AS reports "
                    "ON reports.report_id = current.report_id WHERE current.slot = 1"
                ).fetchone()
                if current is None:
                    raise ProjectError(
                        "validation_not_ready",
                        "尚未有可確認的剪輯檢查結果。",
                        status_code=409,
                    )
                receipt = connection.execute(
                    "SELECT report_id, request_hash FROM timeline_warning_acceptances "
                    "WHERE request_id = ?",
                    (normalized_request,),
                ).fetchone()
                if receipt is not None:
                    if receipt["request_hash"] != request_hash:
                        raise ProjectError(
                            "request_reused",
                            "這次操作的識別資訊已用於不同內容。",
                            status_code=409,
                        )
                    if current["report_id"] != report_id:
                        raise ProjectError(
                            "validation_report_stale",
                            "剪輯檢查結果已更新，請先查看最新內容。",
                            status_code=409,
                        )
                    connection.rollback()
                    return self.get_state(project_id)
                if current["report_id"] != report_id:
                    raise ProjectError(
                        "validation_report_stale",
                        "剪輯檢查結果已更新，請先查看最新內容。",
                        status_code=409,
                    )
                if current["status"] == ValidationStatus.BLOCKING.value:
                    raise ProjectError(
                        "validation_blocked",
                        "阻擋項目修正前不能開始剪輯。",
                        status_code=409,
                    )
                if current["status"] != ValidationStatus.WARNING.value:
                    raise ProjectError(
                        "warnings_not_required",
                        "這份剪輯檢查不需要接受警告。",
                        status_code=409,
                    )
                connection.execute(
                    "INSERT INTO timeline_warning_acceptances "
                    "(request_id, report_id, report_digest, request_hash, accepted_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        normalized_request,
                        report_id,
                        current["report_digest"],
                        request_hash,
                        now,
                    ),
                )
                connection.commit()
            return self.get_state(project_id)
        except ProjectError:
            raise
        except sqlite3.Error as error:
            raise ProjectError(
                "storage_failed", "Clipen 無法保存你的確認。", status_code=500
            ) from error
