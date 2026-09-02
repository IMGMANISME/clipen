"""Fixed-shape subprocess entry point for the optional MLX Whisper runtime."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import stat
import sys
import wave


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--model", required=True)
    parser.add_argument("--audio")
    parser.add_argument("--output")
    parser.add_argument("--language", default="zh")
    return parser.parse_args()


def _regular(path: str, *, directory: bool = False) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute() or candidate.is_symlink():
        raise ValueError("unsafe path")
    info = os.lstat(candidate)
    expected = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not expected or (not directory and info.st_nlink != 1):
        raise ValueError("unsafe path")
    return candidate.resolve(strict=True)


def _write_result(path: Path, value: dict[str, object]) -> None:
    flags = os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("unsafe output")
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        with os.fdopen(descriptor, "wb", closefd=False) as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
    finally:
        os.close(descriptor)


def _load_normalized_audio(path: Path):
    import numpy

    with wave.open(str(path), "rb") as source:
        if (
            source.getnchannels() != 1
            or source.getsampwidth() != 2
            or source.getframerate() != 16_000
            or not 1 <= source.getnframes() <= 16_000 * 30 * 60
        ):
            raise ValueError("unsupported normalized audio")
        frame_count = source.getnframes()
        frames = source.readframes(frame_count)
        if source.readframes(1):
            raise ValueError("unexpected trailing audio")
    if len(frames) != frame_count * 2:
        raise ValueError("truncated normalized audio")
    return numpy.frombuffer(frames, dtype="<i2").astype(numpy.float32) / 32768.0


def main() -> int:
    arguments = _arguments()
    model = _regular(arguments.model, directory=True)
    if arguments.probe:
        print(json.dumps({"mlx_whisper": importlib.metadata.version("mlx-whisper")}))
        return 0
    if not arguments.audio or not arguments.output:
        return 2
    audio = _regular(arguments.audio)
    output = _regular(arguments.output)
    import mlx_whisper

    raw = mlx_whisper.transcribe(
        _load_normalized_audio(audio),
        path_or_hf_repo=str(model),
        language=arguments.language,
        word_timestamps=True,
        verbose=False,
    )
    segments = []
    for segment in raw.get("segments", []):
        words = [
            {
                "start": float(word["start"]),
                "end": float(word["end"]),
                "text": str(word["word"]),
                "probability": float(word["probability"]),
            }
            for word in segment.get("words", [])
        ]
        segments.append(
            {
                "start": float(segment["start"]),
                "end": float(segment["end"]),
                "text": str(segment["text"]),
                "no_speech_probability": float(segment["no_speech_prob"]),
                "words": words,
            }
        )
    _write_result(
        output,
        {
            "language": str(raw.get("language", arguments.language)),
            "text": str(raw.get("text", "")),
            "segments": segments,
        },
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError):
        raise SystemExit(1)
