"""Bounded local transcription adapter with strict timestamp validation."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import tempfile
import threading
import time

from .audio import PreparedAudio


_MODEL_ID = re.compile(r"[a-z][a-z0-9_.-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_LANGUAGE = re.compile(r"[a-z]{2,3}(?:-[A-Z]{2})?\Z")
_MAX_CAPTURE_BYTES = 1024 * 1024
_MAX_RESULT_BYTES = 32 * 1024 * 1024
_MAX_SEGMENTS = 100_000
_MAX_WORDS = 1_000_000
_POLL_SECONDS = 0.05


class TranscriptionError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class LocalModelSnapshot:
    model_id: str
    revision: str
    path: Path
    manifest_sha256: str

    def __post_init__(self) -> None:
        if _MODEL_ID.fullmatch(self.model_id) is None:
            raise ValueError("ASR model ID is invalid")
        if not self.revision or len(self.revision) > 128 or any(
            ord(character) < 32 for character in self.revision
        ):
            raise ValueError("ASR model revision is invalid")
        if _SHA256.fullmatch(self.manifest_sha256) is None:
            raise ValueError("ASR model manifest digest is invalid")


@dataclass(frozen=True)
class TranscriptWord:
    start_sample: int
    end_sample: int
    text: str
    probability: str | None

    def __post_init__(self) -> None:
        if self.start_sample < 0 or self.end_sample <= self.start_sample:
            raise ValueError("transcript word range is invalid")
        _validate_text(self.text, 1024, "transcript word")
        if self.probability is not None:
            probability = _decimal(self.probability, minimum=Decimal(0), maximum=Decimal(1))
            if str(probability) != self.probability:
                raise ValueError("word probability must be canonical")


@dataclass(frozen=True)
class TranscriptSegment:
    start_sample: int
    end_sample: int
    text: str
    no_speech_probability: str | None
    words: tuple[TranscriptWord, ...]

    def __post_init__(self) -> None:
        if self.start_sample < 0 or self.end_sample <= self.start_sample:
            raise ValueError("transcript segment range is invalid")
        _validate_text(self.text, 64 * 1024, "transcript segment")
        if self.no_speech_probability is not None:
            probability = _decimal(
                self.no_speech_probability,
                minimum=Decimal(0),
                maximum=Decimal(1),
            )
            if str(probability) != self.no_speech_probability:
                raise ValueError("no-speech probability must be canonical")
        previous_end = self.start_sample
        for word in self.words:
            if (
                word.start_sample < self.start_sample
                or word.end_sample > self.end_sample
                or word.start_sample < previous_end
            ):
                raise ValueError("transcript words must be ordered inside their segment")
            previous_end = word.end_sample


@dataclass(frozen=True)
class TranscriptionResult:
    language: str
    text: str
    segments: tuple[TranscriptSegment, ...]

    def __post_init__(self) -> None:
        if _LANGUAGE.fullmatch(self.language) is None:
            raise ValueError("transcript language is invalid")
        _validate_text(self.text, 8 * 1024 * 1024, "transcript")
        previous_start = -1
        for segment in self.segments:
            if segment.start_sample < previous_start:
                raise ValueError("transcript segments must be ordered")
            previous_start = segment.start_sample
        if not self.segments and self.text.strip():
            raise ValueError("transcript text requires timestamped segments")

    @property
    def no_speech_detected(self) -> bool:
        return not self.segments and not self.text.strip()


class MLXWhisperAdapter:
    def __init__(
        self,
        python_executable: Path,
        model: LocalModelSnapshot,
        *,
        timeout_seconds: float = 6 * 60 * 60,
    ) -> None:
        executable = Path(python_executable)
        executable_path = Path(os.path.abspath(executable))
        try:
            executable_target = executable_path.resolve(strict=True)
        except OSError as error:
            raise TranscriptionError("asr_runtime_unavailable") from error
        if (
            not executable_path.is_absolute()
            or not executable_target.is_file()
            or not os.access(executable_target, os.X_OK)
        ):
            raise TranscriptionError("asr_runtime_unavailable")
        if timeout_seconds <= 0:
            raise ValueError("ASR timeout must be positive")
        # A venv's Python is commonly a symlink. Executing the resolved base
        # interpreter would silently discard the venv's site-packages.
        self.python_executable = executable_path
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.worker = Path(__file__).with_name("mlx_worker.py").resolve(strict=True)
        self._model_lock = threading.Lock()
        self._verified_model_path: Path | None = None
        self._verified_model_signature: str | None = None

    def probe(self) -> str:
        model_path = self._verify_model()
        stdout, _, returncode = self._run(
            (
                str(self.python_executable),
                str(self.worker),
                "--probe",
                "--model",
                str(model_path),
            ),
            None,
        )
        if returncode != 0:
            raise TranscriptionError("asr_runtime_unsupported")
        try:
            payload = json.loads(stdout.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise TranscriptionError("asr_runtime_unsupported") from error
        version = payload.get("mlx_whisper") if isinstance(payload, dict) else None
        if not isinstance(version, str) or not 1 <= len(version) <= 80:
            raise TranscriptionError("asr_runtime_unsupported")
        return version

    def transcribe(
        self,
        audio: PreparedAudio,
        output: Path,
        *,
        language: str = "zh",
        cancel: threading.Event | None = None,
    ) -> TranscriptionResult:
        if _LANGUAGE.fullmatch(language) is None:
            raise ValueError("requested ASR language is invalid")
        audio_path = _verify_regular_file(audio.path, "normalized_audio_missing")
        output_path = _verify_empty_output(output)
        model_path = self._verify_model()
        command = (
            str(self.python_executable),
            str(self.worker),
            "--model",
            str(model_path),
            "--audio",
            str(audio_path),
            "--output",
            str(output_path),
            "--language",
            language,
        )
        try:
            _, _, returncode = self._run(command, cancel, observed_output=output_path)
            if returncode != 0:
                raise TranscriptionError("asr_failed")
            return _parse_result(output_path, audio.frame_count, audio.sample_rate)
        except Exception:
            output_path.unlink(missing_ok=True)
            raise

    def _run(
        self,
        command: tuple[str, ...],
        cancel: threading.Event | None,
        *,
        observed_output: Path | None = None,
    ) -> tuple[bytes, bytes, int]:
        deadline = time.monotonic() + self.timeout_seconds
        environment = {
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PYTHONNOUSERSITE": "1",
            "HF_HUB_OFFLINE": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
        }
        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            try:
                process = subprocess.Popen(
                    command,
                    cwd="/",
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    start_new_session=os.name == "posix",
                )
            except OSError as error:
                raise TranscriptionError("asr_runtime_unavailable") from error
            while process.poll() is None:
                if cancel is not None and cancel.is_set():
                    _terminate(process)
                    raise TranscriptionError("cancelled")
                if time.monotonic() >= deadline:
                    _terminate(process)
                    raise TranscriptionError("asr_timeout")
                if (
                    os.fstat(stdout_file.fileno()).st_size > _MAX_CAPTURE_BYTES
                    or os.fstat(stderr_file.fileno()).st_size > _MAX_CAPTURE_BYTES
                ):
                    _terminate(process)
                    raise TranscriptionError("asr_output_too_large")
                if observed_output is not None:
                    try:
                        if observed_output.stat().st_size > _MAX_RESULT_BYTES:
                            _terminate(process)
                            raise TranscriptionError("asr_result_too_large")
                    except FileNotFoundError:
                        pass
                time.sleep(_POLL_SECONDS)
            if (
                os.fstat(stdout_file.fileno()).st_size > _MAX_CAPTURE_BYTES
                or os.fstat(stderr_file.fileno()).st_size > _MAX_CAPTURE_BYTES
            ):
                raise TranscriptionError("asr_output_too_large")
            if observed_output is not None and observed_output.stat().st_size > _MAX_RESULT_BYTES:
                raise TranscriptionError("asr_result_too_large")
            stdout_file.seek(0)
            stderr_file.seek(0)
            return stdout_file.read(), stderr_file.read(), process.returncode

    def _verify_model(self) -> Path:
        with self._model_lock:
            if self._verified_model_path is None:
                path = _verify_model(self.model)
                self._verified_model_path = path
                self._verified_model_signature = _model_directory_signature(path)
                return path
            observed = _model_directory_signature(self._verified_model_path)
            if observed != self._verified_model_signature:
                raise TranscriptionError("asr_model_changed")
            return self._verified_model_path


def hash_model_directory(path: Path, *, maximum_bytes: int = 8 * 1024**3) -> str:
    root = Path(path)
    if root.is_symlink() or not root.is_dir():
        raise TranscriptionError("asr_model_unavailable")
    resolved = root.resolve(strict=True)
    digest = hashlib.sha256()
    total = 0
    files = 0
    for candidate in sorted(resolved.rglob("*")):
        if candidate.is_symlink():
            raise TranscriptionError("asr_model_unsafe")
        if candidate.is_dir():
            continue
        info = os.lstat(candidate)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise TranscriptionError("asr_model_unsafe")
        relative = candidate.relative_to(resolved).as_posix()
        if len(relative) > 1024 or files >= 10_000:
            raise TranscriptionError("asr_model_unsafe")
        files += 1
        total += info.st_size
        if total > maximum_bytes:
            raise TranscriptionError("asr_model_too_large")
        file_digest = hashlib.sha256()
        with candidate.open("rb") as source:
            while block := source.read(1024 * 1024):
                file_digest.update(block)
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(info.st_size).encode("ascii"))
        digest.update(b"\0")
        digest.update(file_digest.digest())
    if files < 1:
        raise TranscriptionError("asr_model_unavailable")
    return digest.hexdigest()


def _model_directory_signature(path: Path) -> str:
    root = Path(path)
    if root.is_symlink() or not root.is_dir():
        raise TranscriptionError("asr_model_unavailable")
    resolved = root.resolve(strict=True)
    digest = hashlib.sha256()
    files = 0
    for candidate in sorted(resolved.rglob("*")):
        if candidate.is_symlink():
            raise TranscriptionError("asr_model_unsafe")
        if candidate.is_dir():
            continue
        info = os.lstat(candidate)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or files >= 10_000:
            raise TranscriptionError("asr_model_unsafe")
        files += 1
        relative = candidate.relative_to(resolved).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(
            f"{info.st_dev}:{info.st_ino}:{info.st_size}:{info.st_mtime_ns}".encode(
                "ascii"
            )
        )
        digest.update(b"\0")
    if files < 1:
        raise TranscriptionError("asr_model_unavailable")
    return digest.hexdigest()


def _verify_model(model: LocalModelSnapshot) -> Path:
    path = Path(model.path)
    if not path.is_absolute():
        raise TranscriptionError("asr_model_unavailable")
    observed = hash_model_directory(path)
    if observed != model.manifest_sha256:
        raise TranscriptionError("asr_model_changed")
    return path.resolve(strict=True)


def _parse_result(path: Path, frame_count: int, sample_rate: int) -> TranscriptionResult:
    try:
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > _MAX_RESULT_BYTES:
            raise TranscriptionError("asr_result_invalid")
        value = json.loads(path.read_text(encoding="utf-8"))
    except TranscriptionError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise TranscriptionError("asr_result_invalid") from error
    if not isinstance(value, dict) or set(value) != {"language", "text", "segments"}:
        raise TranscriptionError("asr_result_invalid")
    language = value["language"]
    text = value["text"]
    raw_segments = value["segments"]
    if (
        not isinstance(language, str)
        or _LANGUAGE.fullmatch(language) is None
        or not isinstance(text, str)
        or not isinstance(raw_segments, list)
        or len(raw_segments) > _MAX_SEGMENTS
    ):
        raise TranscriptionError("asr_result_invalid")
    segments: list[TranscriptSegment] = []
    word_count = 0
    try:
        for raw in raw_segments:
            if not isinstance(raw, dict) or set(raw) != {
                "start",
                "end",
                "text",
                "no_speech_probability",
                "words",
            }:
                raise ValueError("segment shape")
            start = _seconds_to_sample(raw["start"], sample_rate, frame_count, start=True)
            end = _seconds_to_sample(raw["end"], sample_rate, frame_count, start=False)
            raw_words = raw["words"]
            if not isinstance(raw_words, list):
                raise ValueError("word shape")
            word_count += len(raw_words)
            if word_count > _MAX_WORDS:
                raise ValueError("too many words")
            words = tuple(
                TranscriptWord(
                    _seconds_to_sample(word["start"], sample_rate, frame_count, start=True),
                    _seconds_to_sample(word["end"], sample_rate, frame_count, start=False),
                    word["text"],
                    _optional_probability(word["probability"]),
                )
                for word in raw_words
                if isinstance(word, dict)
                and set(word) == {"start", "end", "text", "probability"}
            )
            if len(words) != len(raw_words):
                raise ValueError("word shape")
            segments.append(
                TranscriptSegment(
                    start,
                    end,
                    raw["text"],
                    _optional_probability(raw["no_speech_probability"]),
                    words,
                )
            )
        return TranscriptionResult(language, text, tuple(segments))
    except (KeyError, TypeError, ValueError, InvalidOperation) as error:
        raise TranscriptionError("asr_result_invalid") from error


def _seconds_to_sample(value: object, rate: int, frame_count: int, *, start: bool) -> int:
    seconds = _decimal(value, minimum=Decimal(0), maximum=Decimal("1000000000"))
    rounding = ROUND_FLOOR if start else ROUND_CEILING
    sample = int((seconds * rate).to_integral_value(rounding=rounding))
    if start:
        if sample >= frame_count:
            raise ValueError("timestamp starts outside audio")
    elif sample < 1 or sample > frame_count:
        raise ValueError("timestamp ends outside audio")
    return sample


def _optional_probability(value: object) -> str | None:
    if value is None:
        return None
    return str(_decimal(value, minimum=Decimal(0), maximum=Decimal(1)))


def _decimal(value: object, *, minimum: Decimal, maximum: Decimal) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("boolean is not a decimal")
    try:
        result = Decimal(str(value)).normalize()
    except (InvalidOperation, ValueError) as error:
        raise ValueError("invalid decimal") from error
    if not result.is_finite() or not minimum <= result <= maximum:
        raise ValueError("decimal outside bounds")
    return result


def _validate_text(value: object, maximum_bytes: int, label: str) -> None:
    if (
        not isinstance(value, str)
        or "\x00" in value
        or len(value.encode("utf-8")) > maximum_bytes
    ):
        raise ValueError(f"{label} text is invalid")


def _verify_regular_file(path: Path, code: str) -> Path:
    candidate = Path(path)
    if candidate.is_symlink() or not candidate.is_file():
        raise TranscriptionError(code)
    info = os.lstat(candidate)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise TranscriptionError(code)
    return candidate.resolve(strict=True)


def _verify_empty_output(path: Path) -> Path:
    candidate = _verify_regular_file(path, "asr_output_unsafe")
    if candidate.stat().st_size != 0:
        raise TranscriptionError("asr_output_unsafe")
    return candidate


def _terminate(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            process.wait(timeout=2)
            return
    else:
        process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            process.kill()
        process.wait(timeout=2)
