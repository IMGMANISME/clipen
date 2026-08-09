from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from clipen.utils.media import probe_duration


@dataclass(frozen=True)
class RenderResult:
    output_path: Path
    duration: float
    clip_count: int


def render_summary(output_path: Path, clip_count: int) -> RenderResult:
    return RenderResult(
        output_path=output_path, duration=probe_duration(output_path), clip_count=clip_count
    )
