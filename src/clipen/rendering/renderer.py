"""FFmpeg renderer for sequential rough-cut preview timelines."""

from __future__ import annotations

from decimal import Decimal
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from typing import Callable

from clipen.domain import EditTimeline, EffectRequest, MediaClip, Track, TrackKind
from clipen.media import MediaPreviewRepository
from clipen.projects import ProjectError

from .titles import FfmpegTitleCompositor, TitleCardRenderer, TitleRenderError


class PreviewRenderError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class FfmpegPreviewRenderer:
    """Render immutable media plus trusted, bounded presentation effects."""

    _BATCH_SIZE = 8

    def __init__(
        self,
        media: MediaPreviewRepository,
        executable: str = "ffmpeg",
        *,
        popen: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
        hardware_acceleration: bool = False,
    ) -> None:
        self.media = media
        self.executable = executable
        self._popen = popen
        self.hardware_acceleration = hardware_acceleration

    def render(self,project_id,timeline,output,cancel,progress):
        layers=any(e.effect_id=='audio.layers' for t in timeline.tracks for i in t.items if isinstance(i,MediaClip) for e in i.effects)
        if not layers:
            return self._render_base(project_id,timeline,output,cancel,progress)
        from clipen.editing.production_media import mix_audio_layers
        output.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.agent-mix-',dir=output.parent) as temp:
            base=Path(temp)/'camera.mp4'
            self._render_base(project_id,timeline,base,cancel,lambda p:progress(p*.85))
            mix_audio_layers(self.media,project_id,timeline,base,output,cancel)
            progress(1.)

    def _render_base(
        self,
        project_id: str,
        timeline: EditTimeline,
        output: Path,
        cancel: threading.Event,
        progress: Callable[[float], None],
    ) -> None:
        title_track = next(
            (track for track in timeline.tracks if track.kind is TrackKind.TITLE), None
        )
        if not any(t.items for t in timeline.tracks if t.kind is TrackKind.TITLE):
            self._render_media(project_id, timeline, output, cancel, progress)
            return
        output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            with tempfile.TemporaryDirectory(
                prefix=".clipen-title-", dir=output.parent
            ) as temporary:
                directory = Path(temporary)
                media_output = directory / "media.mp4"
                self._render_media(
                    project_id,
                    timeline,
                    media_output,
                    cancel,
                    lambda value: progress(value * 0.82),
                )
                from clipen.editing.production_media import load_text_styles
                styles=load_text_styles(getattr(self.media,'projects',None),project_id)
                if sum(len(t.items) for t in timeline.tracks if t.kind is TrackKind.TITLE)>32 or any(styles.get(i.style_id,{}).get('kind') in {'program_subtitle','program_component'} for t in timeline.tracks if t.kind is TrackKind.TITLE for i in t.items):
                    from .titles import compose_dense_titles
                    compose_dense_titles(self.executable,media_output,timeline,directory,output,cancel,styles)
                    progress(1.)
                    return
                titles = TitleCardRenderer(styles).render(timeline, directory)
                FfmpegTitleCompositor(self.executable, popen=self._popen).compose(
                    media_output,
                    titles,
                    output,
                    cancel,
                    float(self._timeline_seconds(timeline)),
                    lambda value: progress(0.82 + value * 0.18),
                )
        except TitleRenderError as error:
            raise PreviewRenderError(error.code) from error

    def _render_media(self, project_id, timeline, output, cancel, progress, *, preserve_audio_gain=False):
        try:
            return self._render_media_once(project_id, timeline, output, cancel, progress, preserve_audio_gain=preserve_audio_gain)
        except PreviewRenderError as error:
            if not self.hardware_acceleration or error.code != 'ffmpeg_render_failed' or cancel.is_set():
                raise
            # Unsupported input/encoder on this machine: retry with the original
            # software path, retaining all filters, source ranges and timing.
            self.hardware_acceleration = False
            return self._render_media_once(project_id, timeline, output, cancel, progress, preserve_audio_gain=preserve_audio_gain)

    def _render_media_once(
        self,
        project_id: str,
        timeline: EditTimeline,
        output: Path,
        cancel: threading.Event,
        progress: Callable[[float], None],
        *, preserve_audio_gain: bool = False,
    ) -> None:
        if os.name == "nt":
            raise PreviewRenderError("preview_platform_unsupported")
        video = next(
            (track for track in timeline.tracks if track.kind is TrackKind.VIDEO), None
        )
        audio = next(
            (track for track in timeline.tracks if track.kind is TrackKind.AUDIO and track.items), None
        )
        if video is None or not video.items:
            raise PreviewRenderError("preview_video_required")
        if cancel.is_set():
            raise PreviewRenderError("cancelled")
        output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        controlled_audio = preserve_audio_gain or timeline.production_session_id is not None or any(
            effect.effect_id in {'audio.source', 'audio.layers'}
            for track in timeline.tracks for item in track.items
            if isinstance(item, MediaClip) for effect in item.effects
        )
        if audio is None and len(video.items) > self._BATCH_SIZE:
            self._render_batched(project_id, timeline, video, output, cancel, progress, preserve_audio_gain=controlled_audio)
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

            for clip_index,item in enumerate(video.items):
                assert isinstance(item, MediaClip)
                input_index, duration, source = self._add_input(
                    command, opened, project_id, item
                )
                video_inputs.append((input_index, duration, source, item))
                stream = self._stream_index(
                    item.source.span.stream_index,
                    source.video_stream_indices,
                    "preview_video_stream_missing",
                )
                label = f"v{len(video_labels)}"
                base_label = f"vbase{len(video_labels)}"
                filters.append(
                    f"[{input_index}:{stream}]trim=duration={duration},setpts=PTS-STARTPTS,"
                    f"scale={timeline.width}:{timeline.height}:"
                    "force_original_aspect_ratio=decrease,"
                    f"pad={timeline.width}:{timeline.height}:(ow-iw)/2:(oh-ih)/2,"
                    f"fps={timeline.frame_rate.numerator}/"
                    f"{timeline.frame_rate.denominator}[{base_label}]"
                )
                current_label = self._retime_video(
                    filters, base_label, item, float(duration), len(video_labels)
                )
                effect_filters = self._video_effect_filters(item, timeline)
                chain = f"[{current_label}]"
                if effect_filters:
                    chain += effect_filters.removeprefix(",") + ","
                from .production_effects import grade_filters, mosaic_filters, transition_filters
                chain += 'null'+grade_filters(item)+",format=yuv420p"
                styled_label=f'vstyled{clip_index}'
                filters.append(f"{chain}[{styled_label}]")
                masked=mosaic_filters(filters,styled_label,item,timeline,clip_index)
                following=video.items[clip_index+1] if clip_index+1<len(video.items) else None
                # Source PTS and fps rounding can otherwise add a frame at a
                # cut and show the previous shot behind the next caption.
                frame_pad=Decimal(2)*timeline.frame_rate.denominator/timeline.frame_rate.numerator
                filters.append(f"[{masked}]null{transition_filters(item,following,timeline)},tpad=stop_mode=clone:stop_duration={frame_pad},trim=end_frame={item.duration_frames},setpts=N*{timeline.frame_rate.denominator}/({timeline.frame_rate.numerator}*TB)[{label}]")
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
                        f"aformat=sample_rates=48000:channel_layouts=stereo,apad,atrim=duration={Decimal(item.duration_frames)*timeline.frame_rate.denominator/timeline.frame_rate.numerator}[{label}]"
                    )
                    audio_labels.append(f"[{label}]")
            else:
                # A compiled rough cut may not yet have an editable audio track.
                # Keep the camera audio aligned with every video clip so the user
                # can still judge the cut. Silent source clips get matching silence.
                for input_index, duration, source, item in video_inputs:
                    label = f"a{len(audio_labels)}"
                    if source.audio_stream_indices:
                        stream = source.audio_stream_indices[0]
                        base_label = f"abase{len(audio_labels)}"
                        filters.append(
                            f"[{input_index}:{stream}]atrim=duration={duration},"
                            "asetpts=PTS-STARTPTS,aresample=48000,"
                            f"aformat=sample_rates=48000:channel_layouts=stereo[{base_label}]"
                        )
                        current_label = self._retime_audio(
                            filters, base_label, item, float(duration), len(audio_labels)
                        )
                        from .production_effects import camera_audio_filters
                        filters.append(f"[{current_label}]anull{camera_audio_filters(item)},apad,atrim=duration={Decimal(item.duration_frames)*timeline.frame_rate.denominator/timeline.frame_rate.numerator},asetpts=PTS-STARTPTS[{label}]")
                    else:
                        filters.append(
                            "anullsrc=r=48000:cl=stereo,"
                            f"atrim=duration={Decimal(item.duration_frames)*timeline.frame_rate.denominator/timeline.frame_rate.numerator:.6f}[{label}]"
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
                # Programme camera sound is a stem: automatic loudness makeup
                # here would undo explicit attenuation before the music mix.
                finishing = 'alimiter=limit=0.841395:level=0' if controlled_audio else 'loudnorm=I=-16:LRA=11:TP=-1.5'
                filters.append(
                    f"[asequence]apad,atrim=duration={timeline_duration},{finishing}[aout]"
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
                    "-ar",
                    "48000",
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
            if self.hardware_acceleration:
                position = command.index('libx264')
                command[position] = 'h264_videotoolbox'
                for option in ('-preset', '-crf'):
                    index = command.index(option)
                    del command[index:index + 2]
                command[-1:-1] = ['-b:v', '8000k']
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
        if self.hardware_acceleration:
            command.extend(['-hwaccel', 'videotoolbox'])
        command.extend(
            [
                "-seek_timestamp", "1",
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
        *, preserve_audio_gain: bool = False,
    ) -> None:
        total_frames = sum(item.duration_frames for item in video.items)
        completed_frames = 0
        with tempfile.TemporaryDirectory(
            prefix=".clipen-preview-", dir=output.parent
        ) as temporary:
            directory = Path(temporary)
            segments = []
            segment_durations = []
            for batch_index, offset in enumerate(
                range(0, len(video.items), self._BATCH_SIZE)
            ):
                if cancel.is_set():
                    raise PreviewRenderError("cancelled")
                source_items = video.items[offset : offset + self._BATCH_SIZE]
                cursor = 0
                normalized_items = []
                for local_index,item in enumerate(source_items):
                    assert isinstance(item, MediaClip)
                    effects=item.effects
                    position=offset+local_index
                    if local_index==len(source_items)-1 and position+1<len(video.items):
                        incoming=next((e for e in video.items[position+1].effects if e.effect_id=='video.transition'),None)
                        if incoming:
                            effects+=(EffectRequest('video.transition_out',incoming.parameters),)
                    normalized_items.append(
                        MediaClip(
                            item.item_id,
                            cursor,
                            item.duration_frames,
                            item.source,
                            effects,
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
                    production_session_id=timeline.production_session_id,
                )
                segment = directory / f"segment-{batch_index:04d}.mp4"
                base = completed_frames

                def batch_progress(value: float, *, base=base, size=cursor) -> None:
                    progress(0.98 * (base + value * size) / total_frames)

                self._render_media(
                    project_id,
                    batch,
                    segment,
                    cancel,
                    batch_progress,
                    preserve_audio_gain=preserve_audio_gain,
                )
                segments.append(segment)
                segment_durations.append(self._timeline_seconds(batch))
                completed_frames += cursor
            self._concat_segments(segments, output, cancel, segment_durations)
        progress(1.0)

    def _concat_segments(
        self, segments: list[Path], output: Path, cancel: threading.Event,
        durations=None,
    ) -> None:
        manifest = segments[0].parent / "segments.txt"
        manifest.write_text(
            # AAC encoder padding may make a segment's container longer than
            # its video. Letting concat infer that duration inserts a timestamp
            # gap at each batch, changing avg_frame_rate despite correct frames.
            "".join(f"file '{segment.name}'\n"+(f'duration {durations[n]}\n' if durations is not None else '') for n,segment in enumerate(segments)),
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
    def _video_effect_filters(item: MediaClip, timeline: EditTimeline) -> str:
        """Map allowlisted semantic effects to bounded FFmpeg video filters."""

        filters = ""
        for effect in item.effects:
            parameters = effect.parameters
            if effect.effect_id == "video.punch_in":
                scale = min(2.0, max(1.01, float(parameters.get("scale", 1.08))))
                center_x = min(1.0, max(0.0, float(parameters.get("center_x", 0.5))))
                center_y = min(1.0, max(0.0, float(parameters.get("center_y", 0.5))))
                filters += (
                    f",scale=iw*{scale:.4f}:ih*{scale:.4f},"
                    f"crop={timeline.width}:{timeline.height}:"
                    f"(iw-ow)*{center_x:.4f}:(ih-oh)*{center_y:.4f}"
                )
            elif effect.effect_id == "video.slow_push":
                start = min(2.0, max(1.0, float(parameters.get("scale_from", 1.0))))
                end = min(2.0, max(1.0, float(parameters.get("scale_to", 1.08))))
                frames = max(1, item.duration_frames - 1)
                filters += (
                    f",zoompan=z='{start:.4f}+({end:.4f}-{start:.4f})*on/{frames}':"
                    "x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:"
                    f"s={timeline.width}x{timeline.height}:"
                    f"fps={timeline.frame_rate.numerator}/{timeline.frame_rate.denominator}"
                )
            elif effect.effect_id == "video.reframe":
                x = min(1.0, max(0.0, float(parameters.get("x", 0.5))))
                y = min(1.0, max(0.0, float(parameters.get("y", 0.5))))
                filters += (
                    ",scale=iw*1.08:ih*1.08,"
                    f"crop={timeline.width}:{timeline.height}:(iw-ow)*{x:.4f}:(ih-oh)*{y:.4f}"
                )
        return filters

    @classmethod
    def _retime_video(
        cls,
        filters: list[str],
        source_label: str,
        item: MediaClip,
        duration: float,
        index: int,
    ) -> str:
        profile = cls._speed_profile(item)
        if profile is None:
            return source_label
        outside_rate, center_rate = profile
        first_end, center_end = duration * 0.4, duration * 0.6
        filters.append(
            f"[{source_label}]split=3[vr{index}a][vr{index}b][vr{index}c]"
        )
        filters.extend(
            (
                f"[vr{index}a]trim=start=0:end={first_end:.6f},"
                f"setpts=(PTS-STARTPTS)/{outside_rate:.6f}[vr{index}ao]",
                f"[vr{index}b]trim=start={first_end:.6f}:end={center_end:.6f},"
                f"setpts=(PTS-STARTPTS)/{center_rate:.6f}[vr{index}bo]",
                f"[vr{index}c]trim=start={center_end:.6f}:end={duration:.6f},"
                f"setpts=(PTS-STARTPTS)/{outside_rate:.6f}[vr{index}co]",
                f"[vr{index}ao][vr{index}bo][vr{index}co]concat=n=3:v=1:a=0[vr{index}out]",
            )
        )
        return f"vr{index}out"

    @classmethod
    def _retime_audio(
        cls,
        filters: list[str],
        source_label: str,
        item: MediaClip,
        duration: float,
        index: int,
    ) -> str:
        profile = cls._speed_profile(item)
        if profile is None:
            return source_label
        outside_rate, center_rate = profile
        first_end, center_end = duration * 0.4, duration * 0.6
        outside_tempo = cls._atempo(outside_rate)
        center_tempo = cls._atempo(center_rate)
        filters.append(
            f"[{source_label}]asplit=3[ar{index}a][ar{index}b][ar{index}c]"
        )
        filters.extend(
            (
                f"[ar{index}a]atrim=start=0:end={first_end:.6f},"
                f"asetpts=PTS-STARTPTS,{outside_tempo}[ar{index}ao]",
                f"[ar{index}b]atrim=start={first_end:.6f}:end={center_end:.6f},"
                f"asetpts=PTS-STARTPTS,{center_tempo}[ar{index}bo]",
                f"[ar{index}c]atrim=start={center_end:.6f}:end={duration:.6f},"
                f"asetpts=PTS-STARTPTS,{outside_tempo}[ar{index}co]",
                f"[ar{index}ao][ar{index}bo][ar{index}co]concat=n=3:v=0:a=1[ar{index}out]",
            )
        )
        return f"ar{index}out"

    @staticmethod
    def _speed_profile(item: MediaClip) -> tuple[float, float] | None:
        effect = next(
            (value for value in item.effects if value.effect_id == "video.speed_ramp"),
            None,
        )
        if effect is None:
            return None
        center_rate = min(4.0, max(0.25, float(effect.parameters.get("rate", 1.25))))
        if abs(center_rate - 1.0) < 0.001:
            return None
        # The center 20% receives the requested rate. The two outer 40%
        # sections compensate evenly so the clip keeps its exact timeline
        # duration and remains aligned with every following edit.
        outside_rate = 0.8 / (1.0 - 0.2 / center_rate)
        return outside_rate, center_rate

    @staticmethod
    def _atempo(rate: float) -> str:
        values = []
        remaining = rate
        while remaining < 0.5:
            values.append(0.5)
            remaining /= 0.5
        while remaining > 2.0:
            values.append(2.0)
            remaining /= 2.0
        values.append(remaining)
        return ",".join(f"atempo={value:.6f}" for value in values)

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
