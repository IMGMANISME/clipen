"""Persistent Qwen3-ASR worker with confidence-based hallucination rejection."""

from __future__ import annotations

import argparse
from decimal import Decimal
import importlib.metadata
import json
import math
import os
from pathlib import Path
import stat
import sys
import unicodedata
import wave


_MAX_REQUEST_BYTES = 64 * 1024
_MAX_CAPTURE_BYTES = 32 * 1024 * 1024
_MAX_AUDIO_SECONDS = 5 * 60 + 1
_WINDOW_SECONDS = 12
_MAX_NEW_TOKENS = 128
_REVIEW_SCORE = 0.64
_MAX_ENHANCED_RETRIES = 4
_MODEL_FILES = {
    "chat_template.json",
    "config.json",
    "generation_config.json",
    "merges.txt",
    "model.safetensors",
    "preprocessor_config.json",
    "tokenizer_config.json",
    "vocab.json",
}


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--model", required=True)
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


def _model_files(model: Path) -> dict[str, Path]:
    children = tuple(model.iterdir())
    observed = {candidate.name for candidate in children if candidate.is_file()}
    if observed != _MODEL_FILES or any(candidate.is_symlink() for candidate in children):
        raise ValueError("unsafe model")
    return {name: _regular(str(model / name)) for name in _MODEL_FILES}


def _response(value: dict[str, object]) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")), flush=True)


def _write_result(path: Path, value: dict[str, object]) -> None:
    flags = os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("unsafe output")
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        if len(encoded) > _MAX_CAPTURE_BYTES:
            raise ValueError("result too large")
        with os.fdopen(descriptor, "wb", closefd=False) as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
    finally:
        os.close(descriptor)


def _read_audio(path: Path):
    import numpy as np

    with wave.open(str(path), "rb") as audio:
        if (
            audio.getframerate() != 16_000
            or audio.getnchannels() != 1
            or audio.getsampwidth() != 2
            or audio.getnframes() < 1
        ):
            raise ValueError("unsupported audio")
        frame_count = audio.getnframes()
        duration = frame_count / audio.getframerate()
        samples = np.frombuffer(audio.readframes(frame_count), dtype="<i2").astype(
            np.float32
        ) / 32768.0
    if duration > _MAX_AUDIO_SECONDS:
        raise ValueError("audio chunk too long")
    return samples


