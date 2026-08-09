"""Local silence/dead-air detection via ffmpeg's silencedetect filter."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import TypedDict

from clipen.analysis.cache import get_or_compute
from clipen.analysis.hashing import fingerprint

DEFAULT_NOISE_THRESHOLD_DB = -30.0
DEFAULT_MIN_SILENCE_DURATION = 0.5

_SILENCE_START_RE = re.compile(r"silence_start:\s*(?P<t>[\d.]+)")
_SILENCE_END_RE = re.compile(r"silence_end:\s*(?P<t>[\d.]+)")


class SilenceSpan(TypedDict):
    start: float
    end: float


def detect_silence(
    path: Path,
    *,
    noise_threshold_db: float = DEFAULT_NOISE_THRESHOLD_DB,
    min_silence_duration: float = DEFAULT_MIN_SILENCE_DURATION,
) -> list[SilenceSpan]:
    fp = fingerprint(path)
    version_tag = f"ffmpeg-silencedetect-{noise_threshold_db:g}dB-{min_silence_duration:g}s"

    def compute() -> list[SilenceSpan]:
        cmd = [
            "ffmpeg",
            "-i",
            str(path),
            "-af",
            f"silencedetect=noise={noise_threshold_db}dB:d={min_silence_duration}",
            "-f",
            "null",
            "-",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        spans: list[SilenceSpan] = []
        pending_start: float | None = None
        for line in result.stderr.splitlines():
            start_match = _SILENCE_START_RE.search(line)
            if start_match:
                pending_start = float(start_match.group("t"))
                continue
            end_match = _SILENCE_END_RE.search(line)
            if end_match and pending_start is not None:
                spans.append({"start": pending_start, "end": float(end_match.group("t"))})
                pending_start = None
        return spans

    return get_or_compute(fp, "silence", version_tag, compute)
