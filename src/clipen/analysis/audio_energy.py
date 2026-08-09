"""Average loudness of a specific span of a clip. Not cached like the
other analysis stages -- it's called narrowly, on cheap short segments,
only during `edit`, so a full fingerprint-cache entry isn't worth the
bookkeeping."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

_RMS_RE = re.compile(r"RMS level dB:\s*(-?[\d.]+|-inf)")


def segment_rms_db(path: Path, start: float, end: float) -> float:
    duration = max(0.0, end - start)
    cmd = [
        "ffmpeg",
        "-ss",
        f"{start}",
        "-t",
        f"{duration}",
        "-i",
        str(path),
        "-af",
        "astats=metadata=0:reset=1",
        "-f",
        "null",
        "-",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    values = [
        float(m.group(1)) for m in _RMS_RE.finditer(result.stderr) if m.group(1) != "-inf"
    ]
    if not values:
        return float("-inf")
    return sum(values) / len(values)
