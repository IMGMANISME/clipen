"""Pure validation gate between an editable timeline and any compiler."""

from __future__ import annotations

from dataclasses import dataclass

from jsonschema import Draft202012Validator

from .capabilities import (
    CapabilityProfile,
    DeliveryMode,
    EffectCapability,
    ElementCapability,
)
from .plans import PlanRevision, PlanStatus
from .reports import ValidationIssue, ValidationReport, ValidationSeverity
from .timeline import EditTimeline, GraphicClip, MediaClip, TextClip, TrackKind
from .validation import require_identifier


_REQUIRED_DELIVERIES = frozenset(
    (DeliveryMode.MOVIE, DeliveryMode.RESOLVE_PROJECT)
)


def _identifier_set(values: frozenset[str], field: str) -> frozenset[str]:
    normalized = frozenset(values)
    for value in normalized:
        require_identifier(value, field)
    return normalized


@dataclass(frozen=True)
class TimelineReferences:
    """Project-scoped references that the timeline is authorized to use."""

    project_id: str
    plan: PlanRevision
    version_ids: frozenset[str] = frozenset()
    artifact_ids: frozenset[str] = frozenset()
    style_ids: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        require_identifier(self.project_id, "project_id")
        if not isinstance(self.plan, PlanRevision):
            raise ValueError("timeline plan reference is invalid")
        if self.plan.project_id != self.project_id:
            raise ValueError("timeline plan belongs to another project")
        object.__setattr__(
            self, "version_ids", _identifier_set(self.version_ids, "version_id")
        )
        object.__setattr__(
            self, "artifact_ids", _identifier_set(self.artifact_ids, "artifact_id")
        )
        object.__setattr__(
            self, "style_ids", _identifier_set(self.style_ids, "style_id")
        )


