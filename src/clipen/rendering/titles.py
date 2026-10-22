"""Rasterized title cards and a bounded FFmpeg overlay pass."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from typing import Callable, Iterable

from clipen.domain import EditTimeline, TextClip, TrackKind


class TitleRenderError(RuntimeError):
    """Raised when a trusted timeline cannot be composed locally."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class RenderedTitle:
    path: Path
    start_seconds: float
    duration_seconds: float


class TitleCardRenderer:
    """Turn semantic title styles into transparent, full-canvas PNG overlays."""

    # Each card becomes one bounded FFmpeg input during the finishing pass.
    # Keep this below the point where full-HD RGBA frames can create avoidable
    # memory pressure on consumer machines.
    _MAX_TITLES = 32

    def __init__(self,custom_styles=None):
        self.custom_styles=custom_styles or {}

    def render(self, timeline: EditTimeline, directory: Path) -> tuple[RenderedTitle, ...]:
        items = tuple(
            item
            for track in timeline.tracks
            if track.kind is TrackKind.TITLE
            for item in track.items
        )
        if len(items) > self._MAX_TITLES:
            raise TitleRenderError("preview_title_limit_exceeded")
        if not items:
            return ()
        try:
            from PIL import Image, ImageDraw, ImageFont
        except ImportError as error:
            raise TitleRenderError("preview_title_runtime_missing") from error

        font_path = self._font_path()
        if font_path is None:
            raise TitleRenderError("preview_title_font_missing")
        frame_rate = timeline.frame_rate.numerator / timeline.frame_rate.denominator
        rendered = []
        for index, item in enumerate(items):
            assert isinstance(item, TextClip)
            image = Image.new("RGBA", (timeline.width, timeline.height), (0, 0, 0, 0))
            draw = ImageDraw.Draw(image)
            self._draw_card(draw, ImageFont, font_path, timeline, item)
            path = directory / f"title-{index:04d}.png"
            image.save(path, "PNG", optimize=True)
            rendered.append(
                RenderedTitle(
                    path,
                    item.start_frame / frame_rate,
                    item.duration_frames / frame_rate,
                )
            )
        return tuple(rendered)

    @staticmethod
    def _font_path() -> Path | None:
        configured = os.environ.get("CLIPEN_TITLE_FONT")
        candidates = (
            Path(configured).expanduser() if configured else None,
            Path("/System/Library/Fonts/Hiragino Sans GB.ttc"),
            Path("/System/Library/Fonts/STHeiti Light.ttc"),
            Path("/System/Library/Fonts/PingFang.ttc"),
            Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
            Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
            Path("C:/Windows/Fonts/msjh.ttc"),
        )
        return next((path for path in candidates if path is not None and path.is_file()), None)

    def _draw_card(
        self, draw, image_font, font_path: Path, timeline: EditTimeline, item: TextClip, relative_frame=None
    ) -> None:
        width, height = timeline.width, timeline.height
        if self.custom_styles.get(item.style_id,{}).get('kind')=='program_component':
            from .program_components import draw_program_component
            draw_program_component(draw,font_path,timeline,item,self.custom_styles[item.style_id],relative_frame)
            return
        if self.custom_styles.get(item.style_id,{}).get('kind')=='program_subtitle':
            from .program_subtitles import draw_program_subtitle
            draw_program_subtitle(draw,font_path,timeline,item,self.custom_styles[item.style_id],relative_frame)
            return
        if item.style_id in self.custom_styles:
            profile=self.custom_styles[item.style_id]
            size=max(1,round(height*profile['font_size_ratio']))
            font=image_font.truetype(str(font_path),size)
            maximum=round(width*profile['width_ratio'])
            lines=self._wrap(draw,item.text,font,maximum)
            line_height=max(1,round(size*1.5));pad=max(1,round(size*.25))
            x=round(width*profile['x']);y=round(height*profile['y'])
            if y+line_height*len(lines)+pad>height or x+maximum>width:
                raise TitleRenderError('preview_text_overflow')
            for row in lines:
                length=draw.textlength(row,font=font)
                at=x if profile['align']=='left' else x+(maximum-length)/(2 if profile['align']=='center' else 1)
                draw.rounded_rectangle((at-pad,y-pad,at+length+pad,y+line_height),radius=pad,fill=tuple(profile['background_color']))
                draw.text((at,y),row,font=font,fill=tuple(profile['text_color']));y+=line_height
            return
        if item.style_id == "title.chapter":
            size = max(28, round(height * 0.055))
            font = image_font.truetype(str(font_path), size)
            lines = self._wrap(draw, item.text, font, round(width * 0.55))
            box = self._text_box(draw, lines, font, round(size * 0.34))
            x, y = round(width * 0.065), round(height * 0.10)
            pad_x, pad_y = round(size * 0.58), round(size * 0.36)
            draw.rounded_rectangle(
                (x - pad_x, y - pad_y, x + box[0] + pad_x, y + box[1] + pad_y),
                radius=round(size * 0.42),
                fill=(37, 83, 62, 230),
            )
            self._multiline(draw, (x, y), lines, font, (255, 255, 248, 255), size)
            return

        if item.style_id == "title.caption":
            size = max(26, round(height * 0.043))
            font = image_font.truetype(str(font_path), size)
            lines = self._wrap(draw, item.text, font, round(width * 0.70))
            box = self._text_box(draw, lines, font, round(size * 0.32))
            x = round((width - box[0]) / 2)
            y = round(height * 0.79 - box[1] / 2)
            pad_x, pad_y = round(size * 0.62), round(size * 0.38)
            draw.rounded_rectangle(
                (x - pad_x + 4, y - pad_y + 7, x + box[0] + pad_x + 4, y + box[1] + pad_y + 7),
                radius=round(size * 0.36),
                fill=(0, 0, 0, 75),
            )
            draw.rounded_rectangle(
                (x - pad_x, y - pad_y, x + box[0] + pad_x, y + box[1] + pad_y),
                radius=round(size * 0.36),
                fill=(250, 249, 242, 238),
            )
            self._multiline(draw, (x, y), lines, font, (24, 27, 24, 255), size)
            return

        size = max(34, round(height * 0.072))
        font = image_font.truetype(str(font_path), size)
        lines = self._wrap(draw, item.text, font, round(width * 0.78))
        box = self._text_box(draw, lines, font, round(size * 0.30))
        x = round((width - box[0]) / 2)
        y = round((height - box[1]) / 2)
        pad_x, pad_y = round(size * 0.66), round(size * 0.44)
        draw.rounded_rectangle(
            (x - pad_x + 5, y - pad_y + 9, x + box[0] + pad_x + 5, y + box[1] + pad_y + 9),
            radius=round(size * 0.34),
            fill=(0, 0, 0, 90),
        )
        draw.rounded_rectangle(
            (x - pad_x, y - pad_y, x + box[0] + pad_x, y + box[1] + pad_y),
            radius=round(size * 0.34),
            fill=(19, 24, 21, 205),
        )
        self._multiline(draw, (x, y), lines, font, (255, 255, 250, 255), size)

    @staticmethod
    def _wrap(draw, text: str, font, maximum_width: int) -> tuple[str, ...]:
        lines: list[str] = []
        for paragraph in text.splitlines() or (text,):
            current = ""
            for character in paragraph:
                candidate = current + character
                if current and draw.textbbox((0, 0), candidate, font=font)[2] > maximum_width:
                    lines.append(current)
                    current = character
                else:
                    current = candidate
            if current:
                lines.append(current)
        return tuple(lines) or (text,)

    @staticmethod
    def _text_box(draw, lines: Iterable[str], font, spacing: int) -> tuple[int, int]:
        boxes = [draw.textbbox((0, 0), line, font=font) for line in lines]
        return max(box[2] - box[0] for box in boxes), sum(
            box[3] - box[1] for box in boxes
        ) + spacing * (len(boxes) - 1)

    @staticmethod
    def _multiline(draw, origin, lines: Iterable[str], font, fill, font_size: int) -> None:
        draw.multiline_text(
            origin,
            "\n".join(lines),
            font=font,
            fill=fill,
            spacing=round(font_size * 0.30),
            align="center",
            stroke_width=max(1, round(font_size * 0.018)),
            stroke_fill=(0, 0, 0, 45),
        )


