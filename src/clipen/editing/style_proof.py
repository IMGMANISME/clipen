"""One source of truth for the bounded first-pass style proof."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from clipen.domain import EditTimeline, MediaClip, TrackKind


MIN_CLIPS = 8
MAX_CLIPS = 30
MIN_DURATION_SECONDS = 30
MAX_DURATION_SECONDS = 75
MAX_CLIP_SECONDS = 10
MIN_TITLES = 1
MAX_TITLES = 3
MIN_EFFECTS = 1
MAX_EFFECTS = 6


@dataclass(frozen=True, slots=True)
class StyleProofStats:
    duration_seconds: float
    clip_count: int
    longest_clip_seconds: float
    title_count: int
    effect_count: int


def style_proof_stats(timeline: EditTimeline) -> StyleProofStats:
    frame_rate = Fraction(
        timeline.frame_rate.numerator, timeline.frame_rate.denominator
    )
    video = next(
        (track for track in timeline.tracks if track.kind is TrackKind.VIDEO), None
    )
    clips = () if video is None else video.items
    clip_seconds = [Fraction(item.duration_frames, 1) / frame_rate for item in clips]
    return StyleProofStats(
        float(Fraction(timeline.duration_frames, 1) / frame_rate),
        len(clips),
        float(max(clip_seconds, default=Fraction(0, 1))),
        sum(
            len(track.items)
            for track in timeline.tracks
            if track.kind is TrackKind.TITLE
        ),
        sum(
            len(item.effects)
            for track in timeline.tracks
            for item in track.items
            if isinstance(item, MediaClip)
        ),
    )


def is_style_proof(timeline: EditTimeline) -> bool:
    stats = style_proof_stats(timeline)
    return (
        _is_bounded_draft(stats)
        and MIN_TITLES <= stats.title_count
        and MIN_EFFECTS <= stats.effect_count
    )


def is_style_proof_draft(timeline: EditTimeline) -> bool:
    """Accept a bounded diagnostic draft even when style elements are incomplete."""

    return _is_bounded_draft(style_proof_stats(timeline))


def _is_bounded_draft(stats: StyleProofStats) -> bool:
    return (
        MIN_CLIPS <= stats.clip_count <= MAX_CLIPS
        and MIN_DURATION_SECONDS <= stats.duration_seconds <= MAX_DURATION_SECONDS
        and stats.longest_clip_seconds <= MAX_CLIP_SECONDS
        and stats.title_count <= MAX_TITLES
        and stats.effect_count <= MAX_EFFECTS
    )
