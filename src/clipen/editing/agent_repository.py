"""Crash-safe persistence for bounded AI editing sessions."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Mapping

from clipen.domain import EditTimeline
from clipen.projects import ProjectError, ProjectRepository
from clipen.projects.models import validate_project_id, validate_request_id
from clipen.projects.storage import command_digest, utc_now

from .agent_models import (
    AgentRevision,
    AgentRun,
    REVISION_ID_PATTERN,
    RUN_ID_PATTERN,
    bounded_text,
    encoded,
    make_id,
)


class EditAgentRepository:
    """Persist every agent mutation as an immutable revision and append-only event."""

    def __init__(self, projects: ProjectRepository) -> None:
        self.projects = projects

    def create_run(
        self,
        project_id: str,
        request_id: object,
        *,
        provider: str = "codex",
        max_tool_calls: int = 80,
        max_preview_renders: int = 4,
    ) -> tuple[AgentRun, bool]:
        validate_project_id(project_id)
        request = validate_request_id(request_id)
        if isinstance(max_tool_calls, bool) or not 1 <= max_tool_calls <= 500:
            raise ValueError("max_tool_calls is invalid")
        if isinstance(max_preview_renders, bool) or not 0 <= max_preview_renders <= 20:
            raise ValueError("max_preview_renders is invalid")
        request_hash = command_digest(
            {
                "provider": provider,
                "max_tool_calls": max_tool_calls,
                "max_preview_renders": max_preview_renders,
            }
        )
        if provider not in {"codex", "claude", "gemini"}:
            raise ValueError("provider is invalid")
        run_id = make_id("editrun_", [project_id, request])
        now = utc_now()
        with self.projects.connect(project_id) as database:
            try:
                database.execute("BEGIN IMMEDIATE")
                receipt = database.execute(
                    "SELECT command_type,request_hash,response_json "
                    "FROM edit_agent_command_receipts WHERE request_id=?",
                    (request,),
                ).fetchone()
                if receipt is not None:
                    if receipt["command_type"] != "start" or receipt["request_hash"] != request_hash:
                        raise ProjectError(
                            "request_reused",
                            "這次操作的識別資訊已用於不同內容。",
                            status_code=409,
                        )
                    replay_id = json.loads(receipt["response_json"])["run_id"]
                    result = self._read_run(database, replay_id)
                    database.rollback()
                    return result, False
                base = database.execute(
                    "SELECT timelines.* FROM current_timeline current "
                    "JOIN edit_timelines timelines ON timelines.timeline_id=current.timeline_id "
                    "WHERE current.slot=1"
                ).fetchone()
                if base is None:
                    raise ProjectError(
                        "timeline_required",
                        "請先建立可執行的剪輯版本。",
                        status_code=409,
                    )
                busy = database.execute(
                    "SELECT run_id FROM edit_agent_runs "
                    "WHERE state IN ('queued','running') LIMIT 1"
                ).fetchone()
                if busy is not None:
                    raise ProjectError(
                        "edit_agent_busy",
                        "已有一個 AI 剪輯工作正在進行。",
                        status_code=409,
                    )
                payload = json.loads(base["payload_json"])
                timeline = EditTimeline.from_payload(payload)
                digest = hashlib.sha256(encoded(payload).encode("utf-8")).hexdigest()
                revision_id = make_id("editrev_", [run_id, 0, digest])
                database.execute(
                    "INSERT INTO edit_agent_runs "
                    "(run_id,project_id,request_id,base_timeline_id,state,phase,"
                    "max_tool_calls,max_preview_renders,current_revision_id,provider,created_at) "
                    "VALUES(?,?,?,?,'queued','ready',?,?,?,?,?)",
                    (
                        run_id,
                        project_id,
                        request,
                        timeline.timeline_id,
                        max_tool_calls,
                        max_preview_renders,
                        revision_id,
                        provider,
                        now,
                    ),
                )
                database.execute(
                    "INSERT INTO edit_agent_revisions "
                    "(revision_id,run_id,ordinal,parent_revision_id,payload_digest,"
                    "payload_json,summary,created_at) VALUES(?,?,0,NULL,?,?,?,?)",
                    (revision_id, run_id, digest, encoded(payload), "原始剪輯版本", now),
                )
                self._append_event(database, run_id, "run_created", {"revision_id": revision_id}, now)
                database.execute(
                    "INSERT INTO edit_agent_command_receipts VALUES(?,?,?,?,?)",
                    (request, "start", request_hash, encoded({"run_id": run_id}), now),
                )
                result = self._read_run(database, run_id)
                database.commit()
                return result, True
            except ProjectError:
                database.rollback()
                raise
            except (json.JSONDecodeError, KeyError, TypeError, ValueError, sqlite3.Error) as error:
                database.rollback()
                raise ProjectError(
                    "edit_agent_storage_failed",
                    "Clipen 無法建立 AI 剪輯工作。",
                    status_code=500,
                ) from error

    def activate(self, project_id: str, run_id: object) -> AgentRun:
        normalized = self._validate_run_id(run_id)
        with self.projects.connect(project_id) as database:
            database.execute("BEGIN IMMEDIATE")
            run = self._read_run(database, normalized)
            if run.state == "queued":
                now = utc_now()
                database.execute(
                    "UPDATE edit_agent_runs SET state='running',phase='editing',started_at=? "
                    "WHERE run_id=?",
                    (now, normalized),
                )
                self._append_event(database, normalized, "run_started", {}, now)
            elif run.state != "running":
                database.rollback()
                raise ProjectError(
                    "edit_agent_finished",
                    "這次 AI 剪輯工作已經結束。",
                    status_code=409,
                )
            result = self._read_run(database, normalized)
            database.commit()
            return result

    def consume_tool_call(self, project_id: str, run_id: object, tool_name: str) -> AgentRun:
        normalized = self._validate_run_id(run_id)
        bounded_text(tool_name, "工具名稱", 80)
        with self.projects.connect(project_id) as database:
            database.execute("BEGIN IMMEDIATE")
            run = self._read_run(database, normalized)
            if run.state != "running":
                database.rollback()
                raise ProjectError("edit_agent_not_running", "AI 剪輯工作並未執行。", status_code=409)
            if run.tool_calls >= run.max_tool_calls:
                now = utc_now()
                database.execute(
                    "UPDATE edit_agent_runs SET state='interrupted',phase='budget_exhausted',"
                    "error_code='tool_budget_exhausted',completed_at=? WHERE run_id=?",
                    (now, normalized),
                )
                self._append_event(database, normalized, "budget_exhausted", {}, now)
                database.commit()
                raise ProjectError(
                    "tool_budget_exhausted",
                    "這次 AI 剪輯已達工具呼叫上限。",
                    status_code=409,
                )
            database.execute(
                "UPDATE edit_agent_runs SET tool_calls=tool_calls+1 WHERE run_id=?",
                (normalized,),
            )
            now = utc_now()
            self._append_event(database, normalized, "tool_called", {"tool": tool_name}, now)
            result = self._read_run(database, normalized)
            database.commit()
            return result

    def current_revision(self, project_id: str, run_id: object) -> AgentRevision:
        normalized = self._validate_run_id(run_id)
        with self.projects.connect(project_id) as database:
            run = self._read_run(database, normalized)
            return self._read_revision(database, run.current_revision_id)

    def save_revision(
        self,
        project_id: str,
        run_id: object,
        timeline: EditTimeline,
        summary: object,
    ) -> tuple[AgentRevision, bool]:
        normalized = self._validate_run_id(run_id)
        normalized_summary = bounded_text(summary, "修改摘要", 500)
        payload = timeline.to_payload()
        digest = hashlib.sha256(encoded(payload).encode("utf-8")).hexdigest()
        with self.projects.connect(project_id) as database:
            database.execute("BEGIN IMMEDIATE")
            run = self._read_run(database, normalized)
            if run.state != "running":
                database.rollback()
                raise ProjectError("edit_agent_not_running", "AI 剪輯工作並未執行。", status_code=409)
            existing = database.execute(
                "SELECT revision_id FROM edit_agent_revisions WHERE run_id=? AND payload_digest=?",
                (normalized, digest),
            ).fetchone()
            if existing is not None:
                database.execute(
                    "UPDATE edit_agent_runs SET current_revision_id=? WHERE run_id=?",
                    (existing["revision_id"], normalized),
                )
                result = self._read_revision(database, existing["revision_id"])
                database.commit()
                return result, False
            ordinal = database.execute(
                "SELECT COALESCE(MAX(ordinal),-1)+1 value FROM edit_agent_revisions WHERE run_id=?",
                (normalized,),
            ).fetchone()["value"]
            revision_id = make_id("editrev_", [normalized, ordinal, digest])
            now = utc_now()
            database.execute(
                "INSERT INTO edit_agent_revisions VALUES(?,?,?,?,?,?,?,?)",
                (
                    revision_id,
                    normalized,
                    ordinal,
                    run.current_revision_id,
                    digest,
                    encoded(payload),
                    normalized_summary,
                    now,
                ),
            )
            database.execute(
                "UPDATE edit_agent_runs SET current_revision_id=?,phase='editing' WHERE run_id=?",
                (revision_id, normalized),
            )
            self._append_event(
                database,
                normalized,
                "revision_created",
                {"revision_id": revision_id, "summary": normalized_summary},
                now,
            )
            result = self._read_revision(database, revision_id)
            database.commit()
            return result, True

    def undo(self, project_id: str, run_id: object) -> AgentRevision:
        normalized = self._validate_run_id(run_id)
        with self.projects.connect(project_id) as database:
            database.execute("BEGIN IMMEDIATE")
            run = self._read_run(database, normalized)
            current = self._read_revision(database, run.current_revision_id)
            if current.parent_revision_id is None:
                database.rollback()
                raise ProjectError("undo_unavailable", "目前已是最初版本。", status_code=409)
            parent = self._read_revision(database, current.parent_revision_id)
            database.execute(
                "UPDATE edit_agent_runs SET current_revision_id=? WHERE run_id=?",
                (parent.revision_id, normalized),
            )
            self._append_event(
                database,
                normalized,
                "revision_selected",
                {"revision_id": parent.revision_id},
                utc_now(),
            )
            database.commit()
            return parent

    def finish(self, project_id: str, run_id: object, summary: object) -> AgentRun:
        normalized = self._validate_run_id(run_id)
        normalized_summary = bounded_text(summary, "完成摘要", 1_000)
        with self.projects.connect(project_id) as database:
            database.execute("BEGIN IMMEDIATE")
            run = self._read_run(database, normalized)
            if run.state != "running":
                database.rollback()
                raise ProjectError("edit_agent_not_running", "AI 剪輯工作並未執行。", status_code=409)
            now = utc_now()
            database.execute(
                "UPDATE edit_agent_runs SET state='succeeded',phase='completed',"
                "final_revision_id=current_revision_id,completed_at=? WHERE run_id=?",
                (now, normalized),
            )
            self._append_event(database, normalized, "run_finished", {"summary": normalized_summary}, now)
            result = self._read_run(database, normalized)
            database.commit()
            return result

    def record_provider_outcome(
        self,
        project_id: str,
        run_id: object,
        attempt_id: str,
        *,
        usage: Mapping[str, int] | None,
        error_code: str | None = None,
    ) -> AgentRun:
        normalized = self._validate_run_id(run_id)
        bounded_text(attempt_id, "Provider attempt", 128)
        if error_code is not None:
            bounded_text(error_code, "Provider 錯誤", 128)
        usage_json = None
        if usage is not None:
            expected = {"input_tokens", "cached_input_tokens", "output_tokens"}
            if set(usage) != expected or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in usage.values()
            ):
                raise ValueError("provider usage is invalid")
            if usage["cached_input_tokens"] > usage["input_tokens"]:
                raise ValueError("provider cached usage exceeds input usage")
            usage_json = encoded(dict(usage))
        with self.projects.connect(project_id) as database:
            database.execute("BEGIN IMMEDIATE")
            run = self._read_run(database, normalized)
            now = utc_now()
            if error_code is not None and run.state in {"queued", "running"}:
                database.execute(
                    "UPDATE edit_agent_runs SET state='failed',phase='failed',error_code=?,"
                    "provider_attempt_id=?,usage_json=?,completed_at=? WHERE run_id=?",
                    (error_code, attempt_id, usage_json, now, normalized),
                )
            else:
                database.execute(
                    "UPDATE edit_agent_runs SET provider_attempt_id=?,usage_json=? WHERE run_id=?",
                    (attempt_id, usage_json, normalized),
                )
            self._append_event(
                database,
                normalized,
                "provider_finished" if error_code is None else "provider_failed",
                {"attempt_id": attempt_id, "usage": usage, "error_code": error_code},
                now,
            )
            result = self._read_run(database, normalized)
            database.commit()
            return result

    def get(self, project_id: str, run_id: object) -> AgentRun:
        normalized = self._validate_run_id(run_id)
        with self.projects.connect(project_id) as database:
            return self._read_run(database, normalized)

    def latest(self, project_id: str) -> AgentRun | None:
        validate_project_id(project_id)
        with self.projects.connect(project_id) as database:
            row = database.execute(
                "SELECT run_id FROM edit_agent_runs ORDER BY created_at DESC,rowid DESC LIMIT 1"
            ).fetchone()
            return None if row is None else self._read_run(database, row["run_id"])

    def cancel(self, project_id: str, run_id: object) -> AgentRun:
        normalized = self._validate_run_id(run_id)
        with self.projects.connect(project_id) as database:
            database.execute("BEGIN IMMEDIATE")
            run = self._read_run(database, normalized)
            if run.state in {"succeeded", "failed", "cancelled", "interrupted"}:
                database.rollback()
                return run
            now = utc_now()
            database.execute(
                "UPDATE edit_agent_runs SET state='cancelled',phase='cancelled',"
                "error_code='user_cancelled',completed_at=? WHERE run_id=?",
                (now, normalized),
            )
            self._append_event(database, normalized, "run_cancelled", {}, now)
            result = self._read_run(database, normalized)
            database.commit()
            return result

    def interrupt_incomplete(self, project_id: str) -> int:
        validate_project_id(project_id)
        with self.projects.connect(project_id) as database:
            database.execute("BEGIN IMMEDIATE")
            rows = database.execute(
                "SELECT run_id FROM edit_agent_runs WHERE state IN ('queued','running')"
            ).fetchall()
            now = utc_now()
            for row in rows:
                database.execute(
                    "UPDATE edit_agent_runs SET state='interrupted',phase='interrupted',"
                    "error_code='controller_stopped',completed_at=? WHERE run_id=?",
                    (now, row["run_id"]),
                )
                self._append_event(
                    database, row["run_id"], "run_interrupted", {}, now
                )
            database.commit()
            return len(rows)

    def interrupt_for_shutdown(self, project_id: str, run_id: object) -> AgentRun:
        """Record Controller shutdown after its provider process has been stopped."""

        normalized = self._validate_run_id(run_id)
        with self.projects.connect(project_id) as database:
            database.execute("BEGIN IMMEDIATE")
            run = self._read_run(database, normalized)
            may_interrupt = run.state in {"queued", "running"} or (
                run.state == "failed" and run.error_code == "provider_cancelled"
            )
            if not may_interrupt:
                database.rollback()
                return run
            now = utc_now()
            database.execute(
                "UPDATE edit_agent_runs SET state='interrupted',phase='interrupted',"
                "error_code='controller_stopped',completed_at=? WHERE run_id=?",
                (now, normalized),
            )
            self._append_event(database, normalized, "run_interrupted", {}, now)
            result = self._read_run(database, normalized)
            database.commit()
            return result

    @staticmethod
    def _validate_run_id(value: object) -> str:
        if not isinstance(value, str) or RUN_ID_PATTERN.fullmatch(value) is None:
            raise ProjectError("edit_agent_not_found", "找不到 AI 剪輯工作。", status_code=404)
        return value

    @staticmethod
    def _append_event(database, run_id: str, event_type: str, payload: object, now: str) -> None:
        sequence = database.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 value FROM edit_agent_events WHERE run_id=?",
            (run_id,),
        ).fetchone()["value"]
        database.execute(
            "INSERT INTO edit_agent_events(run_id,sequence,event_type,payload_json,created_at) "
            "VALUES(?,?,?,?,?)",
            (run_id, sequence, event_type, encoded(payload), now),
        )

    @staticmethod
    def _read_run(database, run_id: str) -> AgentRun:
        row = database.execute("SELECT * FROM edit_agent_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise ProjectError("edit_agent_not_found", "找不到 AI 剪輯工作。", status_code=404)
        try:
            usage = None if row["usage_json"] is None else json.loads(row["usage_json"])
        except (json.JSONDecodeError, TypeError) as error:
            raise ProjectError("storage_corrupt", "Clipen 無法讀取 AI 用量紀錄。", status_code=500) from error
        return AgentRun(
            row["run_id"], row["project_id"], row["base_timeline_id"], row["state"],
            row["phase"], row["max_tool_calls"], row["tool_calls"],
            row["max_preview_renders"], row["preview_renders"], row["current_revision_id"],
            row["final_revision_id"], row["provider"], row["provider_attempt_id"], usage,
            row["error_code"], row["created_at"],
            row["started_at"], row["completed_at"],
        )

    @staticmethod
    def _read_revision(database, revision_id: str) -> AgentRevision:
        if not isinstance(revision_id, str) or REVISION_ID_PATTERN.fullmatch(revision_id) is None:
            raise ProjectError("edit_revision_not_found", "找不到剪輯修改版本。", status_code=404)
        row = database.execute(
            "SELECT * FROM edit_agent_revisions WHERE revision_id=?", (revision_id,)
        ).fetchone()
        if row is None:
            raise ProjectError("edit_revision_not_found", "找不到剪輯修改版本。", status_code=404)
        try:
            timeline = EditTimeline.from_payload(json.loads(row["payload_json"]))
        except (json.JSONDecodeError, TypeError, ValueError) as error:
            raise ProjectError("storage_corrupt", "Clipen 無法讀取剪輯修改版本。", status_code=500) from error
        return AgentRevision(
            row["revision_id"], row["run_id"], row["ordinal"], row["parent_revision_id"],
            timeline, row["summary"], row["created_at"],
        )
