"""Builds the editable OTIO timeline that `edit` writes and `render` later
reads back -- this schema is the contract between the AI-decision stage
and the mechanical render stage, and the shape a future GUI would also
edit. Derives everything purely from each EditDecision's own fields
(order/start/end/transition_in) -- no dependency on filenames or
grouping, so any decision-producing function that emits a valid
`list[EditDecision]` works here unmodified."""

from __future__ import annotations

from pathlib import Path

import opentimelineio as otio

from clipen.editing.models import EditDecision
from clipen.utils.media import probe_duration

NOMINAL_FPS = 30.0


def build_timeline(
    edit_decisions: list[EditDecision],
    *,
    project_root: Path,
    name: str = "clipen edit",
    transition_duration: float = 0.0,
    min_transition_duration: float = 0.05,
) -> otio.schema.Timeline:
    """transition_duration > 0 requests a crossfade at every boundary whose
    decision has transition_in == "crossfade". Each boundary's actual
    duration is independently clamped to the real footage available past
    the outgoing clip's end and before the incoming clip's start; a
    boundary that can't support at least min_transition_duration falls
    back to a hard cut (no otio.schema.Transition emitted) rather than a
    near-zero crossfade."""
    timeline = otio.schema.Timeline(name=name)
    track = otio.schema.Track(name="video", kind=otio.schema.TrackKind.Video)
    timeline.tracks.append(track)

    decisions = sorted(edit_decisions, key=lambda d: d.order)
    boundary_durations = resolve_boundary_durations(
        decisions,
        transition_duration=transition_duration,
        min_transition_duration=min_transition_duration,
    )

    for i, decision in enumerate(decisions):
        left_pad = boundary_durations[i - 1] if i > 0 else 0.0
        right_pad = boundary_durations[i] if i < len(decisions) - 1 else 0.0

        target_url = _project_relative(decision.source_path, project_root)
        media_ref = otio.schema.ExternalReference(target_url=str(target_url))
        padded_start = decision.start - left_pad
        padded_duration = decision.duration + left_pad + right_pad
        source_range = otio.opentime.TimeRange(
            start_time=otio.opentime.RationalTime(padded_start * NOMINAL_FPS, NOMINAL_FPS),
            duration=otio.opentime.RationalTime(padded_duration * NOMINAL_FPS, NOMINAL_FPS),
        )
        clip = otio.schema.Clip(
            name=decision.source_path.stem,
            media_reference=media_ref,
            source_range=source_range,
        )

        if i > 0 and boundary_durations[i - 1] > 0:
            d = boundary_durations[i - 1]
            track.append(
                otio.schema.Transition(
                    name="crossfade",
                    transition_type=otio.schema.TransitionTypes.SMPTE_Dissolve,
                    in_offset=otio.opentime.RationalTime(d * NOMINAL_FPS, NOMINAL_FPS),
                    out_offset=otio.opentime.RationalTime(d * NOMINAL_FPS, NOMINAL_FPS),
                )
            )
        track.append(clip)

    return timeline


def resolve_boundary_durations(
    decisions: list[EditDecision],
    *,
    transition_duration: float,
    min_transition_duration: float,
) -> list[float]:
    """One entry per boundary between consecutive decisions (len - 1
    entries total). Exposed so editing/captions.py can mirror the exact
    same per-boundary durations when computing caption timestamps --
    crossfades shift where each decision's content lands in output time,
    so caption offsets must use identical boundary math or they drift out
    of sync with the actual rendered video."""
    boundaries: list[float] = []
    for i in range(len(decisions) - 1):
        outgoing, incoming = decisions[i], decisions[i + 1]
        wants_crossfade = transition_duration > 0 and incoming.transition_in == "crossfade"
        if not wants_crossfade:
            boundaries.append(0.0)
            continue

        tail_room = max(0.0, probe_duration(outgoing.source_path) - outgoing.end)
        head_room = max(0.0, incoming.start)
        actual = min(transition_duration, tail_room, head_room)
        boundaries.append(actual if actual >= min_transition_duration else 0.0)
    return boundaries


def _project_relative(path: Path, project_root: Path) -> Path:
    """Media references are stored project-relative so the .clipen/
    project folder stays portable. Falls back to an absolute path if the
    source genuinely lives outside project_root."""
    try:
        return path.resolve().relative_to(project_root.resolve())
    except ValueError:
        return path.resolve()


def write_timeline(timeline: otio.schema.Timeline, path: Path) -> None:
    otio.adapters.write_to_file(timeline, str(path), adapter_name="otio_json")


def read_timeline(path: Path) -> otio.schema.Timeline:
    return otio.adapters.read_from_file(str(path), adapter_name="otio_json")
