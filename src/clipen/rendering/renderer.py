"""FFmpeg renderer for sequential rough-cut preview timelines."""

from __future__ import annotations

from decimal import Decimal
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from typing import Callable

from clipen.domain import EditTimeline, MediaClip, Track, TrackKind
from clipen.media import MediaPreviewRepository
from clipen.projects import ProjectError


class PreviewRenderError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class FfmpegPreviewRenderer:
    """Render only the immutable media tracks present in a compiled timeline."""

    _BATCH_SIZE = 8

    def __init__(
        self,
        media: MediaPreviewRepository,
        executable: str = "ffmpeg",
        *,
        popen: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
    ) -> None:
        self.media = media
        self.executable = executable
        self._popen = popen

    def render(
        self,
        project_id: str,
        timeline: EditTimeline,
        output: Path,
        cancel: threading.Event,
        progress: Callable[[float], None],
    ) -> None:
        if os.name == "nt":
            raise PreviewRenderError("preview_platform_unsupported")
        video = next(
            (track for track in timeline.tracks if track.kind is TrackKind.VIDEO), None
        )
        audio = next(
            (track for track in timeline.tracks if track.kind is TrackKind.AUDIO), None
        )
        if video is None or not video.items:
            raise PreviewRenderError("preview_video_required")
        if cancel.is_set():
            raise PreviewRenderError("cancelled")
        output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if audio is None and len(video.items) > self._BATCH_SIZE:
            self._render_batched(project_id, timeline, video, output, cancel, progress)
            return

        opened = []
        try:
            command = [
                self.executable,
                "-hide_banner",
                "-loglevel",
                "error",
                "-filter_complex_threads",
                "2",
                "-y",
            ]
            filters: list[str] = []
            video_labels: list[str] = []
            audio_labels: list[str] = []
            video_inputs = []

            for item in video.items:
                assert isinstance(item, MediaClip)
                input_index, duration, source = self._add_input(
                    command, opened, project_id, item
                )
                video_inputs.append((input_index, duration, source))
                stream = self._stream_index(
                    item.source.span.stream_index,
                    source.video_stream_indices,
                    "preview_video_stream_missing",
                )
                label = f"v{len(video_labels)}"
                filters.append(
                    f"[{input_index}:{stream}]trim=duration={duration},setpts=PTS-STARTPTS,"
                    f"scale={timeline.width}:{timeline.height}:"
                    "force_original_aspect_ratio=decrease,"
                    f"pad={timeline.width}:{timeline.height}:(ow-iw)/2:(oh-ih)/2,"
                    f"fps={timeline.frame_rate.numerator}/"
                    f"{timeline.frame_rate.denominator},format=yuv420p[{label}]"
                )
                video_labels.append(f"[{label}]")

            if audio is not None:
                for item in audio.items:
                    assert isinstance(item, MediaClip)
                    input_index, duration, source = self._add_input(
                        command, opened, project_id, item
                    )
                    stream = self._stream_index(
                        item.source.span.stream_index,
                        source.audio_stream_indices,
                        "preview_audio_stream_missing",
                    )
                    label = f"a{len(audio_labels)}"
                    filters.append(
                        f"[{input_index}:{stream}]atrim=duration={duration},"
                        "asetpts=PTS-STARTPTS,aresample=48000,"
                        f"aformat=sample_rates=48000:channel_layouts=stereo[{label}]"
                    )
                    audio_labels.append(f"[{label}]")
            else:
                # A compiled rough cut may not yet have an editable audio track.
                # Keep the camera audio aligned with every video clip so the user
                # can still judge the cut. Silent source clips get matching silence.
                for input_index, duration, source in video_inputs:
                    label = f"a{len(audio_labels)}"
                    if source.audio_stream_indices:
                        stream = source.audio_stream_indices[0]
                        filters.append(
                            f"[{input_index}:{stream}]atrim=duration={duration},"
                            "asetpts=PTS-STARTPTS,aresample=48000,"
                            f"aformat=sample_rates=48000:channel_layouts=stereo[{label}]"
                        )
                    else:
                        filters.append(
                            "anullsrc=r=48000:cl=stereo,"
                            f"atrim=duration={duration}[{label}]"
                        )
                    audio_labels.append(f"[{label}]")

            filters.append(
                f"{''.join(video_labels)}concat=n={len(video_labels)}:v=1:a=0[vout]"
            )
            timeline_duration = self._timeline_seconds(timeline)
            if audio_labels:
                filters.append(
                    f"{''.join(audio_labels)}concat=n={len(audio_labels)}:v=0:a=1[asequence]"
                )
                filters.append(
                    f"[asequence]apad,atrim=duration={timeline_duration}[aout]"
                )
            else:
                filters.append(
                    f"anullsrc=r=48000:cl=stereo,atrim=duration={timeline_duration}[aout]"
                )

            command.extend(
                [
                    "-filter_complex",
                    ";".join(filters),
                    "-map",
                    "[vout]",
                    "-map",
                    "[aout]",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-threads",
                    "4",
                    "-crf",
                    "24",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "160k",
                    "-movflags",
                    "+faststart",
                    "-progress",
                    "pipe:1",
                    "-nostats",
                    str(output),
                ]
            )
            with tempfile.TemporaryFile() as errors:
                try:
                    process = self._popen(
                        command,
                        stdout=subprocess.PIPE,
                        stderr=errors,
                        text=True,
                        pass_fds=tuple(item.descriptor for item in opened),
                    )
                except FileNotFoundError as error:
                    raise PreviewRenderError("ffmpeg_unavailable") from error
                except OSError as error:
                    raise PreviewRenderError("ffmpeg_start_failed") from error
                assert process.stdout is not None
                terminated = False
                try:
                    for line in process.stdout:
                        if cancel.is_set() and not terminated:
                            process.terminate()
                            terminated = True
                        if line.startswith("out_time_ms="):
                            raw_micros = line.split("=", 1)[1].strip()
                            if not raw_micros.isdigit():
                                continue
                            micros = int(raw_micros)
                            progress(
                                min(
                                    0.99,
                                    micros / 1_000_000 / float(timeline_duration),
                                )
                            )
                finally:
                    close_stdout = getattr(process.stdout, "close", None)
                    if close_stdout is not None:
                        close_stdout()
                code = process.wait()
                if cancel.is_set():
                    raise PreviewRenderError("cancelled")
                if code != 0 or not output.is_file() or output.stat().st_size < 1:
                    raise PreviewRenderError("ffmpeg_render_failed")
                progress(1.0)
        finally:
            for item in opened:
                item.close()

    def _add_input(self, command, opened, project_id: str, item: MediaClip):
        try:
            source = self.media.open(project_id, item.source.span.version_id)
        except ProjectError as error:
            raise PreviewRenderError(error.code) from error
        opened.append(source)
        start = self._seconds(item.source.span.start_pts, item.source.span.time_base)
        duration = self._seconds(
            str(item.source.span.duration_pts), item.source.span.time_base
        )
        command.extend(
            [
                "-ss",
                start,
                "-t",
                duration,
                "-threads",
                "1",
                "-i",
                f"/dev/fd/{source.descriptor}",
            ]
        )
        return len(opened) - 1, duration, source

    def _render_batched(
        self,
        project_id: str,
        timeline: EditTimeline,
        video: Track,
        output: Path,
        cancel: threading.Event,
        progress: Callable[[float], None],
    ) -> None:
        total_frames = sum(item.duration_frames for item in video.items)
        completed_frames = 0
        with tempfile.TemporaryDirectory(
            prefix=".clipen-preview-", dir=output.parent
        ) as temporary:
            directory = Path(temporary)
            segments = []
            for batch_index, offset in enumerate(
                range(0, len(video.items), self._BATCH_SIZE)
            ):
                if cancel.is_set():
                    raise PreviewRenderError("cancelled")
                source_items = video.items[offset : offset + self._BATCH_SIZE]
                cursor = 0
                normalized_items = []
                for item in source_items:
                    assert isinstance(item, MediaClip)
                    normalized_items.append(
                        MediaClip(
                            item.item_id,
                            cursor,
                            item.duration_frames,
                            item.source,
                            item.effects,
                        )
                    )
                    cursor += item.duration_frames
                batch = EditTimeline(
                    timeline.timeline_id,
                    timeline.project_id,
                    timeline.plan_id,
                    timeline.plan_revision,
                    timeline.plan_content_hash,
                    timeline.frame_rate,
                    timeline.width,
                    timeline.height,
                    cursor,
                    (Track("video-primary", TrackKind.VIDEO, tuple(normalized_items)),),
                )
                segment = directory / f"segment-{batch_index:04d}.mp4"
                base = completed_frames

                def batch_progress(value: float, *, base=base, size=cursor) -> None:
                    progress(0.98 * (base + value * size) / total_frames)

                self.render(
                    project_id,
                    batch,
                    segment,
                    cancel,
                    batch_progress,
                )
                segments.append(segment)
                completed_frames += cursor
            self._concat_segments(segments, output, cancel)
        progress(1.0)

    def _concat_segments(
        self, segments: list[Path], output: Path, cancel: threading.Event
    ) -> None:
        manifest = segments[0].parent / "segments.txt"
        manifest.write_text(
            "".join(f"file '{segment.name}'\n" for segment in segments),
            encoding="utf-8",
        )
        command = [
            self.executable,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(manifest),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            "-progress",
            "pipe:1",
            "-nostats",
            str(output),
        ]
        with tempfile.TemporaryFile() as errors:
            try:
                process = self._popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=errors,
                    text=True,
                )
            except FileNotFoundError as error:
                raise PreviewRenderError("ffmpeg_unavailable") from error
            except OSError as error:
                raise PreviewRenderError("ffmpeg_start_failed") from error
            assert process.stdout is not None
            terminated = False
            try:
                for _line in process.stdout:
                    if cancel.is_set() and not terminated:
                        process.terminate()
                        terminated = True
            finally:
                close_stdout = getattr(process.stdout, "close", None)
                if close_stdout is not None:
                    close_stdout()
            code = process.wait()
            if cancel.is_set():
                raise PreviewRenderError("cancelled")
            if code != 0 or not output.is_file() or output.stat().st_size < 1:
                raise PreviewRenderError("ffmpeg_render_failed")

    @staticmethod
    def _stream_index(requested: int, available: tuple[int, ...], error: str) -> int:
        if requested in available:
            return requested
        if available:
            return available[0]
        raise PreviewRenderError(error)

    @staticmethod
    def _timeline_seconds(timeline: EditTimeline) -> Decimal:
        return (
            Decimal(timeline.duration_frames)
            * Decimal(timeline.frame_rate.denominator)
            / Decimal(timeline.frame_rate.numerator)
        )

    @staticmethod
    def _seconds(pts: str, time_base) -> str:
        value = (
            Decimal(pts)
            * Decimal(time_base.numerator)
            / Decimal(time_base.denominator)
        )
        return format(value.quantize(Decimal("0.000001")), "f")
