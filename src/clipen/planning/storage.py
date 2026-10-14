"""Durable planning runs and immutable editable Plan revision contents."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
import re
import sqlite3
import uuid
from collections.abc import Mapping

from clipen.domain import PlanRevision, PlanStatus
from clipen.domain.validation import require_identifier
from clipen.projects.models import (
    ProjectError,
    validate_project_id,
    validate_request_id,
    validate_revision,
)
from clipen.projects.storage import ProjectRepository, command_digest, utc_now
from clipen.providers.recovery import ProviderUsage

from .models import PlanningContext
from .proposal import PlanProposalError, resolve_plan_proposal
from .quality import assess_plan_quality


_RUN_ID = re.compile(r"planrun_[0-9a-f]{32}\Z")


class PlanningRunState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True)
class PlanningRun:
    run_id: str
    project_id: str
    project_revision: int
    editing_skill_id: str
    system_skill_release: str
    context_hash: str
    estimated_input_tokens: int
    state: PlanningRunState
    phase: str
    plan_id: str | None
    plan_revision: int | None
    usage: ProviderUsage | None
    error_code: str | None
    created_at: str
    started_at: str | None
    completed_at: str | None

    def to_public(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "project_revision": self.project_revision,
            "editing_skill_id": self.editing_skill_id,
            "estimated_input_tokens": self.estimated_input_tokens,
            "state": self.state.value,
            "phase": self.phase,
            "plan_id": self.plan_id,
            "plan_revision": self.plan_revision,
            "usage": None if self.usage is None else self.usage.to_dict(),
            "error_code": self.error_code,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }


class PlanningRepository:
    def __init__(self, projects: ProjectRepository) -> None:
        self.projects = projects

    def create_run(
        self,
        project_id: str,
        request_id: object,
        expected_revision: object,
        editing_skill_id: str,
        system_skill_release: str,
        context: PlanningContext,
        estimated_input_tokens: int | None = None,
    ) -> tuple[PlanningRun, bool]:
        validate_project_id(project_id)
        normalized_request = validate_request_id(request_id)
        revision = validate_revision(expected_revision)
        require_identifier(editing_skill_id, "editing_skill_id")
        require_identifier(system_skill_release, "system_skill_release")
        if context.project_id != project_id:
            raise ValueError("planning context belongs to another project")
        estimate = (
            context.estimated_input_tokens
            if estimated_input_tokens is None
            else estimated_input_tokens
        )
        if isinstance(estimate, bool) or not isinstance(estimate, int) or estimate < 1:
            raise ValueError("planning token estimate is invalid")
        command_hash = command_digest(
            {
                "project_revision": revision,
                "editing_skill_id": editing_skill_id,
                "system_skill_release": system_skill_release,
                "context_hash": context.content_hash,
                "estimated_input_tokens": estimate,
            }
        )
        run_id = "planrun_" + uuid.uuid4().hex
        now = utc_now()
        encoded_context = json.dumps(
            context.to_storage(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        try:
            with self.projects.connect(project_id) as database:
                database.execute("BEGIN IMMEDIATE")
                existing = database.execute(
                    "SELECT * FROM planning_runs WHERE request_id = ?",
                    (normalized_request,),
                ).fetchone()
                if existing is not None:
                    stored_hash = command_digest(
                        {
                            "project_revision": existing["project_revision"],
                            "editing_skill_id": existing["editing_skill_id"],
                            "system_skill_release": existing["system_skill_release"],
                            "context_hash": existing["context_hash"],
                            "estimated_input_tokens": existing["estimated_input_tokens"],
                        }
                    )
                    if stored_hash != command_hash:
                        raise ProjectError(
                            "request_conflict",
                            "這次產生計畫的操作與既有內容衝突。",
                            status_code=409,
                        )
                    database.rollback()
                    return self._run(existing), False
                current_revision = database.execute(
                    "SELECT revision FROM project WHERE project_id = ?", (project_id,)
                ).fetchone()[0]
                if current_revision != revision:
                    raise ProjectError(
                        "revision_conflict",
                        "作品內容已更新，請重新整理後再產生計畫。",
                        status_code=409,
                    )
                if database.execute(
                    "SELECT 1 FROM planning_runs WHERE state IN ('queued', 'running')"
                ).fetchone():
                    raise ProjectError(
                        "planning_busy", "目前已有剪輯計畫正在產生。", status_code=409
                    )
                current_plan = database.execute(
                    "SELECT revisions.* FROM current_plan AS current "
                    "JOIN plan_revisions AS revisions "
                    "ON revisions.plan_id=current.plan_id "
                    "AND revisions.revision=current.revision WHERE current.slot=1"
                ).fetchone()
                if current_plan is not None:
                    previous = PlanRevision.from_payload(
                        json.loads(current_plan["payload_json"])
                    )
                    if previous.status not in {
                        PlanStatus.READY_FOR_REVIEW,
                        PlanStatus.CONFIRMED,
                    }:
                        raise ProjectError(
                            "planning_execution_active",
                            "目前的剪輯版本仍在執行或檢查，不能重新產生計畫。",
                            status_code=409,
                        )
                    retired = previous.transition(PlanStatus.SUPERSEDED)
                    database.execute(
                        "UPDATE plan_revisions SET status=?, payload_json=? "
                        "WHERE plan_id=? AND revision=?",
                        (
                            retired.status.value,
                            self._encoded_plan(retired),
                            retired.plan_id,
                            retired.revision,
                        ),
                    )
                    database.execute("DELETE FROM current_plan WHERE slot=1")
                database.execute(
                    "INSERT INTO planning_runs "
                    "(run_id, project_id, request_id, project_revision, editing_skill_id, "
                    "system_skill_release, context_hash, context_json, "
                    "estimated_input_tokens, state, phase, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', 'queued', ?)",
                    (
                        run_id,
                        project_id,
                        normalized_request,
                        revision,
                        editing_skill_id,
                        system_skill_release,
                        context.content_hash,
                        encoded_context,
                        estimate,
                        now,
                    ),
                )
                row = database.execute(
                    "SELECT * FROM planning_runs WHERE run_id = ?", (run_id,)
                ).fetchone()
                database.commit()
                return self._run(row), True
        except ProjectError:
            raise
        except (json.JSONDecodeError, TypeError, ValueError) as error:
            raise ProjectError(
                "storage_corrupt", "Clipen 無法讀取既有剪輯計畫。", status_code=500
            ) from error
        except sqlite3.Error as error:
            raise ProjectError(
                "storage_failed", "Clipen 無法建立剪輯計畫工作。", status_code=500
            ) from error

    def context(self, project_id: str, run_id: str) -> PlanningContext:
        row = self._get_row(project_id, run_id)
        try:
            return self._stored_context(row)
        except (json.JSONDecodeError, TypeError, ValueError) as error:
            raise ProjectError(
                "storage_corrupt", "Clipen 無法讀取剪輯計畫輸入。", status_code=500
            ) from error

    def get_run(self, project_id: str, run_id: str) -> PlanningRun:
        return self._run(self._get_row(project_id, run_id))

    def latest_run(self, project_id: str) -> PlanningRun | None:
        validate_project_id(project_id)
        with self.projects.connect(project_id) as database:
            row = database.execute(
                "SELECT * FROM planning_runs ORDER BY created_at DESC, rowid DESC LIMIT 1"
            ).fetchone()
        return None if row is None else self._run(row)

    def mark_running(self, project_id: str, run_id: str) -> PlanningRun:
        now = utc_now()
        with self.projects.connect(project_id) as database:
            database.execute("BEGIN IMMEDIATE")
            row = self._get_row_in(database, run_id)
            if row["state"] != PlanningRunState.QUEUED.value:
                raise ProjectError(
                    "planning_state_conflict",
                    "這次剪輯計畫目前不能開始。",
                    status_code=409,
                )
            database.execute(
                "UPDATE planning_runs SET state='running', phase='provider', started_at=? "
                "WHERE run_id=?",
                (now, run_id),
            )
            updated = database.execute(
                "SELECT * FROM planning_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            database.commit()
        return self._run(updated)

    def request_cancel(self, project_id: str, run_id: str) -> PlanningRun:
        """Cancel queued work immediately; running work stops through its token."""

        with self.projects.connect(project_id) as database:
            database.execute("BEGIN IMMEDIATE")
            existing = self._get_row_in(database, run_id)
            if existing["state"] == PlanningRunState.QUEUED.value:
                now = utc_now()
                database.execute(
                    "UPDATE planning_runs SET state='cancelled', phase='terminal', "
                    "error_code='user_cancelled', completed_at=? WHERE run_id=?",
                    (now, run_id),
                )
                existing = database.execute(
                    "SELECT * FROM planning_runs WHERE run_id=?", (run_id,)
                ).fetchone()
            elif existing["state"] == PlanningRunState.RUNNING.value:
                database.execute(
                    "UPDATE planning_runs SET phase='cancelling' WHERE run_id=?",
                    (run_id,),
                )
                existing = database.execute(
                    "SELECT * FROM planning_runs WHERE run_id=?", (run_id,)
                ).fetchone()
            database.commit()
        return self._run(existing)

    def publish(
        self,
        project_id: str,
        run_id: str,
        plan: PlanRevision,
        usage: ProviderUsage | None,
    ) -> PlanningRun:
        if plan.project_id != project_id or plan.status is not PlanStatus.READY_FOR_REVIEW:
            raise ValueError("published plan identity or status is invalid")
        payload = json.dumps(
            plan.to_payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        now = utc_now()
        usage_json = (
            None
            if usage is None
            else json.dumps(usage.to_dict(), separators=(",", ":"))
        )
        with self.projects.connect(project_id) as database:
            try:
                database.execute("BEGIN IMMEDIATE")
                run = self._get_row_in(database, run_id)
                if run["state"] != PlanningRunState.RUNNING.value:
                    raise ProjectError(
                        "planning_state_conflict",
                        "這次剪輯計畫目前不能保存結果。",
                        status_code=409,
                    )
                context = self._stored_context(run)
                if (
                    plan.material_snapshot_id != context.material_snapshot_id
                    or plan.analysis_snapshot_id != context.analysis_snapshot_id
                    or plan.system_skill_release != run["system_skill_release"]
                ):
                    raise ProjectError(
                        "planning_snapshot_conflict",
                        "剪輯計畫與輸入快照不一致。",
                        status_code=409,
                    )
                previous_row = database.execute(
                    "SELECT revisions.* FROM current_plan AS current "
                    "JOIN plan_revisions AS revisions "
                    "ON revisions.plan_id=current.plan_id "
                    "AND revisions.revision=current.revision WHERE current.slot=1"
                ).fetchone()
                if previous_row is not None:
                    previous = PlanRevision.from_payload(
                        json.loads(previous_row["payload_json"])
                    )
                    if previous.status in {
                        PlanStatus.READY_FOR_REVIEW,
                        PlanStatus.CONFIRMED,
                    }:
                        previous = previous.transition(PlanStatus.SUPERSEDED)
                        database.execute(
                            "UPDATE plan_revisions SET status=?, payload_json=? "
                            "WHERE plan_id=? AND revision=?",
                            (
                                previous.status.value,
                                self._encoded_plan(previous),
                                previous.plan_id,
                                previous.revision,
                            ),
                        )
                    elif previous.status in {
                        PlanStatus.EXECUTING,
                        PlanStatus.REVIEWED,
                    }:
                        raise ProjectError(
                            "planning_execution_active",
                            "目前的剪輯版本仍在執行或檢查，不能取代計畫。",
                            status_code=409,
                        )
                    elif previous.status is PlanStatus.SUPERSEDED:
                        raise ValueError("current plan is already superseded")
                database.execute(
                    "INSERT INTO plan_revisions "
                    "(plan_id, revision, project_id, status, content_hash, "
                    "material_snapshot_id, analysis_snapshot_id, system_skill_release, "
                    "payload_json, source_run_id, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        plan.plan_id,
                        plan.revision,
                        project_id,
                        plan.status.value,
                        plan.content_hash,
                        plan.material_snapshot_id,
                        plan.analysis_snapshot_id,
                        plan.system_skill_release,
                        payload,
                        run_id,
                        now,
                    ),
                )
                database.execute(
                    "INSERT INTO current_plan(slot, plan_id, revision) VALUES(1, ?, ?) "
                    "ON CONFLICT(slot) DO UPDATE SET "
                    "plan_id=excluded.plan_id, revision=excluded.revision",
                    (plan.plan_id, plan.revision),
                )
                database.execute(
                    "UPDATE planning_runs SET state='succeeded', phase='completed', "
                    "plan_id=?, plan_revision=?, usage_json=?, error_code=NULL, completed_at=? "
                    "WHERE run_id=?",
                    (plan.plan_id, plan.revision, usage_json, now, run_id),
                )
                updated = database.execute(
                    "SELECT * FROM planning_runs WHERE run_id=?", (run_id,)
                ).fetchone()
                database.commit()
                return self._run(updated)
            except ProjectError:
                database.rollback()
                raise
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
                database.rollback()
                raise ProjectError(
                    "storage_corrupt",
                    "Clipen 無法讀取剪輯計畫輸入。",
                    status_code=500,
                ) from error
            except sqlite3.Error as error:
                database.rollback()
                raise ProjectError(
                    "storage_failed", "Clipen 無法保存剪輯計畫。", status_code=500
                ) from error

    def mark_terminal(
        self,
        project_id: str,
        run_id: str,
        state: PlanningRunState,
        error_code: str,
        usage: ProviderUsage | None = None,
    ) -> PlanningRun:
        if state not in {
            PlanningRunState.FAILED,
            PlanningRunState.CANCELLED,
            PlanningRunState.INTERRUPTED,
        }:
            raise ValueError("planning terminal state is invalid")
        now = utc_now()
        require_identifier(error_code, "error_code")
        usage_json = (
            None
            if usage is None
            else json.dumps(usage.to_dict(), separators=(",", ":"))
        )
        with self.projects.connect(project_id) as database:
            database.execute("BEGIN IMMEDIATE")
            existing = self._get_row_in(database, run_id)
            if existing["state"] not in {
                PlanningRunState.QUEUED.value,
                PlanningRunState.RUNNING.value,
            }:
                raise ProjectError(
                    "planning_state_conflict",
                    "這次剪輯計畫已經結束。",
                    status_code=409,
                )
            database.execute(
                "UPDATE planning_runs SET state=?, phase='terminal', usage_json=?, "
                "error_code=?, completed_at=? WHERE run_id=?",
                (state.value, usage_json, error_code, now, run_id),
            )
            updated = database.execute(
                "SELECT * FROM planning_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            database.commit()
        return self._run(updated)

    def current_plan(self, project_id: str) -> PlanRevision | None:
        validate_project_id(project_id)
        with self.projects.connect(project_id) as database:
            row = database.execute(
                "SELECT revisions.payload_json FROM current_plan AS current "
                "JOIN plan_revisions AS revisions ON revisions.plan_id=current.plan_id "
                "AND revisions.revision=current.revision WHERE current.slot=1"
            ).fetchone()
        if row is None:
            return None
        try:
            return PlanRevision.from_payload(json.loads(row["payload_json"]))
        except (json.JSONDecodeError, TypeError, ValueError) as error:
            raise ProjectError(
                "storage_corrupt", "Clipen 無法讀取剪輯計畫。", status_code=500
            ) from error

    def execution_input(
        self, project_id: str
    ) -> tuple[PlanRevision, PlanningContext]:
        """Return the confirmed plan and the immutable context that resolved it."""

        validate_project_id(project_id)
        try:
            with self.projects.connect(project_id) as database:
                row = self._current_plan_row(database)
                plan = PlanRevision.from_payload(json.loads(row["payload_json"]))
                if plan.status is not PlanStatus.CONFIRMED:
                    raise ProjectError(
                        "plan_not_confirmed",
                        "請先確認一份剪輯計畫。",
                        status_code=409,
                    )
                source_run_id = row["source_run_id"]
                if not isinstance(source_run_id, str):
                    raise ValueError("plan source run is missing")
                context = self._stored_context(
                    self._get_row_in(database, source_run_id)
                )
                if (
                    context.project_id != project_id
                    or context.material_snapshot_id != plan.material_snapshot_id
                    or context.analysis_snapshot_id != plan.analysis_snapshot_id
                ):
                    raise ValueError("plan context does not match the confirmed plan")
                return plan, context
        except ProjectError:
            raise
        except (json.JSONDecodeError, TypeError, ValueError) as error:
            raise ProjectError(
                "storage_corrupt",
                "Clipen 無法讀取已確認的剪輯計畫。",
                status_code=500,
            ) from error

    def update_plan(
        self,
        project_id: str,
        request_id: object,
        expected_plan_id: str,
        expected_revision: object,
        editable: Mapping[str, object],
    ) -> tuple[PlanRevision, bool]:
        """Create a revision using only candidates from the original run context."""

        validate_project_id(project_id)
        normalized_request = validate_request_id(request_id)
        require_identifier(expected_plan_id, "expected_plan_id")
        revision = validate_revision(expected_revision)
        if not isinstance(editable, Mapping):
            raise ProjectError(
                "plan_content_invalid", "剪輯計畫內容格式無效。", status_code=422
            )
        request_hash = command_digest(
            {
                "plan_id": expected_plan_id,
                "revision": revision,
                "editable": editable,
            }
        )
        try:
            with self.projects.connect(project_id) as database:
                database.execute("BEGIN IMMEDIATE")
                replay = self._command_replay(
                    database, normalized_request, "update_plan", request_hash
                )
                if replay is not None:
                    database.rollback()
                    return replay, False
                current_row = self._current_plan_row(database)
                current = PlanRevision.from_payload(
                    json.loads(current_row["payload_json"])
                )
                if current.plan_id != expected_plan_id or current.revision != revision:
                    raise ProjectError(
                        "plan_revision_conflict",
                        "剪輯計畫已更新，請重新整理後再修改。",
                        status_code=409,
                    )
                if current.status not in {
                    PlanStatus.READY_FOR_REVIEW,
                    PlanStatus.CONFIRMED,
                }:
                    raise ProjectError(
                        "plan_not_editable",
                        "這個剪輯計畫目前不能修改。",
                        status_code=409,
                    )
                source_run_id = current_row["source_run_id"]
                if source_run_id is None:
                    raise ValueError("plan source run is missing")
                context = self._stored_context(
                    self._get_row_in(database, source_run_id)
                )
                updated = resolve_plan_proposal(
                    editable,
                    context,
                    plan_id=current.plan_id,
                    revision=current.revision + 1,
                    system_skill_release=current.system_skill_release,
                )
                if updated.content_hash == current.content_hash:
                    raise ProjectError(
                        "plan_unchanged",
                        "剪輯計畫沒有需要儲存的變更。",
                        status_code=409,
                    )
                superseded = current.transition(PlanStatus.SUPERSEDED)
                database.execute(
                    "UPDATE plan_revisions SET status=?, payload_json=? "
                    "WHERE plan_id=? AND revision=?",
                    (
                        superseded.status.value,
                        self._encoded_plan(superseded),
                        current.plan_id,
                        current.revision,
                    ),
                )
                now = utc_now()
                database.execute(
                    "INSERT INTO plan_revisions "
                    "(plan_id, revision, project_id, status, content_hash, "
                    "material_snapshot_id, analysis_snapshot_id, system_skill_release, "
                    "payload_json, source_run_id, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        updated.plan_id,
                        updated.revision,
                        project_id,
                        updated.status.value,
                        updated.content_hash,
                        updated.material_snapshot_id,
                        updated.analysis_snapshot_id,
                        updated.system_skill_release,
                        self._encoded_plan(updated),
                        source_run_id,
                        now,
                    ),
                )
                database.execute(
                    "UPDATE current_plan SET revision=? WHERE slot=1",
                    (updated.revision,),
                )
                self._save_command_receipt(
                    database,
                    normalized_request,
                    "update_plan",
                    request_hash,
                    updated,
                    now,
                )
                database.commit()
                return updated, True
        except ProjectError:
            raise
        except PlanProposalError as error:
            raise ProjectError(
                "plan_content_invalid",
                "剪輯計畫引用了無效或重複的素材片段。",
                status_code=422,
            ) from error
        except (json.JSONDecodeError, TypeError, ValueError) as error:
            raise ProjectError(
                "storage_corrupt", "Clipen 無法讀取剪輯計畫。", status_code=500
            ) from error
        except sqlite3.Error as error:
            raise ProjectError(
                "storage_failed", "Clipen 無法儲存剪輯計畫。", status_code=500
            ) from error

    def confirm_plan(
        self,
        project_id: str,
        request_id: object,
        expected_plan_id: str,
        expected_revision: object,
    ) -> tuple[PlanRevision, bool]:
        validate_project_id(project_id)
        normalized_request = validate_request_id(request_id)
        require_identifier(expected_plan_id, "expected_plan_id")
        revision = validate_revision(expected_revision)
        request_hash = command_digest(
            {"plan_id": expected_plan_id, "revision": revision}
        )
        try:
            with self.projects.connect(project_id) as database:
                database.execute("BEGIN IMMEDIATE")
                replay = self._command_replay(
                    database, normalized_request, "confirm_plan", request_hash
                )
                if replay is not None:
                    database.rollback()
                    return replay, False
                current_row = self._current_plan_row(database)
                current = PlanRevision.from_payload(
                    json.loads(current_row["payload_json"])
                )
                if current.plan_id != expected_plan_id or current.revision != revision:
                    raise ProjectError(
                        "plan_revision_conflict",
                        "剪輯計畫已更新，請重新整理後再確認。",
                        status_code=409,
                    )
                if current.status is not PlanStatus.READY_FOR_REVIEW:
                    raise ProjectError(
                        "plan_not_confirmable",
                        "這個剪輯計畫目前不能確認。",
                        status_code=409,
                    )
                source_run_id = current_row["source_run_id"]
                if not isinstance(source_run_id, str):
                    raise ValueError("plan source run is missing")
                context = self._stored_context(
                    self._get_row_in(database, source_run_id)
                )
                quality = assess_plan_quality(current, context)
                if quality.issue_code == "plan_video_coverage_too_short":
                    raise ProjectError(
                        quality.issue_code,
                        "所選影像長度遠低於你要求的成片長度，請增加影像片段或重新產生計畫。",
                        status_code=422,
                    )
                confirmed = current.transition(PlanStatus.CONFIRMED)
                now = utc_now()
                database.execute(
                    "UPDATE plan_revisions SET status=?, payload_json=? "
                    "WHERE plan_id=? AND revision=?",
                    (
                        confirmed.status.value,
                        self._encoded_plan(confirmed),
                        confirmed.plan_id,
                        confirmed.revision,
                    ),
                )
                self._save_command_receipt(
                    database,
                    normalized_request,
                    "confirm_plan",
                    request_hash,
                    confirmed,
                    now,
                )
                database.commit()
                return confirmed, True
        except ProjectError:
            raise
        except (json.JSONDecodeError, TypeError, ValueError) as error:
            raise ProjectError(
                "storage_corrupt", "Clipen 無法讀取剪輯計畫。", status_code=500
            ) from error
        except sqlite3.Error as error:
            raise ProjectError(
                "storage_failed", "Clipen 無法確認剪輯計畫。", status_code=500
            ) from error

    def interrupt_incomplete(self, project_id: str) -> int:
        validate_project_id(project_id)
        with self.projects.connect(project_id) as database:
            cursor = database.execute(
                "UPDATE planning_runs SET state='interrupted', phase='terminal', "
                "error_code='controller_stopped', completed_at=? "
                "WHERE state IN ('queued','running')",
                (utc_now(),),
            )
            return cursor.rowcount

    @staticmethod
    def _encoded_plan(plan: PlanRevision) -> str:
        return json.dumps(
            plan.to_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _current_plan_row(database: sqlite3.Connection):
        row = database.execute(
            "SELECT revisions.* FROM current_plan AS current "
            "JOIN plan_revisions AS revisions ON revisions.plan_id=current.plan_id "
            "AND revisions.revision=current.revision WHERE current.slot=1"
        ).fetchone()
        if row is None:
            raise ProjectError(
                "plan_not_found", "尚未建立可編輯的剪輯計畫。", status_code=404
            )
        return row

    @staticmethod
    def _command_replay(
        database: sqlite3.Connection,
        request_id: str,
        command_type: str,
        request_hash: str,
    ) -> PlanRevision | None:
        row = database.execute(
            "SELECT command_type, request_hash, response_json "
            "FROM plan_command_receipts WHERE request_id=?",
            (request_id,),
        ).fetchone()
        if row is None:
            return None
        if row["command_type"] != command_type or row["request_hash"] != request_hash:
            raise ProjectError(
                "request_conflict",
                "這次剪輯計畫操作與既有內容衝突。",
                status_code=409,
            )
        return PlanRevision.from_payload(json.loads(row["response_json"]))

    @staticmethod
    def _save_command_receipt(
        database: sqlite3.Connection,
        request_id: str,
        command_type: str,
        request_hash: str,
        plan: PlanRevision,
        created_at: str,
    ) -> None:
        database.execute(
            "INSERT INTO plan_command_receipts "
            "(request_id, command_type, request_hash, response_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                request_id,
                command_type,
                request_hash,
                PlanningRepository._encoded_plan(plan),
                created_at,
            ),
        )

    def _get_row(self, project_id: str, run_id: str):
        validate_project_id(project_id)
        if not isinstance(run_id, str) or _RUN_ID.fullmatch(run_id) is None:
            raise ProjectError("planning_not_found", "找不到這次剪輯計畫。", status_code=404)
        with self.projects.connect(project_id) as database:
            return self._get_row_in(database, run_id)

    @staticmethod
    def _get_row_in(database: sqlite3.Connection, run_id: str):
        if not isinstance(run_id, str) or _RUN_ID.fullmatch(run_id) is None:
            raise ProjectError("planning_not_found", "找不到這次剪輯計畫。", status_code=404)
        row = database.execute(
            "SELECT * FROM planning_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise ProjectError("planning_not_found", "找不到這次剪輯計畫。", status_code=404)
        return row

    @staticmethod
    def _run(row) -> PlanningRun:
        try:
            usage = (
                None
                if row["usage_json"] is None
                else ProviderUsage.from_dict(json.loads(row["usage_json"]))
            )
            return PlanningRun(
                row["run_id"],
                row["project_id"],
                row["project_revision"],
                row["editing_skill_id"],
                row["system_skill_release"],
                row["context_hash"],
                row["estimated_input_tokens"],
                PlanningRunState(row["state"]),
                row["phase"],
                row["plan_id"],
                row["plan_revision"],
                usage,
                row["error_code"],
                row["created_at"],
                row["started_at"],
                row["completed_at"],
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            raise ProjectError(
                "storage_corrupt", "Clipen 無法讀取剪輯計畫狀態。", status_code=500
            ) from error

    @staticmethod
    def _stored_context(row) -> PlanningContext:
        context = PlanningContext.from_storage(json.loads(row["context_json"]))
        if context.content_hash != row["context_hash"]:
            raise ValueError("planning context hash mismatch")
        return context