class TimelineValidator:
    """Validate references, visible continuity, and both delivery paths."""

    def validate(
        self,
        report_id: str,
        timeline: EditTimeline,
        profile: CapabilityProfile,
        references: TimelineReferences,
    ) -> ValidationReport:
        require_identifier(report_id, "report_id")
        if not isinstance(timeline, EditTimeline):
            raise ValueError("timeline is invalid")
        if not isinstance(profile, CapabilityProfile):
            raise ValueError("capability profile is invalid")
        if not isinstance(references, TimelineReferences):
            raise ValueError("timeline references are invalid")

        issues: list[ValidationIssue] = []
        self._validate_plan(timeline, references, issues)
        self._validate_continuity(timeline, issues)
        self._validate_items(timeline, profile, references, issues)
        return ValidationReport(
            report_id,
            timeline.timeline_id,
            profile.profile_id,
            profile.content_hash,
            tuple(issues),
        )

    @staticmethod
    def _validate_plan(
        timeline: EditTimeline,
        references: TimelineReferences,
        issues: list[ValidationIssue],
    ) -> None:
        plan = references.plan
        if timeline.project_id != references.project_id:
            issues.append(
                _blocking(
                    "timeline.reference.project_mismatch",
                    "/project_id",
                    "時間軸不屬於目前作品。",
                    "請用目前作品的固定輸入重新產生時間軸。",
                )
            )
        if (
            timeline.plan_id != plan.plan_id
            or timeline.plan_revision != plan.revision
            or timeline.plan_content_hash != plan.content_hash
        ):
            issues.append(
                _blocking(
                    "timeline.reference.plan_mismatch",
                    "/plan_id",
                    "時間軸使用的剪輯計畫版本與目前確認版本不一致。",
                    "請從目前已確認的剪輯計畫重新建立時間軸。",
                )
            )
        if plan.status is not PlanStatus.CONFIRMED:
            issues.append(
                _blocking(
                    "timeline.reference.plan_not_confirmed",
                    "/plan_revision",
                    "剪輯計畫尚未處於已確認狀態。",
                    "請先讓使用者確認這一版剪輯計畫。",
                )
            )

    @staticmethod
    def _validate_continuity(
        timeline: EditTimeline, issues: list[ValidationIssue]
    ) -> None:
        primary = next(
            (track for track in timeline.tracks if track.kind is TrackKind.VIDEO),
            None,
        )
        if primary is None or not primary.items:
            issues.append(
                _blocking(
                    "timeline.content.video_missing",
                    "/tracks",
                    "時間軸沒有主要影片畫面。",
                    "請加入覆蓋完整片長的主要影片軌。",
                )
            )
        else:
            cursor = 0
            track_index = timeline.tracks.index(primary)
            for item_index, item in enumerate(primary.items):
                if item.start_frame > cursor:
                    issues.append(
                        _blocking(
                            "timeline.content.video_gap",
                            f"/tracks/{track_index}/items/{item_index}/start_frame",
                            (
                                "主要影片軌包含未說明的畫面空洞，"
                                "可能輸出黑畫面。"
                            ),
                            (
                                "請補上畫面，或在後續契約中明確加入"
                                "允許的背景方案。"
                            ),
                            item.item_id,
                        )
                    )
                cursor = item.start_frame + item.duration_frames
            if cursor < timeline.duration_frames:
                issues.append(
                    _blocking(
                        "timeline.content.video_gap",
                        f"/tracks/{track_index}",
                        "主要影片軌未覆蓋時間軸結尾，可能輸出黑畫面。",
                        "請讓主要影片畫面覆蓋到時間軸結尾。",
                    )
                )
        if not any(
            track.kind is TrackKind.AUDIO and track.items
            for track in timeline.tracks
        ):
            issues.append(
                ValidationIssue(
                    "timeline.content.audio_missing",
                    ValidationSeverity.WARNING,
                    "/tracks",
                    "時間軸沒有明確的音訊元素。",
                    "請確認作品刻意靜音，否則加入獨立音訊軌。",
                )
            )

    @staticmethod
    def _validate_items(
        timeline: EditTimeline,
        profile: CapabilityProfile,
        references: TimelineReferences,
        issues: list[ValidationIssue],
    ) -> None:
        elements = {item.kind: item for item in profile.elements}
        effects = {
            (item.effect_id, item.version): item for item in profile.effects
        }
        effect_validators = {
            key: Draft202012Validator(capability.parameter_schema)
            for key, capability in effects.items()
        }
        for track_index, track in enumerate(timeline.tracks):
            capability = elements.get(track.kind)
            missing_deliveries = _missing_deliveries(capability)
            for item_index, item in enumerate(track.items):
                location = f"/tracks/{track_index}/items/{item_index}"
                if missing_deliveries:
                    issues.append(
                        _blocking(
                            "timeline.capability.element_unverified",
                            location,
                            (
                                "這個元素尚未證實可同時輸出成片與 "
                                "Resolve 可編輯工程。"
                            ),
                            "請使用有兩種交付測試證據的元素實作。",
                            item.item_id,
                        )
                    )
                if isinstance(item, MediaClip):
                    if item.source.span.version_id not in references.version_ids:
                        issues.append(
                            _blocking(
                                "timeline.reference.source_missing",
                                location + "/source/version_id",
                                "來源素材版本不在目前作品的固定素材快照中。",
                                "請重新選取仍存在且已授權的素材版本。",
                                item.item_id,
                            )
                        )
                    TimelineValidator._validate_effects(
                        track.kind,
                        item,
                        location,
                        effects,
                        effect_validators,
                        issues,
                    )
                elif isinstance(item, GraphicClip):
                    if item.artifact_id not in references.artifact_ids:
                        issues.append(
                            _blocking(
                                "timeline.reference.artifact_missing",
                                location + "/artifact_id",
                                "插圖不在目前作品的可用產物快照中。",
                                "請重新產生或選擇仍存在的插圖。",
                                item.item_id,
                            )
                        )
                elif (
                    isinstance(item, TextClip)
                    and item.style_id not in references.style_ids
                ):
                    issues.append(
                        _blocking(
                            "timeline.reference.style_missing",
                            location + "/style_id",
                            "文字樣式不在目前作品的可用樣式快照中。",
                            "請選擇已驗證字型與版面的文字樣式。",
                            item.item_id,
                        )
                    )

    @staticmethod
    def _validate_effects(
        kind: TrackKind,
        item: MediaClip,
        location: str,
        capabilities: dict[tuple[str, int], EffectCapability],
        validators: dict[tuple[str, int], Draft202012Validator],
        issues: list[ValidationIssue],
    ) -> None:
        for effect_index, request in enumerate(item.effects):
            effect_location = f"{location}/effects/{effect_index}"
            capability = capabilities.get((request.effect_id, request.version))
            if capability is None:
                issues.append(
                    _blocking(
                        "timeline.capability.effect_unknown",
                        effect_location,
                        "效果版本不在目前能力快照中。",
                        "請移除效果，或改用已有測試證據的效果版本。",
                        item.item_id,
                    )
                )
                continue
            if kind not in capability.track_kinds:
                issues.append(
                    _blocking(
                        "timeline.capability.effect_track_unsupported",
                        effect_location,
                        "效果不能套用在這種軌道元素上。",
                        "請移除效果或移到能力快照允許的元素種類。",
                        item.item_id,
                    )
                )
            if _missing_deliveries(capability):
                issues.append(
                    _blocking(
                        "timeline.capability.effect_delivery_unverified",
                        effect_location,
                        "效果尚未證實可同時輸出成片與 Resolve 可編輯工程。",
                        "請使用通過兩種交付測試的效果。",
                        item.item_id,
                    )
                )
            if not validators[(request.effect_id, request.version)].is_valid(
                request.parameters
            ):
                issues.append(
                    _blocking(
                        "timeline.capability.effect_parameters_invalid",
                        effect_location + "/parameters",
                        "效果參數不符合這個效果版本的安全範圍。",
                        "請修正參數，或重新套用此版本的預設值。",
                        item.item_id,
                    )
                )


def _missing_deliveries(
    capability: ElementCapability | EffectCapability | None,
) -> frozenset[DeliveryMode]:
    if capability is None:
        return _REQUIRED_DELIVERIES
    supported = frozenset(delivery.mode for delivery in capability.deliveries)
    return _REQUIRED_DELIVERIES - supported


def _blocking(
    rule_id: str,
    location: str,
    message: str,
    suggestion: str,
    item_id: str | None = None,
) -> ValidationIssue:
    return ValidationIssue(
        rule_id,
        ValidationSeverity.BLOCKING,
        location,
        message,
        suggestion,
        item_id=item_id,
    )
