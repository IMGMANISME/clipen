from __future__ import annotations

from pathlib import Path


def escape_path_for_filter(path: Path) -> str:
    """ffmpeg filter arguments treat ':' and '\\' specially -- escape a
    filesystem path before interpolating it into a filtergraph string."""
    return str(path).replace("\\", "\\\\").replace(":", "\\:")


def burn_in_filter(captions_path: Path) -> str:
    return f"subtitles={escape_path_for_filter(captions_path)}"
