"""Builds the SRT caption file alongside the OTIO timeline. A caption's
source-clip timestamp is not its position in the final video once cuts
and crossfades are involved, so this module re-derives output-timeline
time from each EditDecision's own start/order/duration -- using the exact
same boundary-duration math as `editing/timeline_builder.py` -- rather
than assuming source and output time line up."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pysrt

from clipen.analysis.asr import transcribe
from clipen.editing.models import EditDecision
from clipen.editing.timeline_builder import resolve_boundary_durations


def _seconds_to_srt_time(seconds: float) -> pysrt.SubRipTime:
    seconds = max(0.0, seconds)
    ms = round(seconds * 1000)
    return pysrt.SubRipTime(milliseconds=ms)


def build_captions(
    edit_decisions: list[EditDecision],
    *,
    transition_duration: float = 0.0,
    min_transition_duration: float = 0.05,
    max_chars_per_line: int | None = None,
) -> pysrt.SubRipFile:
    decisions = sorted(edit_decisions, key=lambda d: d.order)
    boundary_durations = resolve_boundary_durations(
        decisions,
        transition_duration=transition_duration,
        min_transition_duration=min_transition_duration,
    )

    subs = pysrt.SubRipFile()
    index = 1
    cumulative_offset = 0.0

    for i, decision in enumerate(decisions):
        transcript = transcribe(decision.source_path)
        for seg in transcript:
            seg_start = max(seg["start"], decision.start)
            seg_end = min(seg["end"], decision.end)
            if seg_end <= seg_start:
                continue
            text = seg["text"]
            if max_chars_per_line:
                text = "\n".join(textwrap.wrap(text, max_chars_per_line))

            out_start = cumulative_offset + (seg_start - decision.start)
            out_end = cumulative_offset + (seg_end - decision.start)
            subs.append(
                pysrt.SubRipItem(
                    index=index,
                    start=_seconds_to_srt_time(out_start),
                    end=_seconds_to_srt_time(out_end),
                    text=text,
                )
            )
            index += 1

        cumulative_offset += decision.duration
        if i < len(decisions) - 1:
            cumulative_offset += boundary_durations[i]

    return subs


def write_captions(subs: pysrt.SubRipFile, path: Path) -> None:
    subs.save(str(path), encoding="utf-8")


def read_captions(path: Path) -> pysrt.SubRipFile:
    return pysrt.open(str(path), encoding="utf-8")
