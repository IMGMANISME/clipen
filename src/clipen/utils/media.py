"""Thin ffprobe/ffmpeg wrappers. Deliberately free of analysis/llm/auth
dependencies so every other layer can import it without pulling in the
rest of the app."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


def ffprobe_json(path: Path) -> dict:
    cmd = [
        "ffprobe",
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def probe_duration(path: Path) -> float:
    data = ffprobe_json(path)
    return float(data["format"]["duration"])


def has_stream_of_type(path: Path, codec_type: str) -> bool:
    data = ffprobe_json(path)
    return any(stream.get("codec_type") == codec_type for stream in data.get("streams", []))


def extract_frame_jpeg(path: Path, timestamp: float) -> bytes:
    """One JPEG frame at `timestamp` seconds into `path`, as raw bytes --
    used by `analysis/visual.py` to build frames for a vision-capable
    LLM without ever writing a temp file to disk."""
    cmd = [
        "ffmpeg",
        "-ss",
        f"{timestamp}",
        "-i",
        str(path),
        "-frames:v",
        "1",
        "-f",
        "image2pipe",
        "-vcodec",
        "mjpeg",
        "-",
    ]
    result = subprocess.run(cmd, capture_output=True, check=True)
    return result.stdout