class FfmpegTitleCompositor:
    """Overlay pre-rendered title cards without exposing timeline text to a shell."""

    def __init__(
        self,
        executable: str = "ffmpeg",
        *,
        popen: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
    ) -> None:
        self.executable = executable
        self._popen = popen

    def compose(
        self,
        source: Path,
        titles: tuple[RenderedTitle, ...],
        output: Path,
        cancel: threading.Event,
        duration_seconds: float,
        progress: Callable[[float], None],
    ) -> None:
        command = [self.executable, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source)]
        filters = []
        previous = "[0:v]"
        for index, title in enumerate(titles, start=1):
            command.extend(
                ["-loop", "1", "-t", f"{title.duration_seconds:.6f}", "-i", str(title.path)]
            )
            fade = min(0.18, title.duration_seconds / 4)
            fade_out = max(0.0, title.duration_seconds - fade)
            filters.append(
                f"[{index}:v]format=rgba,fade=t=in:st=0:d={fade:.3f}:alpha=1,"
                f"fade=t=out:st={fade_out:.3f}:d={fade:.3f}:alpha=1,"
                f"setpts=PTS+{title.start_seconds:.6f}/TB[title{index}]"
            )
            output_label = f"composed{index}"
            filters.append(
                f"{previous}[title{index}]overlay=0:0:eof_action=pass:shortest=0[{output_label}]"
            )
            previous = f"[{output_label}]"
        command.extend(
            [
                "-filter_complex", ";".join(filters),
                "-map", previous,
                "-map", "0:a?",
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-crf", "21",
                "-pix_fmt", "yuv420p",
                "-c:a", "copy",
                "-movflags", "+faststart",
                "-progress", "pipe:1",
                "-nostats",
                str(output),
            ]
        )
        with tempfile.TemporaryFile() as errors:
            try:
                process = self._popen(command, stdout=subprocess.PIPE, stderr=errors, text=True)
            except FileNotFoundError as error:
                raise TitleRenderError("ffmpeg_unavailable") from error
            except OSError as error:
                raise TitleRenderError("ffmpeg_start_failed") from error
            assert process.stdout is not None
            terminated = False
            try:
                for line in process.stdout:
                    if cancel.is_set() and not terminated:
                        process.terminate()
                        terminated = True
                    if line.startswith("out_time_ms="):
                        value = line.split("=", 1)[1].strip()
                        if value.isdigit() and duration_seconds > 0:
                            progress(min(0.99, int(value) / 1_000_000 / duration_seconds))
            finally:
                close_stdout = getattr(process.stdout, "close", None)
                if close_stdout is not None:
                    close_stdout()
            code = process.wait()
        if cancel.is_set():
            raise TitleRenderError("cancelled")
        if code != 0 or not output.is_file() or output.stat().st_size < 1:
            raise TitleRenderError("ffmpeg_render_failed")
        progress(1.0)


