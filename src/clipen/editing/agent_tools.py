"""Allowlisted, path-free timeline tools exposed through MCP."""

from __future__ import annotations

from dataclasses import replace
from fractions import Fraction
import json
import math
from pathlib import Path
from typing import Callable, Mapping

from clipen.analysis import ArtifactStore
from clipen.domain import (
    ClipSource,
    EffectRequest,
    MediaClip,
    SourceSpan,
    TextClip,
    Track,
    TrackKind,
)
from clipen.domain import PlanRevision, PlanStatus
from clipen.planning import PlanningRepository
from clipen.planning.models import PlanningCandidate, PlanningContext
from clipen.projects import ProjectError
from clipen.projects.models import validate_project_id
from clipen.projects.storage import utc_now

from .agent_models import (
    ALLOWED_EFFECTS,
    ALLOWED_TITLE_STYLES,
    AgentRun,
    ToolResult,
    bounded_text,
)
from .agent_repository import EditAgentRepository
from .style_proof import is_style_proof
from .production_tools import ProductionTools
from .production_catalog import EXTRA_NAMES
from .skill_tools import SkillTools, SourceContext
from .evidence_jobs import EvidenceTools


class EditAgentTools(SkillTools, EvidenceTools, ProductionTools):
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
        self._inspection_count = 0

    def activate(self) -> AgentRun:
        if self._session_value() is None:
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
        handler = (lambda args: self.production_call(name,args)) if name in EXTRA_NAMES else handlers.get(name)
        if handler is None:
            raise ProjectError("tool_not_allowed", "這個工具未獲 Clipen 授權。", status_code=404)
        if not isinstance(arguments, Mapping):
            raise ProjectError("agent_input_invalid", "工具參數必須是物件。", status_code=422)
        arguments = dict(arguments)
        self._expected_revision_id = arguments.pop('expected_revision_id', None)
        mutation_id = arguments.pop('mutation_id', None) if name!='read_mutation_receipt' else None
        from .tool_receipts import TIMELINE_MUTATIONS, begin_receipt, finish_receipt
        if self._session_value() and name in TIMELINE_MUTATIONS and (self._expected_revision_id is None or mutation_id is None):
            raise ProjectError('mutation_guard_required','修改前請提供目前 expected_revision_id 與本次 mutation_id。',status_code=422)
        if mutation_id is not None:
            if name not in TIMELINE_MUTATIONS:
                raise ProjectError('agent_input_invalid','這個讀取／工作工具不接受 mutation_id。',status_code=422)
            replay=begin_receipt(self,name,arguments,self._expected_revision_id,mutation_id)
            if replay is not None:return replay
        elif self._expected_revision_id is not None and self._expected_revision_id != self.repository.current_revision(self.project_id,self.run_id).revision_id:
            raise ProjectError('revision_conflict','作品版本已更新，請先讀取目前版本。',status_code=409)
        try:
            if not getattr(self, "_user_edit", False):self.repository.consume_tool_call(self.project_id, self.run_id, name)
            result = handler(arguments)
        except Exception as error:
            if mutation_id is not None:finish_receipt(self,mutation_id,error=error)
            with self.repository.projects.connect(self.project_id) as db:
                self.repository._append_event(db,self.run_id,'tool_failed',{'tool':name,'arguments':dict(arguments),'error_code':getattr(error,'code','tool_failed')},utc_now())
            raise
        if mutation_id is not None:finish_receipt(self,mutation_id,result=result)
        with self.repository.projects.connect(self.project_id) as db:
            self.repository._append_event(db,self.run_id,'tool_completed',{'tool':name,'arguments':dict(arguments),'authority':'user' if getattr(self,'_user_edit',False) else 'model'},utc_now())
        return ToolResult(
            {**result.payload, "budget": self.repository.get(self.project_id,self.run_id).to_public()["budget"]},
            result.images,
        )

    def _context_value(self) -> PlanningContext:
        session = self._session_value()
        if session is not None:
            return SourceContext(session["intent"]["brief"])
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
        session = self._session_value()
        if session is not None:
            return ToolResult({**self.repository.get(self.project_id, self.run_id).to_public(),
                "mode": "skill_production", "intent": session["intent"], "skill": self._skill_summary(),
                "creative_direction": self.repository.creative_direction(self.project_id, self.run_id),
                "source_count": len(session["sources"]),
                "evidence_coverage": "Unknown until inspected; no prior analysis or plan required.",
                "next_step": "Read read_production_skill(resource='SKILL.md'), then explore sources and decide your workflow."})
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
                "creative_direction": self.repository.creative_direction(self.project_id,self.run_id),
                "confirmed_direction": {
                    "authority": "user_confirmed",
                    "brief": context.brief,
                    "reference_notes": list(context.reference_notes),
                    "evidence_limitations": list(context.limitations),
                    "plan_id": plan.plan_id,
                    "plan_revision": plan.revision,
                    "title": plan.title,
                    "summary": plan.summary,
                    "sections": sections,
                    "blueprint": (
                        None if plan.blueprint is None else plan.blueprint.to_payload()
                    ),
                    "correction_policy": (
                        "Preserve the user's story intent and factual constraints. You own shot "
                        "selection, order, rhythm, sound and presentation. Explain material "
                        "departures from suggested treatments in revision summaries."
                    ),
                    "proof_constraints": {
                        "purpose": "Create a short style proof, not the full film.",
                        "target_duration_seconds": {"minimum": 45, "maximum": 60},
                        "allowed_duration_seconds": {"minimum": 30, "maximum": 75},
                        "selection_policy": "Choose a coherent scene with action and payoff. Do not use a montage or a clip count as a substitute for the user's story.",
                        "maximum_clip_duration_seconds": 30,
                        "maximum_candidate_inspections": self.repository.get(self.project_id,self.run_id).max_tool_calls,
                    },
                },
            }
        )

    def _search_media(self, arguments: Mapping[str, object]) -> ToolResult:
        self._require_keys(arguments, set(), optional={"query", "limit", "offset"})
        query = bounded_text(arguments.get("query", ""), "搜尋文字", 200, empty=True).casefold()
        limit = arguments.get("limit", 20)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise ProjectError("agent_input_invalid", "搜尋筆數必須介於 1 到 50。", status_code=422)
        candidates = self._context_value().candidates + self._custom_candidates()
        offset = arguments.get('offset',0)
        if isinstance(offset,bool) or not isinstance(offset,int) or not 0 <= offset <= 100000:
            raise ProjectError('agent_input_invalid','分頁位置無效。',status_code=422)
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
                "candidates": [item.to_provider() for item in candidates[offset:offset+limit]],
                "returned": len(candidates[offset:offset+limit]),
                "has_more": len(candidates) > offset+limit,
                "next_offset": offset+limit if len(candidates)>offset+limit else None,
                "limitations": list(self._context_value().limitations),
            }
        )

    def _inspect_clip(self, arguments: Mapping[str, object]) -> ToolResult:
        self._require_keys(arguments, {"candidate_id"})
        if self._inspection_count >= self.repository.get(self.project_id,self.run_id).max_tool_calls:
            raise ProjectError(
                "inspection_budget_exhausted",
                "本次素材檢視已達工具預算上限。",
                status_code=409,
            )
        self._inspection_count += 1
        candidate = self._candidate(arguments["candidate_id"])
        images, samples = self._candidate_images(candidate)
        return ToolResult(
            {
                "candidate": candidate.to_provider(),
                "image_count": len(images),
                "image_samples": samples,
            },
            images,
        )

    # Claude Code saves tool output above about 25k tokens to a file the model
    # cannot open; keep every read well under that and say how to read the rest.
    _SEQUENCE_CHARACTERS = 40_000

    def _inspect_sequence(self, arguments: Mapping[str, object]) -> ToolResult:
        self._require_keys(arguments, set(), optional={"track", "from_seconds", "to_seconds"})
        track_kind = arguments.get("track")
        if track_kind is not None and track_kind not in {"video", "title", "audio"}:
            raise ProjectError("agent_input_invalid", "track 只能是 video、title 或 audio。", status_code=422)
        bounds = [arguments.get(key) for key in ("from_seconds", "to_seconds")]
        if any(v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0) for v in bounds):
            raise ProjectError("agent_input_invalid", "from_seconds／to_seconds 必須是非負數。", status_code=422)
        revision = self.repository.current_revision(self.project_id, self.run_id)
        payload = revision.to_public(include_timeline=True)
        timeline = payload["timeline"]
        fps = revision.timeline.frame_rate.numerator / revision.timeline.frame_rate.denominator
        start = round((bounds[0] or 0) * fps)
        end = round(bounds[1] * fps) if bounds[1] is not None else timeline["duration_frames"]
        filtered = track_kind is not None or any(v is not None for v in bounds)
        if filtered:
            timeline["tracks"] = [
                {**track, "items": [item for item in track["items"]
                                    if item["start_frame"] < end and start < item["start_frame"] + item["duration_frames"]]}
                for track in timeline["tracks"] if track_kind is None or track["kind"] == track_kind]
            payload["window"] = {"track": track_kind, "from_frame": start, "to_frame": end}
        if len(json.dumps(payload, ensure_ascii=False)) <= self._SEQUENCE_CHARACTERS:
            return ToolResult(payload)
        # Too large for one read: return an outline and how to read it in parts.
        seconds = timeline["duration_frames"] / fps
        outline = [{"track_id": track["track_id"], "kind": track["kind"], "item_count": len(track["items"]),
                    "items": [{"item_id": item["item_id"], "start_frame": item["start_frame"],
                               "duration_frames": item["duration_frames"],
                               **({"text": item["text"][:40]} if "text" in item else {})} for item in track["items"]][:200]}
                   for track in timeline["tracks"]]
        span = max(10, round(seconds * self._SEQUENCE_CHARACTERS / max(1, len(json.dumps(payload, ensure_ascii=False))) / 2))
        payload["timeline"] = {key: value for key, value in timeline.items() if key != "tracks"}
        payload["outline"] = outline
        payload["read_in_parts"] = (f"The full timeline is too large for one read. Call inspect_sequence with track "
                                    f"(video, title or audio) and/or from_seconds/to_seconds; windows of about {span} "
                                    f"seconds fit. The outline lists every item with its timing.")
        if len(json.dumps(payload, ensure_ascii=False)) > self._SEQUENCE_CHARACTERS:
            for track in outline:
                track["items"] = [{"item_id": i["item_id"], "start_frame": i["start_frame"], "duration_frames": i["duration_frames"]}
                                  for i in track["items"]]
        return ToolResult(payload)

    def _replace_sequence(self, arguments: Mapping[str, object]) -> ToolResult:
        self._require_keys(arguments, {"clips", "summary"},optional={'reprise_reason'})
        if (self._session_value() or {}).get('intent',{}).get('latest_revision_request') and self._timeline().duration_frames > 0:
            raise ProjectError('incremental_revision_required','這是既有影片的修訂，請使用裁切、替換單一片段、重排或元件工具保留其他內容，不可清空重建整條時間軸。',status_code=409)
        selections = arguments["clips"]
        from .production_limits import limits_for
        limits=limits_for(self)
        if not isinstance(selections, list) or not 1 <= len(selections) <= limits.clips:
            raise ProjectError(
                "agent_input_invalid",
                f"本次製作必須包含 1 到 {limits.clips} 個鏡頭。",
                status_code=422,
            )
        parsed = [self._trimmed_candidate(value) for value in selections]
        identities = [identity for _, _, identity in parsed]
        reprise=arguments.get('reprise_reason')
        if reprise is not None:reprise=bounded_text(reprise,'預告／回扣用途',500)
        if len(identities) != len(set(identities)) and not reprise:
            raise ProjectError(
                "agent_input_invalid", "不能重複使用完全相同的來源範圍。", status_code=422
            )
        candidates = [candidate for candidate, _, _ in parsed]
        if any(item.media_kind != "video" for item in candidates):
            raise ProjectError("agent_input_invalid", "主畫面只能使用影片候選片段。", status_code=422)
        revision = self.repository.current_revision(self.project_id, self.run_id)
        timeline = revision.timeline
        cursor = 0
        clips = []
        for index, (_candidate, span, _identity) in enumerate(parsed, 1):
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
        seconds = Fraction(
            cursor * timeline.frame_rate.denominator,
            timeline.frame_rate.numerator,
        )
        if not (Fraction(3, 4) if self._session_value() else Fraction(30, 1)) <= seconds <= Fraction(limits.duration_seconds, 1):
            raise ProjectError(
                "agent_input_invalid",
                f"本次製作片長超出範圍，上限為 {limits.duration_seconds} 秒。",
                status_code=422,
            )
        updated = replace(
            timeline,
            duration_frames=cursor,
            tracks=(Track("video-main", TrackKind.VIDEO, tuple(clips)),),
            markers=(),
        )
        saved, created = self.repository.save_revision(
            self.project_id, self.run_id, updated, arguments["summary"], expected_revision_id=self._expected_revision_id
        )
        return ToolResult({"revision": saved.to_public(), "created": created})

    def _add_title(self, arguments: Mapping[str, object]) -> ToolResult:
        from .program_direction import require_title_cards
        require_title_cards(self)
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
        title_tracks = [track for track in timeline.tracks if track.track_id == 'titles-main']
        title_items = list(title_tracks[0].items) if title_tracks else []
        title_items.append(TextClip(f"agent-title-{len(title_items)+1:04d}", start, duration, text, style))
        tracks = tuple(track for track in timeline.tracks if track.track_id != 'titles-main') + (
            Track("titles-main", TrackKind.TITLE, tuple(title_items)),
        )
        updated = replace(timeline, tracks=tracks)
        saved, created = self.repository.save_revision(
            self.project_id, self.run_id, updated, arguments["summary"], expected_revision_id=self._expected_revision_id
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
        if not parameters:
            parameters = self._default_effect_parameters(effect_id)
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
            self.project_id, self.run_id, updated, arguments["summary"], expected_revision_id=self._expected_revision_id
        )
        return ToolResult({"revision": saved.to_public(), "created": created})

    def _undo_revision(self, arguments: Mapping[str, object]) -> ToolResult:
        self._require_keys(arguments, set())
        return ToolResult({"revision": self.repository.undo(self.project_id, self.run_id,expected_revision_id=self._expected_revision_id).to_public()})

    def _finish_edit(self, arguments: Mapping[str, object]) -> ToolResult:
        self._require_keys(arguments, {"summary"})
        timeline = self.repository.current_revision(
            self.project_id, self.run_id
        ).timeline
        issues=self._production_validate_sequence({}).payload['issues']
        if issues:
            raise ProjectError('agent_sequence_invalid','時間軸或交付條件尚未通過，請先呼叫 validate_sequence 查看實際範圍與待修項目。',status_code=422)
        # Production bounds, including minimum delivery duration, are reported
        # by validate_sequence. Only legacy style proofs have a separate policy.
        if self._session_value() is None and not is_style_proof(timeline):
            raise ProjectError(
                "style_proof_invalid",
                "剪輯方向樣片尚未符合片長或鏡頭範圍限制。",
                status_code=422,
            )
        from .production_preview import finish_review_status
        review=finish_review_status(self)
        if not review['reviewed_revision']:
            raise ProjectError('agent_preview_review_required','請先逐鏡檢視目前預覽及文字，使用 review_cut 記錄判斷並修正未解決問題；validate_sequence 會列出缺少的取樣。',status_code=409)
        exported=self._production_export_edit({}).payload
        return ToolResult({**self.repository.finish(self.project_id, self.run_id, arguments["summary"],expected_revision_id=self._expected_revision_id).to_public(),'review':review,'export':exported})

    def _candidate(self, value: object) -> PlanningCandidate:
        if not isinstance(value, str):
            raise ProjectError("agent_input_invalid", "候選片段識別資訊無效。", status_code=422)
        candidate = self._context_value().candidate_map().get(value) or next((c for c in self._custom_candidates() if c.candidate_id==value),None)
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

    @staticmethod
    def _default_effect_parameters(effect_id: str) -> dict[str, float]:
        defaults = {
            "video.punch_in": {"scale": 1.08, "center_x": 0.5, "center_y": 0.5},
            "video.slow_push": {"scale_from": 1.0, "scale_to": 1.08},
            "video.reframe": {"x": 0.5, "y": 0.5},
            "video.speed_ramp": {"rate": 1.25},
            "audio.dialogue_focus": {"strength": 0.6},
            "audio.duck_music": {"decibels": -12.0},
        }
        return defaults[effect_id]

    def _trimmed_candidate(
        self, value: object
    ) -> tuple[PlanningCandidate, SourceSpan, tuple[str, int, int]]:
        if not isinstance(value, Mapping) or set(value) != {
            "candidate_id",
            "start_offset_seconds",
            "duration_seconds",
        }:
            raise ProjectError(
                "agent_input_invalid", "精剪鏡頭內容無效。", status_code=422
            )
        candidate = self._candidate(value["candidate_id"])
        start = self._finite_fraction(value["start_offset_seconds"], "鏡頭入點")
        duration = self._finite_fraction(value["duration_seconds"], "鏡頭長度")
        if start < 0 or not Fraction(3, 4) <= duration <= Fraction(30, 1):
            raise ProjectError(
                "agent_input_invalid",
                "單一鏡頭必須介於 0.75 到 30 秒。",
                status_code=422,
            )
        source = candidate.source
        units_per_second = Fraction(source.time_base.denominator, source.time_base.numerator)
        start_offset_pts = round(start * units_per_second)
        duration_pts = max(1, round(duration * units_per_second))
        start_pts = int(source.start_pts) + start_offset_pts
        end_pts = start_pts + duration_pts
        if start_pts < int(source.start_pts) or end_pts > int(source.end_pts):
            raise ProjectError(
                "agent_input_invalid",
                "精剪鏡頭超出候選片段的來源範圍。",
                status_code=422,
            )
        span = SourceSpan(
            source.version_id,
            source.stream_index,
            str(start_pts),
            str(end_pts),
            source.time_base,
        )
        return candidate, span, (candidate.candidate_id, start_pts, end_pts)

    @staticmethod
    def _finite_fraction(value: object, label: str) -> Fraction:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            raise ProjectError(
                "agent_input_invalid", f"{label}必須是有限數字。", status_code=422
            )
        return Fraction(str(value))

    def _candidate_images(
        self, candidate: PlanningCandidate
    ) -> tuple[tuple[Path, ...], list[dict[str, object]]]:
        with self.repository.projects.connect(self.project_id) as database:
            rows = database.execute(
                "SELECT results.result_json FROM evidence_results results "
                "JOIN evidence_runs runs ON runs.run_id=results.run_id "
                "WHERE runs.state='succeeded' ORDER BY runs.created_at DESC, results.ordinal"
            ).fetchall()
        artifacts: list[tuple[int, str, int]] = []
        center = (int(candidate.source.start_pts) + int(candidate.source.end_pts)) // 2
        for row in rows:
            try:
                result = json.loads(row["result_json"])
                source = result.get("source")
                if not isinstance(source, dict) or source.get("version_id") != candidate.source.version_id:
                    continue
                for frame in result.get("frames", []):
                    if isinstance(frame, dict) and isinstance(frame.get("artifact_id"), str):
                        pts = int(frame.get("pts", center))
                        artifacts.append((abs(pts - center), frame["artifact_id"], pts))
                if artifacts:
                    break
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
        inside = [
            item
            for item in artifacts
            if int(candidate.source.start_pts) <= item[2] <= int(candidate.source.end_pts)
        ]
        # A nearby frame from another candidate can look plausible while pointing
        # the model at an invalid offset. Returning no image is safer than mixing
        # evidence from outside the approved source range.
        nearest = sorted(inside)[:4]
        selected = sorted(nearest, key=lambda item: item[2])
        paths = []
        samples = []
        for _, artifact_id, pts in selected:
            try:
                paths.append(self.artifacts.path(self.artifacts.get(self.project_id, artifact_id)))
                offset = Fraction(
                    (pts - int(candidate.source.start_pts))
                    * candidate.source.time_base.numerator,
                    candidate.source.time_base.denominator,
                )
                samples.append(
                    {
                        "image_index": len(paths) - 1,
                        "offset_seconds": round(float(offset), 3),
                    }
                )
            except (OSError, ProjectError):
                continue
        return tuple(paths), samples

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
