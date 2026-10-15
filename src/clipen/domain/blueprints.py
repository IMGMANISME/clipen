"""Structured, provider-independent production directions for an editable cut."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .validation import require_identifier


def _bounded_text(
    value: object, field: str, maximum: int, *, allow_empty: bool = False
) -> None:
    if (
        not isinstance(value, str)
        or "\x00" in value
        or len(value) > maximum
        or (not allow_empty and not value.strip())
    ):
        raise ValueError(f"{field} is invalid")


class BlueprintPacing(str, Enum):
    RELAXED = "relaxed"
    BALANCED = "balanced"
    ENERGETIC = "energetic"


class SubtitleMode(str, Enum):
    NONE = "none"
    DIALOGUE = "dialogue"
    FULL = "full"
    DYNAMIC = "dynamic"


class GraphicsIntensity(str, Enum):
    MINIMAL = "minimal"
    BALANCED = "balanced"
    EXPRESSIVE = "expressive"


class MusicMode(str, Enum):
    NONE = "none"
    AMBIENT_ONLY = "ambient_only"
    APPROVED_ASSET_REQUIRED = "approved_asset_required"


class ClipRole(str, Enum):
    HOOK = "hook"
    STORY = "story"
    B_ROLL = "b_roll"
    REACTION = "reaction"
    TRANSITION = "transition"
    ENDING = "ending"


class AudioTreatment(str, Enum):
    DIALOGUE = "dialogue"
    AMBIENT = "ambient"
    MIXED = "mixed"
    MUTE = "mute"


class EffectKind(str, Enum):
    PUNCH_IN = "punch_in"
    SLOW_PUSH = "slow_push"
    REFRAME = "reframe"
    SPEED_RAMP = "speed_ramp"


class EffectIntensity(str, Enum):
    SUBTLE = "subtle"
    MEDIUM = "medium"
    STRONG = "strong"


class TransitionKind(str, Enum):
    CUT = "cut"
    DISSOLVE = "dissolve"
    FADE = "fade"
    WHIP = "whip"


class TitlePlacement(str, Enum):
    SECTION_START = "section_start"
    OVER_CLIP = "over_clip"
    SECTION_END = "section_end"


class TitleStyle(str, Enum):
    MINIMAL = "minimal"
    CHAPTER = "chapter"
    LOCATION = "location"
    ENDING = "ending"


class SoundKind(str, Enum):
    WHOOSH = "whoosh"
    IMPACT = "impact"
    CLICK = "click"
    RISER = "riser"
    AMBIENT_ACCENT = "ambient_accent"


class SoundPlacement(str, Enum):
    SECTION_START = "section_start"
    SECTION_MIDDLE = "section_middle"
    SECTION_END = "section_end"


@dataclass(frozen=True, slots=True)
class BlueprintEffect:
    effect: EffectKind
    intensity: EffectIntensity

    def __post_init__(self) -> None:
        if not isinstance(self.effect, EffectKind) or not isinstance(
            self.intensity, EffectIntensity
        ):
            raise ValueError("blueprint effect is invalid")

    def to_payload(self) -> dict[str, str]:
        return {"effect": self.effect.value, "intensity": self.intensity.value}

    @classmethod
    def from_payload(cls, value: object) -> "BlueprintEffect":
        if not isinstance(value, dict) or set(value) != {"effect", "intensity"}:
            raise ValueError("stored blueprint effect is invalid")
        return cls(EffectKind(value["effect"]), EffectIntensity(value["intensity"]))


@dataclass(frozen=True, slots=True)
class BlueprintClip:
    candidate_id: str
    role: ClipRole
    audio_treatment: AudioTreatment
    effects: tuple[BlueprintEffect, ...] = ()

    def __post_init__(self) -> None:
        require_identifier(self.candidate_id, "candidate_id")
        if not isinstance(self.role, ClipRole) or not isinstance(
            self.audio_treatment, AudioTreatment
        ):
            raise ValueError("blueprint clip direction is invalid")
        effects = tuple(self.effects)
        if len(effects) > 8 or any(
            not isinstance(effect, BlueprintEffect) for effect in effects
        ):
            raise ValueError("blueprint clip effects are invalid")
        kinds = [effect.effect for effect in effects]
        if len(kinds) != len(set(kinds)):
            raise ValueError("blueprint clip effects must be unique")
        object.__setattr__(self, "effects", effects)

    def to_payload(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "role": self.role.value,
            "audio_treatment": self.audio_treatment.value,
            "effects": [effect.to_payload() for effect in self.effects],
        }

    @classmethod
    def from_payload(cls, value: object) -> "BlueprintClip":
        if not isinstance(value, dict) or set(value) != {
            "candidate_id",
            "role",
            "audio_treatment",
            "effects",
        }:
            raise ValueError("stored blueprint clip is invalid")
        if not isinstance(value["candidate_id"], str) or not isinstance(
            value["effects"], list
        ):
            raise ValueError("stored blueprint clip fields are invalid")
        return cls(
            value["candidate_id"],
            ClipRole(value["role"]),
            AudioTreatment(value["audio_treatment"]),
            tuple(BlueprintEffect.from_payload(item) for item in value["effects"]),
        )


@dataclass(frozen=True, slots=True)
class BlueprintTitleCue:
    cue_id: str
    text: str
    placement: TitlePlacement
    style: TitleStyle

    def __post_init__(self) -> None:
        require_identifier(self.cue_id, "cue_id")
        _bounded_text(self.text, "blueprint title text", 240)
        if not isinstance(self.placement, TitlePlacement) or not isinstance(
            self.style, TitleStyle
        ):
            raise ValueError("blueprint title direction is invalid")

    def to_payload(self) -> dict[str, str]:
        return {
            "cue_id": self.cue_id,
            "text": self.text,
            "placement": self.placement.value,
            "style": self.style.value,
        }

    @classmethod
    def from_payload(cls, value: object) -> "BlueprintTitleCue":
        if not isinstance(value, dict) or set(value) != {
            "cue_id",
            "text",
            "placement",
            "style",
        }:
            raise ValueError("stored blueprint title cue is invalid")
        if not isinstance(value["cue_id"], str) or not isinstance(value["text"], str):
            raise ValueError("stored blueprint title cue fields are invalid")
        return cls(
            value["cue_id"],
            value["text"],
            TitlePlacement(value["placement"]),
            TitleStyle(value["style"]),
        )


@dataclass(frozen=True, slots=True)
class BlueprintSoundCue:
    cue_id: str
    sound: SoundKind
    placement: SoundPlacement
    intensity: EffectIntensity

    def __post_init__(self) -> None:
        require_identifier(self.cue_id, "cue_id")
        if (
            not isinstance(self.sound, SoundKind)
            or not isinstance(self.placement, SoundPlacement)
            or not isinstance(self.intensity, EffectIntensity)
        ):
            raise ValueError("blueprint sound cue is invalid")

    def to_payload(self) -> dict[str, str]:
        return {
            "cue_id": self.cue_id,
            "sound": self.sound.value,
            "placement": self.placement.value,
            "intensity": self.intensity.value,
        }

    @classmethod
    def from_payload(cls, value: object) -> "BlueprintSoundCue":
        if not isinstance(value, dict) or set(value) != {
            "cue_id",
            "sound",
            "placement",
            "intensity",
        }:
            raise ValueError("stored blueprint sound cue is invalid")
        if not isinstance(value["cue_id"], str):
            raise ValueError("stored blueprint sound cue fields are invalid")
        return cls(
            value["cue_id"],
            SoundKind(value["sound"]),
            SoundPlacement(value["placement"]),
            EffectIntensity(value["intensity"]),
        )


@dataclass(frozen=True, slots=True)
class BlueprintSection:
    section_id: str
    clips: tuple[BlueprintClip, ...]
    title_cues: tuple[BlueprintTitleCue, ...]
    sound_cues: tuple[BlueprintSoundCue, ...]
    transition_out: TransitionKind

    def __post_init__(self) -> None:
        require_identifier(self.section_id, "section_id")
        clips = tuple(self.clips)
        titles = tuple(self.title_cues)
        sounds = tuple(self.sound_cues)
        if not clips or len(clips) > 512 or any(
            not isinstance(clip, BlueprintClip) for clip in clips
        ):
            raise ValueError("blueprint section clips are invalid")
        if len(titles) > 8 or any(
            not isinstance(cue, BlueprintTitleCue) for cue in titles
        ):
            raise ValueError("blueprint section title cues are invalid")
        if len(sounds) > 16 or any(
            not isinstance(cue, BlueprintSoundCue) for cue in sounds
        ):
            raise ValueError("blueprint section sound cues are invalid")
        clip_ids = [clip.candidate_id for clip in clips]
        cue_ids = [cue.cue_id for cue in titles]
        sound_ids = [cue.cue_id for cue in sounds]
        all_cue_ids = cue_ids + sound_ids
        if (
            len(clip_ids) != len(set(clip_ids))
            or len(all_cue_ids) != len(set(all_cue_ids))
        ):
            raise ValueError("blueprint section identifiers must be unique")
        if not isinstance(self.transition_out, TransitionKind):
            raise ValueError("blueprint section transition is invalid")
        object.__setattr__(self, "clips", clips)
        object.__setattr__(self, "title_cues", titles)
        object.__setattr__(self, "sound_cues", sounds)

    def to_payload(self) -> dict[str, object]:
        return {
            "section_id": self.section_id,
            "clips": [clip.to_payload() for clip in self.clips],
            "title_cues": [cue.to_payload() for cue in self.title_cues],
            "sound_cues": [cue.to_payload() for cue in self.sound_cues],
            "transition_out": self.transition_out.value,
        }

    @classmethod
    def from_payload(cls, value: object) -> "BlueprintSection":
        if not isinstance(value, dict) or set(value) != {
            "section_id",
            "clips",
            "title_cues",
            "sound_cues",
            "transition_out",
        }:
            raise ValueError("stored blueprint section is invalid")
        if (
            not isinstance(value["section_id"], str)
            or not isinstance(value["clips"], list)
            or not isinstance(value["title_cues"], list)
            or not isinstance(value["sound_cues"], list)
        ):
            raise ValueError("stored blueprint section fields are invalid")
        return cls(
            value["section_id"],
            tuple(BlueprintClip.from_payload(item) for item in value["clips"]),
            tuple(BlueprintTitleCue.from_payload(item) for item in value["title_cues"]),
            tuple(BlueprintSoundCue.from_payload(item) for item in value["sound_cues"]),
            TransitionKind(value["transition_out"]),
        )


@dataclass(frozen=True, slots=True)
class EditBlueprint:
    target_duration_seconds: int | None
    pacing: BlueprintPacing
    subtitle_mode: SubtitleMode
    graphics_intensity: GraphicsIntensity
    music_mode: MusicMode
    music_mood: str
    sound_design: str
    sections: tuple[BlueprintSection, ...]

    def __post_init__(self) -> None:
        if self.target_duration_seconds is not None and (
            isinstance(self.target_duration_seconds, bool)
            or not isinstance(self.target_duration_seconds, int)
            or not 1 <= self.target_duration_seconds <= 21_600
        ):
            raise ValueError("blueprint target duration is invalid")
        for value, expected, field in (
            (self.pacing, BlueprintPacing, "pacing"),
            (self.subtitle_mode, SubtitleMode, "subtitle mode"),
            (self.graphics_intensity, GraphicsIntensity, "graphics intensity"),
            (self.music_mode, MusicMode, "music mode"),
        ):
            if not isinstance(value, expected):
                raise ValueError(f"blueprint {field} is invalid")
        _bounded_text(self.music_mood, "blueprint music mood", 160, allow_empty=True)
        _bounded_text(self.sound_design, "blueprint sound design", 1_000, allow_empty=True)
        if self.music_mode is MusicMode.APPROVED_ASSET_REQUIRED and not self.music_mood.strip():
            raise ValueError("blueprint music mood is required for an approved asset")
        if self.music_mode is not MusicMode.APPROVED_ASSET_REQUIRED and self.music_mood:
            raise ValueError("blueprint music mood requires an approved asset")
        sections = tuple(self.sections)
        if not sections or len(sections) > 256 or any(
            not isinstance(section, BlueprintSection) for section in sections
        ):
            raise ValueError("blueprint sections are invalid")
        identifiers = [section.section_id for section in sections]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("blueprint section IDs must be unique")
        object.__setattr__(self, "sections", sections)

    def to_payload(self) -> dict[str, object]:
        return {
            "target_duration_seconds": self.target_duration_seconds,
            "pacing": self.pacing.value,
            "subtitle_mode": self.subtitle_mode.value,
            "graphics_intensity": self.graphics_intensity.value,
            "music": {"mode": self.music_mode.value, "mood": self.music_mood},
            "sound_design": self.sound_design,
            "sections": [section.to_payload() for section in self.sections],
        }

    @classmethod
    def from_payload(cls, value: object) -> "EditBlueprint":
        if not isinstance(value, dict) or set(value) != {
            "target_duration_seconds",
            "pacing",
            "subtitle_mode",
            "graphics_intensity",
            "music",
            "sound_design",
            "sections",
        }:
            raise ValueError("stored edit blueprint is invalid")
        music = value["music"]
        sections = value["sections"]
        if (
            not isinstance(music, dict)
            or set(music) != {"mode", "mood"}
            or not isinstance(music["mood"], str)
            or not isinstance(value["sound_design"], str)
            or not isinstance(sections, list)
        ):
            raise ValueError("stored edit blueprint fields are invalid")
        return cls(
            value["target_duration_seconds"],
            BlueprintPacing(value["pacing"]),
            SubtitleMode(value["subtitle_mode"]),
            GraphicsIntensity(value["graphics_intensity"]),
            MusicMode(music["mode"]),
            music["mood"],
            value["sound_design"],
            tuple(BlueprintSection.from_payload(section) for section in sections),
        )