def _window_ranges(samples, sample_rate: int = 16_000) -> list[tuple[int, int]]:
    """Create bounded windows and move cuts toward nearby low-energy frames."""

    import numpy as np

    search_frame = max(1, sample_rate // 50)
    usable = int(samples.shape[0]) // search_frame * search_frame
    frames = samples[:usable].reshape(-1, search_frame)
    energies = np.mean(frames * frames, axis=1).tolist()
    return _window_ranges_from_energy(
        int(samples.shape[0]), energies, search_frame, sample_rate
    )


def _window_ranges_from_energy(
    total: int, energies: list[float], frame_size: int, sample_rate: int = 16_000
) -> list[tuple[int, int]]:
    maximum = _WINDOW_SECONDS * sample_rate
    minimum = 8 * sample_rate
    ranges: list[tuple[int, int]] = []
    start = 0
    while start < total:
        hard_end = min(total, start + maximum)
        end = hard_end
        if hard_end < total:
            search_start = start + minimum
            candidates = [
                (energies[index], index * frame_size)
                for index in range(
                    (search_start + frame_size - 1) // frame_size,
                    min(len(energies), hard_end // frame_size),
                )
            ]
            if candidates:
                end = min(candidates)[1]
        if end <= start:
            end = hard_end
        ranges.append((start, end))
        start = end
    return ranges


def _has_audible_activity(samples, sample_rate: int = 16_000) -> bool:
    """Skip digital silence and very low camera noise before loading the model."""

    import numpy as np

    if int(samples.shape[0]) < sample_rate // 2:
        return False
    frame_size = max(1, sample_rate // 50)
    usable = int(samples.shape[0]) // frame_size * frame_size
    if usable < frame_size:
        return False
    frames = samples[:usable].reshape(-1, frame_size)
    rms = np.sqrt(np.mean(frames * frames, axis=1))
    return _audible_from_rms(rms.tolist(), int(samples.shape[0]), sample_rate)


def _audible_from_rms(
    rms: list[float], sample_count: int, sample_rate: int = 16_000
) -> bool:
    if sample_count < sample_rate // 2 or not rms:
        return False
    return max(rms) >= 0.006 and sum(value >= 0.003 for value in rms) / len(rms) >= 0.02


def _percentile(values: list[float], fraction: float) -> float:
    if not values or not 0 <= fraction <= 1:
        raise ValueError("invalid percentile")
    ordered = sorted(float(value) for value in values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _quality_from_rms(
    rms: list[float], *, peak: float, clipping_ratio: float
) -> dict[str, float | bool]:
    """Summarize capture quality without claiming that energy equals speech."""

    if (
        not rms
        or not math.isfinite(peak)
        or not math.isfinite(clipping_ratio)
        or peak < 0
        or not 0 <= clipping_ratio <= 1
        or any(not math.isfinite(value) or value < 0 for value in rms)
    ):
        raise ValueError("invalid audio quality input")
    noise_floor = _percentile(rms, 0.2)
    foreground = _percentile(rms, 0.9)
    denominator = max(noise_floor, 0.0005)
    snr_db = 20 * math.log10(max(foreground, denominator) / denominator)
    active_threshold = max(0.003, noise_floor * 2.5)
    active_ratio = sum(value >= active_threshold for value in rms) / len(rms)
    quiet = foreground < 0.025
    noisy = noise_floor >= 0.0025 and snr_db < 12
    clipped = clipping_ratio >= 0.001
    return {
        "noise_floor": noise_floor,
        "foreground_rms": foreground,
        "snr_db": snr_db,
        "active_ratio": active_ratio,
        "peak": peak,
        "clipping_ratio": clipping_ratio,
        "quiet": quiet,
        "noisy": noisy,
        "clipped": clipped,
        "needs_enhancement": quiet or noisy,
    }


def _audio_quality(samples, sample_rate: int = 16_000) -> dict[str, float | bool]:
    import numpy as np

    frame_size = max(1, sample_rate // 50)
    usable = int(samples.shape[0]) // frame_size * frame_size
    if usable < frame_size:
        raise ValueError("audio window is too short")
    frames = samples[:usable].reshape(-1, frame_size)
    rms = np.sqrt(np.mean(frames * frames, axis=1)).tolist()
    absolute = np.abs(samples)
    return _quality_from_rms(
        rms,
        peak=float(np.max(absolute)),
        clipping_ratio=float(np.mean(absolute >= 0.985)),
    )


def _enhance_for_speech(samples, quality: dict[str, float | bool]):
    """Apply one bounded spectral-gating candidate to a short local window."""

    import numpy as np

    source = np.asarray(samples, dtype=np.float32)
    if source.ndim != 1 or source.size < 1 or not np.all(np.isfinite(source)):
        raise ValueError("invalid audio samples")
    frame_size = 512
    hop = 256
    if source.size < frame_size:
        output = source.astype(np.float32, copy=True)
    else:
        frame_count = 1 + math.ceil((source.size - frame_size) / hop)
        padded_size = (frame_count - 1) * hop + frame_size
        padded = np.pad(source, (0, padded_size - source.size))
        frames = np.lib.stride_tricks.sliding_window_view(padded, frame_size)[::hop]
        window = np.hanning(frame_size).astype(np.float32)
        spectrum = np.fft.rfft(frames * window, axis=1)
        magnitude = np.abs(spectrum)
        noise = np.quantile(magnitude, 0.2, axis=0)
        strength = 1.2 if bool(quality["noisy"]) else 0.8
        floor = 0.12 if bool(quality["noisy"]) else 0.22
        gain = np.clip(
            1.0 - strength * noise[None, :] / np.maximum(magnitude, 1e-7),
            floor,
            1.0,
        )
        frequencies = np.fft.rfftfreq(frame_size, d=1 / 16_000)
        gain[:, frequencies < 80] *= 0.15
        reconstructed = np.fft.irfft(spectrum * gain, n=frame_size, axis=1)
        output = np.zeros(padded_size, dtype=np.float64)
        weights = np.zeros(padded_size, dtype=np.float64)
        synthesis = window.astype(np.float64)
        for index, frame in enumerate(reconstructed):
            start = index * hop
            output[start : start + frame_size] += frame * synthesis
            weights[start : start + frame_size] += synthesis * synthesis
        valid = weights > 1e-8
        output[valid] /= weights[valid]
        output = output[: source.size].astype(np.float32)
    level = float(np.quantile(np.abs(output), 0.95))
    if level > 1e-5:
        amplification = min(6.0, max(0.5, 0.18 / level))
        output = output * amplification
    peak = float(np.max(np.abs(output)))
    if peak > 0.95:
        output = output * (0.95 / peak)
    return np.clip(output, -1.0, 1.0).astype(np.float32)


def _retry_budget(sample_count: int, sample_rate: int = 16_000) -> int:
    if sample_count < 1 or sample_rate < 1:
        raise ValueError("invalid retry duration")
    minutes = sample_count / sample_rate / 60
    return min(_MAX_ENHANCED_RETRIES, max(1, math.ceil(minutes)))


def _semantic(value: str) -> str:
    return "".join(
        character.casefold()
        for character in value
        if unicodedata.category(character)[0] in {"L", "N"}
    )


def _looks_repetitive(value: str) -> bool:
    compact = _semantic(value)
    if len(compact) >= 12 and len(set(compact)) / len(compact) < 0.2:
        return True
    for width in range(1, min(16, len(compact) // 3 + 1)):
        phrase = compact[:width]
        if phrase and compact.count(phrase) >= 4 and len(phrase) * 4 >= len(compact) * 0.7:
            return True
    return False


def _confidence(probabilities: list[float]) -> tuple[float, float, float]:
    if not probabilities:
        return 0.0, 1.0, 0.0
    mean = sum(probabilities) / len(probabilities)
    low_ratio = sum(value < 0.5 for value in probabilities) / len(probabilities)
    # The aggregate remains conservative even when a few high-confidence tokens
    # hide an uncertain phrase. It is stored as phrase confidence downstream.
    score = min(mean, 1.0 - low_ratio)
    return mean, low_ratio, score


def _accept_candidate(
    text: str, probabilities: list[float], *, ended: bool
) -> tuple[bool, float]:
    compact = _semantic(text)
    _mean, low_ratio, score = _confidence(probabilities)
    if not ended or len(compact) < 2 or _looks_repetitive(text):
        return False, score
    # Token confidence can reject weak decoding and fluent hallucinations, but
    # it cannot prove that audible speech belongs to the foreground subject.
    # Capture stability is evaluated separately before downstream use.
    return score >= 0.68 and low_ratio < 0.32, score


def _candidate(runtime, convert, audio) -> dict[str, object]:
    text, probabilities, ended = _transcribe(runtime, audio)
    text = convert(text).strip()
    accepted, score = _accept_candidate(text, probabilities, ended=ended)
    return {"text": text, "accepted": accepted, "score": score}


def _should_retry(
    candidate: dict[str, object], quality: dict[str, float | bool]
) -> bool:
    # Model confidence was high for several labelled Pocket 3 near-homophone
    # errors. Capture quality, not model confidence alone, controls the bounded
    # comparison pass.
    del candidate
    return bool(quality["needs_enhancement"])


def _choose_candidate(
    original: dict[str, object], enhanced: dict[str, object] | None
) -> tuple[str, float] | None:
    original_ok = bool(original["accepted"])
    if enhanced is None:
        return (
            (str(original["text"]), float(original["score"]))
            if original_ok
            else None
        )
    enhanced_ok = bool(enhanced["accepted"])
    if original_ok and enhanced_ok:
        original_score = float(original["score"])
        enhanced_score = float(enhanced["score"])
        selected = enhanced if enhanced_score > original_score else original
        if _semantic(str(original["text"])) == _semantic(str(enhanced["text"])):
            return str(selected["text"]), max(original_score, enhanced_score)
        # Enhancement is a disagreement detector, not an authority. Preserve
        # the original capture's reading and make the uncertainty visible.
        return str(original["text"]), min(original_score, _REVIEW_SCORE)
    if original_ok:
        return str(original["text"]), float(original["score"])
    if enhanced_ok:
        # Speech recovered only after processing must remain visibly reviewable.
        return str(enhanced["text"]), min(float(enhanced["score"]), _REVIEW_SCORE)
    return None


def _selection_needs_review(
    original: dict[str, object],
    enhanced: dict[str, object] | None,
    quality: dict[str, float | bool],
) -> bool:
    """Require human review when weak capture has no stable second reading."""

    if not bool(quality["needs_enhancement"]):
        return False
    original_ok = bool(original["accepted"])
    enhanced_ok = enhanced is not None and bool(enhanced["accepted"])
    if not original_ok and not enhanced_ok:
        return False
    return not (
        original_ok
        and enhanced_ok
        and _semantic(str(original["text"])) == _semantic(str(enhanced["text"]))
    )


def _canonical_probability(value: float) -> str:
    decimal = Decimal(str(round(max(0.0, min(1.0, value)), 6))).normalize()
    return "0" if decimal == 0 else str(decimal)


def _transcribe(runtime, audio) -> tuple[str, list[float], bool]:
    import mlx.core as mx
    import numpy as np
    from mlx_speech.generation.qwen3_asr import (
        _first_token_id,
        parse_asr_output,
        replace_audio_embeddings,
    )

    features = runtime.processor.feature_extractor(audio, sample_rate=16_000)
    audio_length = int(features.audio_lengths.reshape(-1)[0])
    prompt = runtime.processor.build_prompt(
        context="", audio_length=audio_length, language="Chinese"
    )
    audio_features = runtime.model.get_audio_features(
        mx.array(features.input_features, dtype=mx.float32),
        feature_attention_mask=mx.array(features.feature_attention_mask, dtype=mx.int32),
    )
    mx.eval(audio_features)
    input_ids = mx.array([prompt.input_ids], dtype=mx.int32)
    embeddings = replace_audio_embeddings(
        input_ids,
        runtime.model.embed_input_ids(input_ids),
        audio_features,
        audio_token_id=runtime.config.audio_token_id,
    )
    prefill = runtime.model.prefill(
        inputs_embeds=embeddings,
        max_cache_len=len(prompt.input_ids) + _MAX_NEW_TOKENS,
    )
    mx.eval(prefill.logits)
    if prefill.past_key_values is None:
        raise RuntimeError("missing cache")
    logits = prefill.logits
    tokens: list[int] = []
    probabilities: list[float] = []
    eos = set(runtime.eos_token_ids)
    ended = False
    for _ in range(_MAX_NEW_TOKENS):
        token = _first_token_id(mx.argmax(logits[:, -1, :], axis=-1))
        if token in eos:
            ended = True
            break
        probability = mx.softmax(logits[:, -1, :].astype(mx.float32), axis=-1)[0, token]
        mx.eval(probability)
        probabilities.append(float(np.asarray(probability)))
        tokens.append(token)
        step = runtime.model.decode_step(
            input_ids=mx.array([[token]], dtype=mx.int32),
            kv_cache=prefill.past_key_values,
        )
        mx.eval(step.logits)
        logits = step.logits
    raw = runtime.processor.tokenizer.decode(tokens, skip_special_tokens=True).strip()
    _, text = parse_asr_output(raw, user_language=prompt.language)
    return text.strip(), probabilities, ended


def _result(runtime, convert, audio: Path) -> dict[str, object]:
    samples = _read_audio(audio)
    windows: list[dict[str, object]] = []
    for start_sample, end_sample in _window_ranges(samples):
        window = samples[start_sample:end_sample]
        if not _has_audible_activity(window):
            continue
        quality = _audio_quality(window)
        original = _candidate(runtime, convert, window)
        windows.append(
            {
                "start": start_sample,
                "end": end_sample,
                "audio": window,
                "quality": quality,
                "original": original,
                "enhanced": None,
            }
        )
    eligible = [
        (index, record)
        for index, record in enumerate(windows)
        if _should_retry(record["original"], record["quality"])  # type: ignore[arg-type]
    ]
    eligible.sort(
        key=lambda item: (
            bool(item[1]["original"]["accepted"]),  # type: ignore[index]
            float(item[1]["quality"]["active_ratio"]),  # type: ignore[index]
            float(item[1]["original"]["score"]),  # type: ignore[index]
        ),
        reverse=True,
    )
    for index, _record in eligible[: _retry_budget(int(samples.shape[0]))]:
        record = windows[index]
        enhanced_audio = _enhance_for_speech(
            record["audio"], record["quality"]  # type: ignore[arg-type]
        )
        record["enhanced"] = _candidate(runtime, convert, enhanced_audio)

    selections: list[tuple[dict[str, object], str, float, bool]] = []
    for record in windows:
        selected = _choose_candidate(
            record["original"], record["enhanced"]  # type: ignore[arg-type]
        )
        if selected is None:
            continue
        text, score = selected
        selections.append(
            (
                record,
                text,
                score,
                _selection_needs_review(
                    record["original"],  # type: ignore[arg-type]
                    record["enhanced"],  # type: ignore[arg-type]
                    record["quality"],  # type: ignore[arg-type]
                ),
            )
        )

    # One uncertain phrase makes the whole transcript unsafe for automatic
    # downstream use. Cap every phrase so averaging cannot hide that fact.
    transcript_needs_review = any(selection[3] for selection in selections)
    segments: list[dict[str, object]] = []
    for record, text, score, _needs_review in selections:
        if transcript_needs_review:
            score = min(score, _REVIEW_SCORE)
        start_sample = int(record["start"])
        end_sample = int(record["end"])
        start = start_sample / 16_000
        end = end_sample / 16_000
        probability = _canonical_probability(score)
        segments.append(
            {
                "start": start,
                "end": end,
                "text": text,
                "no_speech_probability": None,
                # Qwen3-ASR does not expose word timestamps. A single phrase
                # span retains confidence without inventing per-word timing.
                "words": [
                    {
                        "start": start,
                        "end": end,
                        "text": text,
                        "probability": probability,
                    }
                ],
            }
        )
    return {
        "language": "zh",
        "text": "\n".join(str(segment["text"]) for segment in segments),
        "segments": segments,
    }


def _load_runtime(model: Path):
    import mlx_speech

    return mlx_speech.asr.load(str(model))._runtime


def _serve(model: Path) -> int:
    from opencc import OpenCC

    runtime = _load_runtime(model)
    convert = OpenCC("s2twp").convert
    _response({"type": "ready", "qwen3_asr_runtime": "mlx-speech-0.5.2"})
    while line := sys.stdin.buffer.readline(_MAX_REQUEST_BYTES + 1):
        if len(line) > _MAX_REQUEST_BYTES or not line.endswith(b"\n"):
            return 2
        try:
            request = json.loads(line)
            if not isinstance(request, dict) or set(request) != {
                "audio",
                "initial_prompt",
                "language",
                "output",
            }:
                raise ValueError("invalid request")
            if request["language"] != "zh" or request["initial_prompt"] is not None:
                raise ValueError("unsupported request")
            audio = _regular(request["audio"])
            output = _regular(request["output"])
            if output.stat().st_size != 0:
                raise ValueError("unsafe output")
            _write_result(output, _result(runtime, convert, audio))
            _response({"type": "result", "ok": True})
        except (KeyError, OSError, TypeError, ValueError, wave.Error):
            _response({"type": "result", "ok": False, "code": "asr_request_invalid"})
        except Exception:
            _response({"type": "result", "ok": False, "code": "asr_failed"})
    return 0


def main() -> int:
    arguments = _arguments()
    model = _regular(arguments.model, directory=True)
    _model_files(model)
    if arguments.probe:
        mlx_version = importlib.metadata.version("mlx")
        speech_version = importlib.metadata.version("mlx-speech")
        opencc_version = importlib.metadata.version("opencc-python-reimplemented")
        _response(
            {
                "qwen3_asr_runtime": (
                    f"mlx-speech-{speech_version}+mlx-{mlx_version}+opencc-{opencc_version}"
                )
            }
        )
        return 0
    if arguments.serve:
        return _serve(model)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
