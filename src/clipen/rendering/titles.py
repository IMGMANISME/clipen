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
        self, draw, image_font, font_path: Path, timeline: EditTimeline, item: TextClip
    ) -> None:
        width, height = timeline.width, timeline.height
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
        return tuple(lines[:4]) or (text,)

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
