"""Deterministically compile a confirmed plan into an exact edit timeline."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import hashlib
import json

from clipen.domain import (
    ClipSource,
    EditTimeline,
    Marker,
    MediaClip,
    PlanRevision,
    PlanStatus,
    Rational,
    SourceSpan,
    Track,
    TrackKind,
)
from clipen.planning import PlanningContext


_PRESETS = {
    "landscape_1080p": (1920, 1080),
    "portrait_1080p": (1080, 1920),
}
_FRAME_RATES = frozenset((24, 25, 30, 60))


@dataclass(frozen=True, slots=True)
class OutputSettings:
    preset: str
    frame_rate: int

    def __post_init__(self) -> None:
        if not isinstance(self.preset, str) or self.preset not in _PRESETS:
            raise ValueError("output preset is invalid")
        if (
            isinstance(self.frame_rate, bool)
            or not isinstance(self.frame_rate, int)
            or self.frame_rate not in _FRAME_RATES
        ):
            raise ValueError("output frame rate is invalid")

    @property
    def canvas(self) -> tuple[int, int]:
        return _PRESETS[self.preset]

    def to_payload(self) -> dict[str, object]:
        width, height = self.canvas
        return {
            "preset": self.preset,
            "frame_rate": self.frame_rate,
            "width": width,
            "height": height,
        }


class TimelineCompiler:
    """Compile only Controller-resolved candidates; never consume file paths."""

    def compile(
        self,
        plan: PlanRevision,
        context: PlanningContext,
        settings: OutputSettings,
    ) -> EditTimeline:
        if plan.status is not PlanStatus.CONFIRMED:
            raise ValueError("timeline requires a confirmed plan")
        if plan.project_id != context.project_id:
            raise ValueError("timeline context belongs to another project")
        candidates = {
            candidate.candidate_id: candidate for candidate in context.candidates
        }
        video_items: list[MediaClip] = []
        audio_sources: list[SourceSpan] = []
        markers: list[Marker] = []
        video_cursor = 0
        video_ordinal = 0
        audio_ordinal = 0
        rate = Rational(settings.frame_rate, 1)

        for section_index, section in enumerate(plan.sections):
            section_video_start = video_cursor
            section_has_media = False
            for selected in section.source_ranges:
                candidate = candidates.get(selected.candidate_id)
                if candidate is None or candidate.source != selected.source:
                    raise ValueError("plan source is not present in its immutable context")
                frames = _source_frames(selected.source, settings.frame_rate)
                section_has_media = True
                if candidate.media_kind == "video":
                    video_ordinal += 1
                    video_items.append(
                        MediaClip(
                            f"video-{video_ordinal:06d}",
                            video_cursor,
                            frames,
                            ClipSource(selected.source),
                        )
                    )
                    video_cursor += frames
                elif candidate.media_kind == "audio":
                    audio_sources.append(selected.source)
                else:
                    raise ValueError("plan source media kind is invalid")
            if section_has_media:
                markers.append(
                    Marker(
                        f"section-{section_index + 1:04d}",
                        section_video_start,
                        section.title,
                        section.purpose,
                    )
                )

        audio_limit = video_cursor if video_items else None
        audio_cursor = 0
        audio_items: list[MediaClip] = []
        for source in audio_sources:
            if audio_limit is not None and audio_cursor >= audio_limit:
                break
            frames = _source_frames(source, settings.frame_rate)
            effective_source = source
            if audio_limit is not None and audio_cursor + frames > audio_limit:
                effective_source = _trim_source_to_frames(
                    source, audio_limit - audio_cursor, settings.frame_rate
                )
                frames = _source_frames(effective_source, settings.frame_rate)
            audio_ordinal += 1
            audio_items.append(
                MediaClip(
                    f"audio-{audio_ordinal:06d}",
                    audio_cursor,
                    frames,
                    ClipSource(effective_source),
                )
            )
            audio_cursor += frames

        duration = video_cursor if video_items else audio_cursor
        if duration < 1:
            raise ValueError("confirmed plan does not contain executable media")
        tracks = []
        if video_items:
            tracks.append(Track("video-primary", TrackKind.VIDEO, tuple(video_items)))
        if audio_items:
            tracks.append(Track("audio-primary", TrackKind.AUDIO, tuple(audio_items)))
        marker_limit = duration
        normalized_markers = tuple(
            marker for marker in markers if marker.frame <= marker_limit
        )
        identity = {
            "project_id": plan.project_id,
            "plan_id": plan.plan_id,
            "plan_revision": plan.revision,
            "plan_content_hash": plan.content_hash,
            "output": settings.to_payload(),
        }
        digest = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        width, height = settings.canvas
        return EditTimeline(
            "tl_" + digest[:32],
            plan.project_id,
            plan.plan_id,
            plan.revision,
            plan.content_hash,
            rate,
            width,
            height,
            duration,
            tuple(tracks),
            normalized_markers,
        )


def _source_frames(source: SourceSpan, frame_rate: int) -> int:
    exact = Fraction(
        source.duration_pts * source.time_base.numerator * frame_rate,
        source.time_base.denominator,
    )
    return max(1, (exact.numerator * 2 + exact.denominator) // (2 * exact.denominator))


def _trim_source_to_frames(
    source: SourceSpan, maximum_frames: int, frame_rate: int
) -> SourceSpan:
    if maximum_frames < 1:
        raise ValueError("trimmed source duration is empty")
    pts = Fraction(
        maximum_frames * source.time_base.denominator,
        source.time_base.numerator * frame_rate,
    )
    duration_pts = min(source.duration_pts, max(1, pts.numerator // pts.denominator))
    return SourceSpan(
        source.version_id,
        source.stream_index,
        str(int(source.start_pts)),
        str(int(source.start_pts) + duration_pts),
        source.time_base,
    )
