"""Turns a clip's frames into one short text description via a
vision-capable LLM (typically a local, free model -- see `llm/factory.py`
and `cli/auth.py`). This is the local half of the hybrid local-VLM +
cloud-LLM design: the cloud narrative call in `editing/narrative.py`
never sees an image, only this pre-digested sentence.

Deliberately not called from `project/manager.py`'s `analyze()` stage --
that stage is documented and tested as local-only/zero-LLM, and calling
even a local model there would break that guarantee for every project,
including ones that never asked for vision at all. This function is only
ever invoked from `editing/narrative.py`, which itself only runs during
`edit(vision_provider_name=...)`.
"""

from __future__ import annotations

from pathlib import Path

from clipen.analysis.cache import get_or_compute
from clipen.analysis.hashing import fingerprint
from clipen.analysis.shots import Shot
from clipen.llm.base import LLMClient, LLMMessage
from clipen.utils.media import extract_frame_jpeg

_PROMPT = (
    "These are frames from one raw video clip, in chronological order. "
    "In 1-2 sentences: what's happening, and is anything visually notable "
    "(great composition, shaky/blurry, generic B-roll)?"
)


def _sample_frame_times(shots: list[Shot], max_frames: int) -> list[float]:
    midpoints = [(s["start"] + s["end"]) / 2 for s in shots] or [0.0]
    if len(midpoints) <= max_frames:
        return midpoints
    step = (len(midpoints) - 1) / (max_frames - 1) if max_frames > 1 else 0
    indices = sorted({round(i * step) for i in range(max_frames)})
    return [midpoints[i] for i in indices]


def describe_clip_visually(
    path: Path,
    shots: list[Shot],
    vlm_client: LLMClient,
    *,
    model_tag: str,
    max_frames: int = 6,
) -> str:
    fp = fingerprint(path)
    version_tag = f"vlm-{model_tag}-{max_frames}f"

    def compute() -> str:
        try:
            frame_times = _sample_frame_times(shots, max_frames)
            frames = tuple(extract_frame_jpeg(path, t) for t in frame_times)
            if not frames:
                return ""
            response = vlm_client.complete(
                [LLMMessage(role="user", content=_PROMPT, images=frames)],
                max_tokens=150,
            )
            return response.text.strip()
        except Exception:
            return ""

    return get_or_compute(fp, "visual_summary", version_tag, compute)