def compose_dense_titles(executable,source,timeline,directory,output,cancel,custom_styles=None):
    """One event-based RGBA stream avoids opening dozens of full-HD inputs."""
    from PIL import Image,ImageDraw,ImageFont
    from clipen.finishing.audio import run_process
    items=[i for t in timeline.tracks if t.kind is TrackKind.TITLE for i in t.items]
    if len(items)>(960 if timeline.production_session_id else 96):raise TitleRenderError('preview_title_limit_exceeded')
    font=TitleCardRenderer._font_path()
    if font is None:raise TitleRenderError('preview_title_font_missing')
    renderer=TitleCardRenderer(custom_styles);fps=timeline.frame_rate.numerator/timeline.frame_rate.denominator
    boundaries=sorted({0,timeline.duration_frames,*(i.start_frame for i in items),*(i.start_frame+i.duration_frames for i in items)})
    from .program_subtitles import animation_boundaries
    boundaries=sorted(set(boundaries)|{b for i in items if (custom_styles or {}).get(i.style_id,{}).get('kind')=='program_subtitle' for b in animation_boundaries(i)})
    from .program_components import animation_boundaries as component_boundaries
    boundaries=sorted(set(boundaries)|{b for i in items if (custom_styles or {}).get(i.style_id,{}).get('kind')=='program_component'
                                      for b in component_boundaries(i,custom_styles[i.style_id])})
    lines=['ffconcat version 1.0']
    for index,(start,end) in enumerate(zip(boundaries,boundaries[1:])):
        if cancel.is_set():raise TitleRenderError('cancelled')
        image=Image.new('RGBA',(timeline.width,timeline.height),(0,0,0,0));draw=ImageDraw.Draw(image)
        for item in items:
            if item.start_frame<=start<item.start_frame+item.duration_frames:renderer._draw_card(draw,ImageFont,font,timeline,item,start-item.start_frame)
        name=f'text-event-{index}.png';image.save(directory/name)
        lines.extend((f"file '{name}'",f'option framerate {timeline.frame_rate.numerator}/{timeline.frame_rate.denominator}',f'duration {(end-start)/fps:.9f}'))
    lines.extend((f"file 'text-event-{len(boundaries)-2}.png'",f'option framerate {timeline.frame_rate.numerator}/{timeline.frame_rate.denominator}'))
    manifest=directory/'text-events.ffconcat';manifest.write_text('\n'.join(lines)+'\n')
    run_process([executable,'-v','error','-nostdin','-y','-filter_complex_threads','2','-i',str(source),'-f','concat','-safe','0','-i',str(manifest),
                 '-filter_complex','[0:v][1:v]overlay=eof_action=repeat:format=auto,format=yuv420p[v]',
                 '-map','[v]','-map','0:a?','-t',str(timeline.duration_frames/fps),'-r',f'{timeline.frame_rate.numerator}/{timeline.frame_rate.denominator}',
                 '-c:v','libx264','-preset','veryfast','-crf','21','-c:a','copy','-movflags','+faststart',str(output)],cancel,timeout=max(300,round(timeline.duration_frames/fps*3)))
