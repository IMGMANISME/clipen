"""Local speech-to-text via faster-whisper. Zero LLM calls, zero tokens."""

from __future__ import annotations

from pathlib import Path
from typing import TypedDict

from clipen.analysis.cache import get_or_compute
from clipen.analysis.hashing import fingerprint

DEFAULT_MODEL_SIZE = "small"

_model_cache: dict[str, object] = {}


class TranscriptWord(TypedDict):
    start: float
    end: float
    text: str


class TranscriptSegment(TypedDict):
    start: float
    end: float
    text: str
    words: list[TranscriptWord]


def _get_model(model_size: str):
    if model_size not in _model_cache:
        from faster_whisper import WhisperModel

        _model_cache[model_size] = WhisperModel(model_size, compute_type="int8")
    return _model_cache[model_size]


def transcribe(path: Path, *, model_size: str = DEFAULT_MODEL_SIZE) -> list[TranscriptSegment]:
    fp = fingerprint(path)

    def compute() -> list[TranscriptSegment]:
        model = _get_model(model_size)
        segments, _info = model.transcribe(str(path), word_timestamps=True)
        result: list[TranscriptSegment] = []
        for seg in segments:
            result.append(
                {
                    "start": seg.start,
                    "end": seg.end,
                    "text": seg.text.strip(),
                    "words": [
                        {"start": w.start, "end": w.end, "text": w.word.strip()}
                        for w in (seg.words or [])
                    ],
                }
            )
        return result

    return get_or_compute(fp, "transcript", f"faster-whisper-{model_size}", compute)
