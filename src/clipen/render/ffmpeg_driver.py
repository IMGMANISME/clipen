"""Turns `timeline.otio` + `captions.srt` into a rendered video, purely
mechanically -- no analysis, no LLM client. This is what makes a hand
edit to either file reproducible with `render` alone: the padding needed
for crossfades is already baked into each clip's `source_range` by
`editing/timeline_builder.py`, so this module only has to read it back
and build the matching ffmpeg filtergraph."""

from __future__ import annotations

import subprocess
from pathlib import Path

import opentimelineio as otio

from clipen.render.probe import RenderResult, render_summary
from clipen.render.subtitles import burn_in_filter

_VIDEO_CODEC = ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
_AUDIO_CODEC = ["-c:a", "aac"]


def _walk_clips_and_boundaries(
    track: otio.schema.Track,
) -> tuple[list[otio.schema.Clip], list[float]]:
    """Returns the clips in order, plus one crossfade duration per
    boundary between consecutive clips (0.0 = hard cut). A Transition
    item only appears in the track between two clips when that boundary
    is a crossfade (see timeline_builder.build_timeline)."""
    clips: list[otio.schema.Clip] = []
    boundaries: list[float] = []
    pending_transition = 0.0
    have_first_clip = False

    for item in track:
        if isinstance(item, otio.schema.Transition):
            pending_transition = item.in_offset.value / item.in_offset.rate
            continue
        if isinstance(item, otio.schema.Clip):
            if have_first_clip:
                boundaries.append(pending_transition)
            pending_transition = 0.0
            have_first_clip = True
            clips.append(item)

    return clips, boundaries


def render(
    timeline_path: Path,
    captions_path: Path,
    output_path: Path,
    *,
    burn_in: bool = False,
) -> RenderResult:
    timeline = otio.adapters.read_from_file(str(timeline_path), adapter_name="otio_json")
    project_root = timeline_path.parent.parent
    track = timeline.tracks[0]

    clips, boundaries = _walk_clips_and_boundaries(track)
    if not clips:
        raise ValueError(f"Timeline at {timeline_path} has no clips to render")

    inputs: list[str] = []
    clip_durations: list[float] = []
    for clip in clips:
        source_path = (project_root / clip.media_reference.target_url).resolve()
        rate = clip.source_range.start_time.rate
        start = clip.source_range.start_time.value / rate
        duration = clip.source_range.duration.value / rate
        inputs += ["-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(source_path)]
        clip_durations.append(duration)

    subtitle_input_index: int | None = None
    if not burn_in:
        inputs += ["-i", str(captions_path)]
        subtitle_input_index = len(clips)

    filter_parts: list[str] = []
    for i in range(len(clips)):
        filter_parts.append(f"[{i}:v]setpts=PTS-STARTPTS[v{i}]")
        filter_parts.append(f"[{i}:a]asetpts=PTS-STARTPTS[a{i}]")

    cur_v, cur_a = "v0", "a0"
    cur_duration = clip_durations[0]
    for i in range(1, len(clips)):
        boundary_duration = boundaries[i - 1]
        nxt_v, nxt_a = f"v{i}", f"a{i}"
        out_v, out_a = f"vchain{i}", f"achain{i}"
        if boundary_duration > 0:
            offset = max(0.0, cur_duration - boundary_duration)
            filter_parts.append(
                f"[{cur_v}][{nxt_v}]xfade=transition=fade:"
                f"duration={boundary_duration:.3f}:offset={offset:.3f}[{out_v}]"
            )
            filter_parts.append(f"[{cur_a}][{nxt_a}]acrossfade=d={boundary_duration:.3f}[{out_a}]")
            cur_duration = cur_duration + clip_durations[i] - boundary_duration
        else:
            filter_parts.append(
                f"[{cur_v}][{cur_a}][{nxt_v}][{nxt_a}]concat=n=2:v=1:a=1[{out_v}][{out_a}]"
            )
            cur_duration = cur_duration + clip_durations[i]
        cur_v, cur_a = out_v, out_a

    video_map = f"[{cur_v}]"
    if burn_in:
        filter_parts.append(f"[{cur_v}]{burn_in_filter(captions_path)}[vburned]")
        video_map = "[vburned]"

    filter_complex = ";".join(filter_parts)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-y", *inputs, "-filter_complex", filter_complex, "-map", video_map, "-map", f"[{cur_a}]"]
    if subtitle_input_index is not None:
        cmd += ["-map", f"{subtitle_input_index}:s", "-c:s", "mov_text"]
    cmd += [*_VIDEO_CODEC, *_AUDIO_CODEC, "-movflags", "+faststart", str(output_path)]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        tail = "\n".join(result.stderr.strip().splitlines()[-20:])
        raise RuntimeError(f"ffmpeg render failed:\n{tail}")

    return render_summary(output_path, clip_count=len(clips))
