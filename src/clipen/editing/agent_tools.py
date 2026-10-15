"""Allowlisted, path-free timeline tools exposed through MCP."""

from __future__ import annotations

from dataclasses import replace
from fractions import Fraction
import json
from pathlib import Path
from typing import Callable, Mapping

from clipen.analysis import ArtifactStore
from clipen.domain import (
    ClipSource,
    EffectRequest,
    MediaClip,
    TextClip,
    Track,
    TrackKind,
)
from clipen.domain import PlanRevision, PlanStatus
from clipen.planning import PlanningRepository
from clipen.planning.models import PlanningCandidate, PlanningContext
from clipen.projects import ProjectError
from clipen.projects.models import validate_project_id

from .agent_models import (
    ALLOWED_EFFECTS,
    ALLOWED_TITLE_STYLES,
    AgentRun,
    ToolResult,
    bounded_text,
)
from .agent_repository import EditAgentRepository


class EditAgentTools:
    """Small allowlisted tool surface; it never returns source filesystem paths."""

    def __init__(
        self,
        repository: EditAgentRepository,
        project_id: str,
        run_id: str,
        *,
        planning: PlanningRepository | None = None,
        artifacts: ArtifactStore | None = None,
    ) -> None:
        validate_project_id(project_id)
        self.repository = repository
        self.project_id = project_id
        self.run_id = run_id
        self.planning = planning or PlanningRepository(repository.projects)
        self.artifacts = artifacts or ArtifactStore(repository.projects)
        self._plan: PlanRevision | None = None
        self._context: PlanningContext | None = None

    def activate(self) -> AgentRun:
        self._confirmed_input()
        return self.repository.activate(self.project_id, self.run_id)

    def call(self, name: str, arguments: object) -> ToolResult:
        handlers: dict[str, Callable[[Mapping[str, object]], ToolResult]] = {
            "get_edit_status": self._get_edit_status,
            "search_media": self._search_media,
            "inspect_clip": self._inspect_clip,
            "inspect_sequence": self._inspect_sequence,
            "replace_sequence": self._replace_sequence,
            "add_title": self._add_title,
            "set_clip_effect": self._set_clip_effect,
            "undo_revision": self._undo_revision,
            "finish_edit": self._finish_edit,
        }
        handler = handlers.get(name)
        if handler is None:
            raise ProjectError("tool_not_allowed", "這個工具未獲 Clipen 授權。", status_code=404)
        if not isinstance(arguments, Mapping):
            raise ProjectError("agent_input_invalid", "工具參數必須是物件。", status_code=422)
        budget = self.repository.consume_tool_call(self.project_id, self.run_id, name)
        result = handler(arguments)
        return ToolResult(
            {**result.payload, "budget": budget.to_public()["budget"]},
            result.images,
        )

    def _context_value(self) -> PlanningContext:
        self._confirmed_input()
        assert self._context is not None
        return self._context

    def _confirmed_input(self) -> tuple[PlanRevision, PlanningContext]:
        if self._plan is None or self._context is None:
            plan, context = self.planning.execution_input(self.project_id)
            revision = self.repository.current_revision(self.project_id, self.run_id)
            timeline = revision.timeline
            if (
                plan.status is not PlanStatus.CONFIRMED
                or plan.plan_id != timeline.plan_id
                or plan.revision != timeline.plan_revision
                or plan.content_hash != timeline.plan_content_hash
            ):
                raise ProjectError(
                    "confirmed_plan_mismatch",
                    "已確認的剪輯計畫與目前時間軸不一致，請重新建立剪輯版本。",
                    status_code=409,
                )
            self._plan = plan
            self._context = context
        return self._plan, self._context

    def _get_edit_status(self, arguments: Mapping[str, object]) -> ToolResult:
        self._require_keys(arguments, set())
        plan, context = self._confirmed_input()
        sections = [
            {
                "section_id": section.section_id,
                "title": section.title,
                "purpose": section.purpose,
                "edit_notes": section.edit_notes,
                "candidate_ids": [item.candidate_id for item in section.source_ranges],
                "unknowns": list(section.unknowns),
            }
            for section in plan.sections
        ]
        return ToolResult(
            {
                **self.repository.get(self.project_id, self.run_id).to_public(),
                "confirmed_direction": {
                    "authority": "user_confirmed",
                    "brief": context.brief,
                    "plan_id": plan.plan_id,
                    "plan_revision": plan.revision,
                    "title": plan.title,
                    "summary": plan.summary,
                    "sections": sections,
                    "blueprint": (
                        None if plan.blueprint is None else plan.blueprint.to_payload()
                    ),
                    "correction_policy": (
                        "Execute this direction as authoritative. Make only the smallest "
                        "necessary correction for an obvious contradiction or invalid timing, "
                        "and record that correction in the revision summary."
                    ),
                },
            }
        )

    def _search_media(self, arguments: Mapping[str, object]) -> ToolResult:
        self._require_keys(arguments, {"query", "limit"}, optional={"query", "limit"})
        query = bounded_text(arguments.get("query", ""), "搜尋文字", 200, empty=True).casefold()
        limit = arguments.get("limit", 20)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise ProjectError("agent_input_invalid", "搜尋筆數必須介於 1 到 50。", status_code=422)
        candidates = self._context_value().candidates
        if query:
            candidates = tuple(
                item
                for item in candidates
                if query in " ".join(
                    filter(None, (item.display_name, item.evidence_summary, item.transcript_excerpt))
                ).casefold()
            )
        return ToolResult(
            {
                "candidates": [item.to_provider() for item in candidates[:limit]],
                "returned": min(len(candidates), limit),
                "has_more": len(candidates) > limit,
                "limitations": list(self._context_value().limitations),
            }
        )

    def _inspect_clip(self, arguments: Mapping[str, object]) -> ToolResult:
        self._require_keys(arguments, {"candidate_id"})
        candidate = self._candidate(arguments["candidate_id"])
        images = self._candidate_images(candidate)
        return ToolResult(
            {"candidate": candidate.to_provider(), "image_count": len(images)}, images
        )

    def _inspect_sequence(self, arguments: Mapping[str, object]) -> ToolResult:
        self._require_keys(arguments, set())
        revision = self.repository.current_revision(self.project_id, self.run_id)
        payload = revision.to_public(include_timeline=True)
        return ToolResult(payload)

    def _replace_sequence(self, arguments: Mapping[str, object]) -> ToolResult:
        self._require_keys(arguments, {"candidate_ids", "summary"})
        identifiers = arguments["candidate_ids"]
        if not isinstance(identifiers, list) or not identifiers or len(identifiers) > 500:
            raise ProjectError("agent_input_invalid", "候選片段清單內容無效。", status_code=422)
        if len(identifiers) != len(set(identifiers)):
            raise ProjectError("agent_input_invalid", "候選片段不能重複。", status_code=422)
        candidates = [self._candidate(value) for value in identifiers]
        if any(item.media_kind != "video" for item in candidates):
            raise ProjectError("agent_input_invalid", "主畫面只能使用影片候選片段。", status_code=422)
        revision = self.repository.current_revision(self.project_id, self.run_id)
        timeline = revision.timeline
        cursor = 0
        clips = []
        for index, candidate in enumerate(candidates, 1):
            span = candidate.source
            frames = max(
                1,
                round(
                    Fraction(span.duration_pts)
                    * span.time_base.numerator
                    * timeline.frame_rate.numerator
                    / (span.time_base.denominator * timeline.frame_rate.denominator)
                ),
            )
            clips.append(
                MediaClip(
                    f"agent-video-{index:04d}",
                    cursor,
                    frames,
                    source=ClipSource(span),
                )
            )
            cursor += frames
        updated = replace(
            timeline,
            duration_frames=cursor,
            tracks=(Track("video-main", TrackKind.VIDEO, tuple(clips)),),
            markers=(),
        )
        saved, created = self.repository.save_revision(
            self.project_id, self.run_id, updated, arguments["summary"]
        )
        return ToolResult({"revision": saved.to_public(), "created": created})

    def _add_title(self, arguments: Mapping[str, object]) -> ToolResult:
        self._require_keys(arguments, {"start_frame", "duration_frames", "text", "style_id", "summary"})
        start = arguments["start_frame"]
        duration = arguments["duration_frames"]
        style = arguments["style_id"]
        if any(isinstance(value, bool) or not isinstance(value, int) for value in (start, duration)):
            raise ProjectError("agent_input_invalid", "字卡時間必須使用整數影格。", status_code=422)
        if not isinstance(style, str) or style not in ALLOWED_TITLE_STYLES:
            raise ProjectError("agent_input_invalid", "字卡樣式不在允許清單中。", status_code=422)
        text = bounded_text(arguments["text"], "字卡", 500)
        revision = self.repository.current_revision(self.project_id, self.run_id)
        timeline = revision.timeline
        title_tracks = [track for track in timeline.tracks if track.kind is TrackKind.TITLE]
        title_items = list(title_tracks[0].items) if title_tracks else []
        title_items.append(TextClip(f"agent-title-{len(title_items)+1:04d}", start, duration, text, style))
        tracks = tuple(track for track in timeline.tracks if track.kind is not TrackKind.TITLE) + (
            Track("titles-main", TrackKind.TITLE, tuple(title_items)),
        )
        updated = replace(timeline, tracks=tracks)
        saved, created = self.repository.save_revision(
            self.project_id, self.run_id, updated, arguments["summary"]
        )
        return ToolResult({"revision": saved.to_public(), "created": created})

    def _set_clip_effect(self, arguments: Mapping[str, object]) -> ToolResult:
        self._require_keys(arguments, {"item_id", "effect_id", "parameters", "summary"})
        item_id = bounded_text(arguments["item_id"], "片段識別", 128)
        effect_id = arguments["effect_id"]
        if not isinstance(effect_id, str) or effect_id not in ALLOWED_EFFECTS:
            raise ProjectError("agent_input_invalid", "效果不在允許清單中。", status_code=422)
        parameters = arguments["parameters"]
        if not isinstance(parameters, Mapping):
            raise ProjectError("agent_input_invalid", "效果參數必須是物件。", status_code=422)
        self._validate_effect_parameters(effect_id, parameters)
        effect = EffectRequest(effect_id, parameters)
        revision = self.repository.current_revision(self.project_id, self.run_id)
        found = False
        tracks = []
        for track in revision.timeline.tracks:
            items = []
            for item in track.items:
                if isinstance(item, MediaClip) and item.item_id == item_id:
                    expected_prefix = "video." if track.kind is TrackKind.VIDEO else "audio."
                    if not effect_id.startswith(expected_prefix):
                        raise ProjectError(
                            "agent_input_invalid",
                            "這個效果不能套用在指定軌道。",
                            status_code=422,
                        )
                    found = True
                    retained = tuple(value for value in item.effects if value.effect_id != effect_id)
                    item = replace(item, effects=retained + (effect,))
                items.append(item)
            tracks.append(replace(track, items=tuple(items)))
        if not found:
            raise ProjectError("timeline_item_not_found", "找不到指定的剪輯片段。", status_code=404)
        updated = replace(revision.timeline, tracks=tuple(tracks))
        saved, created = self.repository.save_revision(
            self.project_id, self.run_id, updated, arguments["summary"]
        )
        return ToolResult({"revision": saved.to_public(), "created": created})

    def _undo_revision(self, arguments: Mapping[str, object]) -> ToolResult:
        self._require_keys(arguments, set())
        return ToolResult({"revision": self.repository.undo(self.project_id, self.run_id).to_public()})

    def _finish_edit(self, arguments: Mapping[str, object]) -> ToolResult:
        self._require_keys(arguments, {"summary"})
        return ToolResult(self.repository.finish(self.project_id, self.run_id, arguments["summary"]).to_public())

    def _candidate(self, value: object) -> PlanningCandidate:
        if not isinstance(value, str):
            raise ProjectError("agent_input_invalid", "候選片段識別資訊無效。", status_code=422)
        candidate = self._context_value().candidate_map().get(value)
        if candidate is None:
            raise ProjectError("candidate_not_found", "找不到指定的候選片段。", status_code=404)
        return candidate

    @staticmethod
    def _validate_effect_parameters(
        effect_id: str,
        parameters: Mapping[str, object],
    ) -> None:
        rules: dict[str, dict[str, tuple[float, float]]] = {
            "video.punch_in": {
                "scale": (1.01, 2.0),
                "center_x": (0.0, 1.0),
                "center_y": (0.0, 1.0),
            },
            "video.slow_push": {
                "scale_from": (1.0, 2.0),
                "scale_to": (1.0, 2.0),
            },
            "video.reframe": {"x": (0.0, 1.0), "y": (0.0, 1.0)},
            "video.speed_ramp": {"rate": (0.25, 4.0)},
            "audio.dialogue_focus": {"strength": (0.0, 1.0)},
            "audio.duck_music": {"decibels": (-30.0, 0.0)},
        }
        allowed = rules[effect_id]
        if not parameters or set(parameters) - set(allowed):
            raise ProjectError(
                "agent_input_invalid",
                "效果參數不符合這個效果的定義。",
                status_code=422,
            )
        for key, value in parameters.items():
            minimum, maximum = allowed[key]
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not minimum <= float(value) <= maximum
            ):
                raise ProjectError(
                    "agent_input_invalid",
                    "效果參數超出安全範圍。",
                    status_code=422,
                )

    def _candidate_images(self, candidate: PlanningCandidate) -> tuple[Path, ...]:
        with self.repository.projects.connect(self.project_id) as database:
            rows = database.execute(
                "SELECT results.result_json FROM evidence_results results "
                "JOIN evidence_runs runs ON runs.run_id=results.run_id "
                "WHERE runs.state='succeeded' ORDER BY runs.created_at DESC, results.ordinal"
            ).fetchall()
        artifacts: list[tuple[int, str]] = []
        center = (int(candidate.source.start_pts) + int(candidate.source.end_pts)) // 2
        for row in rows:
            try:
                result = json.loads(row["result_json"])
                source = result.get("source")
                if not isinstance(source, dict) or source.get("version_id") != candidate.source.version_id:
                    continue
                for frame in result.get("frames", []):
                    if isinstance(frame, dict) and isinstance(frame.get("artifact_id"), str):
                        artifacts.append((abs(int(frame.get("pts", center)) - center), frame["artifact_id"]))
                if artifacts:
                    break
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
        paths = []
        for _, artifact_id in sorted(artifacts)[:8]:
            try:
                paths.append(self.artifacts.path(self.artifacts.get(self.project_id, artifact_id)))
            except (OSError, ProjectError):
                continue
        return tuple(paths)

    @staticmethod
    def _require_keys(
        arguments: Mapping[str, object],
        required: set[str],
        *,
        optional: set[str] | None = None,
    ) -> None:
        allowed = required | (optional or set())
        if not required.issubset(arguments) or set(arguments) - allowed:
            raise ProjectError("agent_input_invalid", "工具參數欄位不完整或包含未知欄位。", status_code=422)
