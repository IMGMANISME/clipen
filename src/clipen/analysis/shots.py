"""Local shot/scene boundary detection via PySceneDetect. Zero LLM calls."""

from __future__ import annotations

from pathlib import Path
from typing import TypedDict

from clipen.analysis.cache import get_or_compute
from clipen.analysis.hashing import fingerprint
from clipen.utils.media import probe_duration

VERSION_TAG = "pyscenedetect-adaptive-v1"


class Shot(TypedDict):
    start: float
    end: float


def detect_shots(path: Path) -> list[Shot]:
    fp = fingerprint(path)

    def compute() -> list[Shot]:
        from scenedetect import AdaptiveDetector, detect

        scene_list = detect(str(path), AdaptiveDetector())
        if not scene_list:
            duration = probe_duration(path)
            return [{"start": 0.0, "end": duration}]
        return [{"start": start.seconds, "end": end.seconds} for start, end in scene_list]

    return get_or_compute(fp, "shots", VERSION_TAG, compute)
