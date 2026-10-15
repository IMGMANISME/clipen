"""Resolve untrusted provider proposals against Controller-owned candidates."""

from __future__ import annotations

from collections.abc import Mapping

from clipen.domain import (
    AudioTreatment,
    BlueprintClip,
    BlueprintEffect,
    BlueprintPacing,
    BlueprintSection,
    BlueprintSoundCue,
    BlueprintTitleCue,
    ClipRole,
    EditBlueprint,
    EffectIntensity,
    EffectKind,
    GraphicsIntensity,
    MusicMode,
    PlanRevision,
    PlanSection,
    PlanSourceRange,
    PlanStatus,
    SoundKind,
    SoundPlacement,
    SubtitleMode,
    TitlePlacement,
    TitleStyle,
    TransitionKind,
)

from .models import PlanningContext


class PlanProposalError(ValueError):
    """A schema-valid proposal still violates local candidate constraints."""


def resolve_plan_proposal(
    payload: Mapping[str, object],
    context: PlanningContext,
    *,
    plan_id: str,
    revision: int,
    system_skill_release: str,
) -> PlanRevision:
    """Bind candidate IDs to exact spans without trusting provider timestamps."""

    try:
        schema_version = payload.get("schema_version")
        expected = {"schema_version", "title", "summary", "sections"}
        if schema_version == 2:
            expected.add("creative_direction")
        if set(payload) != expected or schema_version not in {1, 2}:
            raise TypeError("proposal schema version is invalid")
        title = payload["title"]
        summary = payload["summary"]
        raw_sections = payload["sections"]
        if not isinstance(title, str) or not isinstance(summary, str):
            raise TypeError("proposal text is invalid")
        if not isinstance(raw_sections, list):
            raise TypeError("proposal sections are invalid")
        available = context.candidate_map()
        used: set[str] = set()
        sections: list[PlanSection] = []
        blueprint_sections: list[BlueprintSection] = []
        for raw in raw_sections:
            section_fields = {
                "section_id",
                "title",
                "purpose",
                "edit_notes",
                "unknowns",
            }
            section_fields.add("candidate_ids" if schema_version == 1 else "clips")
            if schema_version == 2:
                section_fields.update({"title_cues", "sound_cues", "transition_out"})
            if not isinstance(raw, dict) or set(raw) != section_fields:
                raise TypeError("proposal section is invalid")
            raw_clips = raw.get("clips")
            candidate_ids = (
                raw["candidate_ids"]
                if schema_version == 1
                else [clip.get("candidate_id") for clip in raw_clips]
                if isinstance(raw_clips, list)
                and all(isinstance(clip, dict) for clip in raw_clips)
                else None
            )
            unknowns = raw["unknowns"]
            if not isinstance(candidate_ids, list) or not isinstance(unknowns, list):
                raise TypeError("proposal section lists are invalid")
            for field in ("section_id", "title", "purpose", "edit_notes"):
                if not isinstance(raw[field], str):
                    raise TypeError("proposal section text is invalid")
            if any(not isinstance(value, str) for value in unknowns):
                raise TypeError("proposal section unknown is invalid")
            ranges = []
            for candidate_id in candidate_ids:
                if not isinstance(candidate_id, str) or candidate_id not in available:
                    raise PlanProposalError("proposal references an unknown candidate")
                if candidate_id in used:
                    raise PlanProposalError("proposal reuses a candidate")
                used.add(candidate_id)
                ranges.append(PlanSourceRange(candidate_id, available[candidate_id].source))
            sections.append(
                PlanSection(
                    raw["section_id"],
                    raw["title"],
                    raw["purpose"],
                    tuple(ranges),
                    raw["edit_notes"],
                    tuple(unknowns),
                )
            )
            if schema_version == 2:
                if (
                    not isinstance(raw_clips, list)
                    or not isinstance(raw["title_cues"], list)
                    or not isinstance(raw["sound_cues"], list)
                ):
                    raise TypeError("proposal blueprint collections are invalid")
                clips = []
                for clip in raw_clips:
                    if set(clip) != {
                        "candidate_id",
                        "role",
                        "audio_treatment",
                        "effects",
                    } or not isinstance(clip["effects"], list):
                        raise TypeError("proposal blueprint clip is invalid")
                    effects = tuple(
                        BlueprintEffect(
                            EffectKind(effect["effect"]),
                            EffectIntensity(effect["intensity"]),
                        )
                        for effect in clip["effects"]
                    )
                    if available[clip["candidate_id"]].media_kind != "video" and effects:
                        raise PlanProposalError(
                            "proposal applies a visual effect to audio-only media"
                        )
                    clips.append(
                        BlueprintClip(
                            clip["candidate_id"],
                            ClipRole(clip["role"]),
                            AudioTreatment(clip["audio_treatment"]),
                            effects,
                        )
                    )
                title_cues = tuple(
                    BlueprintTitleCue(
                        cue["cue_id"],
                        cue["text"],
                        TitlePlacement(cue["placement"]),
                        TitleStyle(cue["style"]),
                    )
                    for cue in raw["title_cues"]
                )
                sound_cues = tuple(
                    BlueprintSoundCue(
                        cue["cue_id"],
                        SoundKind(cue["sound"]),
                        SoundPlacement(cue["placement"]),
                        EffectIntensity(cue["intensity"]),
                    )
                    for cue in raw["sound_cues"]
                )
                blueprint_sections.append(
                    BlueprintSection(
                        raw["section_id"],
                        tuple(clips),
                        title_cues,
                        sound_cues,
                        TransitionKind(raw["transition_out"]),
                    )
                )
        blueprint = None
        if schema_version == 2:
            creative = payload["creative_direction"]
            if not isinstance(creative, dict) or set(creative) != {
                "target_duration_seconds",
                "pacing",
                "subtitle_mode",
                "graphics_intensity",
                "music",
                "sound_design",
            }:
                raise TypeError("proposal creative direction is invalid")
            music = creative["music"]
            if not isinstance(music, dict) or set(music) != {"mode", "mood"}:
                raise TypeError("proposal music direction is invalid")
            blueprint = EditBlueprint(
                creative["target_duration_seconds"],
                BlueprintPacing(creative["pacing"]),
                SubtitleMode(creative["subtitle_mode"]),
                GraphicsIntensity(creative["graphics_intensity"]),
                MusicMode(music["mode"]),
                music["mood"],
                creative["sound_design"],
                tuple(blueprint_sections),
            )
        return PlanRevision.create(
            plan_id=plan_id,
            project_id=context.project_id,
            revision=revision,
            material_snapshot_id=context.material_snapshot_id,
            analysis_snapshot_id=context.analysis_snapshot_id,
            system_skill_release=system_skill_release,
            title=title,
            summary=summary,
            sections=tuple(sections),
            status=PlanStatus.READY_FOR_REVIEW,
            blueprint=blueprint,
        )
    except PlanProposalError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise PlanProposalError("proposal content is invalid") from error
