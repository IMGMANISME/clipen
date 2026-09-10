"""Bounded local transcription adapter with strict timestamp validation."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import signal
import stat
import subprocess
import tempfile
import threading
import time
import unicodedata

from .audio import PreparedAudio
from .resources import local_analysis_slot


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
    expected_files: tuple[tuple[str, int], ...] = ()

    def __post_init__(self) -> None:
        if _MODEL_ID.fullmatch(self.model_id) is None:
            raise ValueError("ASR model ID is invalid")
        if not self.revision or len(self.revision) > 128 or any(
            ord(character) < 32 for character in self.revision
        ):
            raise ValueError("ASR model revision is invalid")
        if _SHA256.fullmatch(self.manifest_sha256) is None:
            raise ValueError("ASR model manifest digest is invalid")
        if any(
            not filename
            or Path(filename).name != filename
            or size < 1
            for filename, size in self.expected_files
        ):
            raise ValueError("ASR model file contract is invalid")


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

    @property
    def word_confidence(self) -> str | None:
        probabilities = [
            Decimal(word.probability)
            for word in self.words
            if word.probability is not None
        ]
        if not probabilities:
            return None
        return _canonical_probability(sum(probabilities) / len(probabilities))


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

    @property
    def needs_review(self) -> bool:
        if self.no_speech_detected:
            return False
        probabilities = [
            Decimal(word.probability)
            for segment in self.segments
            for word in segment.words
            if word.probability is not None
        ]
        if not probabilities:
            # Keep compatibility with older stored transcripts. Current Qwen3
            # results always carry a phrase-level confidence value.
            return any(segment.text.strip() == "[音訊不清楚]" for segment in self.segments)
        mean = sum(probabilities) / len(probabilities)
        low_ratio = sum(
            probability < Decimal("0.5") for probability in probabilities
        ) / len(probabilities)
        return mean < Decimal("0.65") or low_ratio > 0.3

    def to_storage(self) -> dict[str, object]:
        return {
            "language": self.language,
            "text": self.text,
            "segments": [
                {
                    "start_sample": segment.start_sample,
                    "end_sample": segment.end_sample,
                    "text": segment.text,
                    "no_speech_probability": segment.no_speech_probability,
                    "words": [
                        {
                            "start_sample": word.start_sample,
                            "end_sample": word.end_sample,
                            "text": word.text,
                            "probability": word.probability,
                        }
                        for word in segment.words
                    ],
                }
                for segment in self.segments
            ],
        }

    @classmethod
    def from_storage(cls, value: object) -> "TranscriptionResult":
        if not isinstance(value, dict) or set(value) != {"language", "text", "segments"}:
            raise ValueError("stored transcript shape is invalid")
        raw_segments = value["segments"]
        if not isinstance(raw_segments, list) or len(raw_segments) > _MAX_SEGMENTS:
            raise ValueError("stored transcript segments are invalid")
        segments: list[TranscriptSegment] = []
        word_count = 0
        for raw_segment in raw_segments:
            if not isinstance(raw_segment, dict) or set(raw_segment) != {
                "start_sample",
                "end_sample",
                "text",
                "no_speech_probability",
                "words",
            }:
                raise ValueError("stored transcript segment is invalid")
            raw_words = raw_segment["words"]
            if not isinstance(raw_words, list):
                raise ValueError("stored transcript words are invalid")
            word_count += len(raw_words)
            if word_count > _MAX_WORDS:
                raise ValueError("stored transcript has too many words")
            words = []
            for raw_word in raw_words:
                if not isinstance(raw_word, dict) or set(raw_word) != {
                    "start_sample",
                    "end_sample",
                    "text",
                    "probability",
                }:
                    raise ValueError("stored transcript word is invalid")
                words.append(
                    TranscriptWord(
                        raw_word["start_sample"],
                        raw_word["end_sample"],
                        raw_word["text"],
                        raw_word["probability"],
                    )
                )
            segments.append(
                TranscriptSegment(
                    raw_segment["start_sample"],
                    raw_segment["end_sample"],
                    raw_segment["text"],
                    raw_segment["no_speech_probability"],
                    tuple(words),
                )
            )
        return cls(value["language"], value["text"], tuple(segments))


class _PersistentASRWorkerAdapter:
    def __init__(
        self,
        python_executable: Path,
        model: LocalModelSnapshot,
        *,
        timeout_seconds: float = 6 * 60 * 60,
        worker: Path,
        readiness_key: str,
        worker_arguments: tuple[str, ...] = (),
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
        self.worker = Path(worker).resolve(strict=True)
        if not readiness_key or not readiness_key.isidentifier():
            raise ValueError("ASR readiness key is invalid")
        if any(not isinstance(argument, str) or "\x00" in argument for argument in worker_arguments):
            raise ValueError("ASR worker arguments are invalid")
        self._readiness_key = readiness_key
        self._worker_arguments = worker_arguments
        self._model_lock = threading.Lock()
        self._verified_model_path: Path | None = None
        self._verified_model_signature: str | None = None
        self._worker_lock = threading.Lock()
        self._process: subprocess.Popen[bytes] | None = None
        self._responses: queue.Queue[bytes | None] = queue.Queue()
        self._stderr_file = None

    def probe(self) -> str:
        model_path = self._verify_model()
        stdout, _, returncode = self._run(self._worker_command("--probe", model_path), None)
        if returncode != 0:
            raise TranscriptionError("asr_runtime_unsupported")
        try:
            payload = json.loads(stdout.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise TranscriptionError("asr_runtime_unsupported") from error
        version = payload.get(self._readiness_key) if isinstance(payload, dict) else None
        if not isinstance(version, str) or not 1 <= len(version) <= 80:
            raise TranscriptionError("asr_runtime_unsupported")
        return version

    def transcribe(
        self,
        audio: PreparedAudio,
        output: Path,
        *,
        language: str = "zh",
        initial_prompt: str | None = None,
        cancel: threading.Event | None = None,
    ) -> TranscriptionResult:
        if _LANGUAGE.fullmatch(language) is None:
            raise ValueError("requested ASR language is invalid")
        if initial_prompt is not None and (
            not isinstance(initial_prompt, str)
            or not initial_prompt.strip()
            or len(initial_prompt.encode("utf-8")) > 2_200
        ):
            raise ValueError("requested ASR prompt is invalid")
        audio_path = _verify_regular_file(audio.path, "normalized_audio_missing")
        output_path = _verify_empty_output(output)
        model_path = self._verify_model()
        try:
            request = json.dumps(
                {
                    "audio": str(audio_path),
                    "initial_prompt": initial_prompt,
                    "language": language,
                    "output": str(output_path),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8") + b"\n"
            with self._worker_lock, local_analysis_slot(
                cancel, lambda: TranscriptionError("cancelled")
            ):
                process = self._ensure_server(model_path, cancel)
                try:
                    assert process.stdin is not None
                    process.stdin.write(request)
                    process.stdin.flush()
                except (BrokenPipeError, OSError) as error:
                    self._stop_server()
                    raise TranscriptionError("asr_runtime_unavailable") from error
                response = self._wait_for_response(process, cancel, output_path)
                if response.get("type") != "result" or response.get("ok") is not True:
                    code = response.get("code")
                    self._stop_server()
                    raise TranscriptionError(
                        code if code in {"asr_failed", "asr_request_invalid"} else "asr_failed"
                    )
            return _parse_result(output_path, audio.frame_count, audio.sample_rate)
        except Exception:
            output_path.unlink(missing_ok=True)
            raise

    def close(self) -> None:
        with self._worker_lock:
            self._stop_server()

    def _ensure_server(
        self, model_path: Path, cancel: threading.Event | None
    ) -> subprocess.Popen[bytes]:
        if self._process is not None and self._process.poll() is None:
            return self._process
        self._stop_server()
        environment = self._environment()
        responses: queue.Queue[bytes | None] = queue.Queue()
        self._responses = responses
        self._stderr_file = tempfile.TemporaryFile()
        try:
            process = subprocess.Popen(
                self._worker_command("--serve", model_path),
                cwd="/",
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self._stderr_file,
                start_new_session=os.name == "posix",
            )
        except OSError as error:
            self._close_stderr()
            raise TranscriptionError("asr_runtime_unavailable") from error
        self._process = process
        threading.Thread(
            target=self._read_responses,
            args=(process, responses),
            name="clipen-asr-responses",
            daemon=True,
        ).start()
        ready = self._wait_for_response(process, cancel)
        version = ready.get(self._readiness_key)
        if (
            ready.get("type") != "ready"
            or not isinstance(version, str)
            or not 1 <= len(version) <= 80
        ):
            self._stop_server()
            raise TranscriptionError("asr_runtime_unsupported")
        return process

    def _worker_command(self, mode: str, model_path: Path) -> tuple[str, ...]:
        return (
            str(self.python_executable),
            str(self.worker),
            mode,
            "--model",
            str(model_path),
            *self._worker_arguments,
        )

    def _read_responses(
        self,
        process: subprocess.Popen[bytes],
        responses: queue.Queue[bytes | None],
    ) -> None:
        assert process.stdout is not None
        try:
            while line := process.stdout.readline(_MAX_CAPTURE_BYTES + 1):
                if len(line) > _MAX_CAPTURE_BYTES or not line.endswith(b"\n"):
                    responses.put(None)
                    return
                responses.put(line)
        finally:
            responses.put(None)

    def _wait_for_response(
        self,
        process: subprocess.Popen[bytes],
        cancel: threading.Event | None,
        observed_output: Path | None = None,
    ) -> dict[str, object]:
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            if cancel is not None and cancel.is_set():
                self._stop_server()
                raise TranscriptionError("cancelled")
            if time.monotonic() >= deadline:
                self._stop_server()
                raise TranscriptionError("asr_timeout")
            if observed_output is not None:
                try:
                    if observed_output.stat().st_size > _MAX_RESULT_BYTES:
                        self._stop_server()
                        raise TranscriptionError("asr_result_too_large")
                except FileNotFoundError:
                    pass
            if self._stderr_file is not None:
                try:
                    if os.fstat(self._stderr_file.fileno()).st_size > _MAX_CAPTURE_BYTES:
                        self._stop_server()
                        raise TranscriptionError("asr_output_too_large")
                except ValueError:
                    pass
            try:
                line = self._responses.get(timeout=_POLL_SECONDS)
            except queue.Empty:
                if process.poll() is not None:
                    self._stop_server()
                    raise TranscriptionError("asr_runtime_unavailable")
                continue
            if line is None:
                self._stop_server()
                raise TranscriptionError("asr_runtime_unavailable")
            try:
                value = json.loads(line)
            except (UnicodeError, json.JSONDecodeError) as error:
                self._stop_server()
                raise TranscriptionError("asr_runtime_unsupported") from error
            if not isinstance(value, dict):
                self._stop_server()
                raise TranscriptionError("asr_runtime_unsupported")
            return value

    def _stop_server(self) -> None:
        process = self._process
        self._process = None
        if process is not None:
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            if process.poll() is None:
                _terminate(process)
            if process.stdout is not None:
                process.stdout.close()
        self._close_stderr()

    def _close_stderr(self) -> None:
        if self._stderr_file is not None:
            self._stderr_file.close()
            self._stderr_file = None

    @staticmethod
    def _environment() -> dict[str, str]:
        return {
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PYTHONNOUSERSITE": "1",
            "HF_HUB_OFFLINE": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
        }

    def _run(
        self,
        command: tuple[str, ...],
        cancel: threading.Event | None,
        *,
        observed_output: Path | None = None,
    ) -> tuple[bytes, bytes, int]:
        environment = self._environment()
        with local_analysis_slot(cancel, lambda: TranscriptionError("cancelled")), tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            deadline = time.monotonic() + self.timeout_seconds
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


class Qwen3ASRAdapter(_PersistentASRWorkerAdapter):
    """Managed Qwen3-ASR worker with token-confidence gating."""

    def __init__(
        self,
        python_executable: Path,
        model: LocalModelSnapshot,
        *,
        timeout_seconds: float = 6 * 60 * 60,
    ) -> None:
        super().__init__(
            python_executable,
            model,
            timeout_seconds=timeout_seconds,
            worker=Path(__file__).with_name("qwen_worker.py"),
            readiness_key="qwen3_asr_runtime",
        )


def _verify_executable(path: Path) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute() or candidate.is_symlink():
        raise TranscriptionError("asr_runtime_unavailable")
    try:
        info = os.lstat(candidate)
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise TranscriptionError("asr_runtime_unavailable") from error
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or not os.access(resolved, os.X_OK)
    ):
        raise TranscriptionError("asr_runtime_unavailable")
    return resolved


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
    if model.expected_files:
        return _verify_expected_model_files(path, model.expected_files)
    observed = hash_model_directory(path)
    if observed != model.manifest_sha256:
        raise TranscriptionError("asr_model_changed")
    return path.resolve(strict=True)


def _verify_expected_model_files(
    path: Path, expected_files: tuple[tuple[str, int], ...]
) -> Path:
    if path.is_symlink() or not path.is_dir():
        raise TranscriptionError("asr_model_unavailable")
    resolved = path.resolve(strict=True)
    expected = dict(expected_files)
    observed: set[str] = set()
    for candidate in resolved.rglob("*"):
        if candidate.is_symlink():
            raise TranscriptionError("asr_model_unsafe")
        if candidate.is_dir():
            continue
        info = os.lstat(candidate)
        relative = candidate.relative_to(resolved).as_posix()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or relative not in expected
            or info.st_size != expected[relative]
        ):
            raise TranscriptionError("asr_model_changed")
        observed.add(relative)
    if observed != set(expected):
        raise TranscriptionError("asr_model_changed")
    return resolved


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
            words = []
            probabilities = []
            previous_word_end = start
            for word in raw_words:
                if not isinstance(word, dict) or set(word) != {
                    "start",
                    "end",
                    "text",
                    "probability",
                }:
                    raise ValueError("word shape")
                probability = _optional_probability(word["probability"])
                if probability is not None:
                    probabilities.append(Decimal(probability))
                word_start = max(
                    start,
                    previous_word_end,
                    _word_sample(word["start"], sample_rate, frame_count, start=True),
                )
                word_end = min(
                    end,
                    _word_sample(word["end"], sample_rate, frame_count, start=False),
                )
                if word_end <= word_start:
                    continue
                words.append(
                    TranscriptWord(
                        word_start,
                        word_end,
                        word["text"],
                        probability,
                    )
                )
                previous_word_end = word_end
            no_speech_probability = _optional_probability(
                raw["no_speech_probability"]
            )
            if _low_quality_segment(
                raw["text"], no_speech_probability, probabilities
            ):
                continue
            segments.append(
                TranscriptSegment(
                    start,
                    end,
                    raw["text"],
                    no_speech_probability,
                    tuple(words),
                )
            )
        segments = _drop_repetition_loops(segments)
        accepted_text = "\n".join(
            segment.text.strip() for segment in segments if segment.text.strip()
        )
        return TranscriptionResult(language, accepted_text, tuple(segments))
    except (KeyError, TypeError, ValueError, InvalidOperation) as error:
        raise TranscriptionError("asr_result_invalid") from error


def _seconds_to_sample(value: object, rate: int, frame_count: int, *, start: bool) -> int:
    seconds = _decimal(value, minimum=Decimal(0), maximum=Decimal("1000000000"))
    rounding = ROUND_FLOOR if start else ROUND_CEILING
    sample = int((seconds * rate).to_integral_value(rounding=rounding))
    if start:
        if sample >= frame_count:
            raise ValueError("timestamp starts outside audio")
    elif sample < 1 or sample > frame_count + rate:
        raise ValueError("timestamp ends outside audio")
    return min(sample, frame_count)


def _word_sample(value: object, rate: int, frame_count: int, *, start: bool) -> int:
    seconds = _decimal(value, minimum=Decimal(0), maximum=Decimal("1000000000"))
    rounding = ROUND_FLOOR if start else ROUND_CEILING
    sample = int((seconds * rate).to_integral_value(rounding=rounding))
    return min(sample, frame_count)


def _low_quality_segment(
    text: object,
    no_speech_probability: str | None,
    word_probabilities: list[Decimal],
) -> bool:
    if not isinstance(text, str):
        raise ValueError("segment text")
    compact = "".join(character for character in text if not character.isspace())
    if not compact:
        return True
    if not any(unicodedata.category(character)[0] in {"L", "N"} for character in compact):
        return True
    if (
        no_speech_probability is not None
        and Decimal(no_speech_probability) >= Decimal("0.6")
    ):
        return True
    if word_probabilities:
        mean = sum(word_probabilities) / len(word_probabilities)
        low_ratio = sum(
            probability < Decimal("0.5") for probability in word_probabilities
        ) / len(word_probabilities)
        if mean < Decimal("0.2") or (
            mean < Decimal("0.35") and low_ratio >= 0.75
        ):
            return True
    if len(compact) >= 12 and len(set(compact)) / len(compact) < 0.15:
        return True
    return False


def _drop_repetition_loops(
    segments: list[TranscriptSegment],
) -> list[TranscriptSegment]:
    """Keep natural emphasis while capping decoder loops across windows."""

    accepted: list[TranscriptSegment] = []
    totals: dict[str, int] = {}
    previous = ""
    consecutive = 0
    for segment in segments:
        normalized = "".join(
            character.casefold()
            for character in segment.text
            if unicodedata.category(character)[0] in {"L", "N"}
        )
        if not normalized:
            continue
        consecutive = consecutive + 1 if normalized == previous else 1
        previous = normalized
        totals[normalized] = totals.get(normalized, 0) + 1
        if consecutive > 2 or totals[normalized] > 3:
            continue
        accepted.append(segment)
    return accepted


def _optional_probability(value: object) -> str | None:
    if value is None:
        return None
    return str(_decimal(value, minimum=Decimal(0), maximum=Decimal(1)))


def _canonical_probability(value: Decimal) -> str:
    rendered = format(value, "f").rstrip("0").rstrip(".")
    return rendered or "0"


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
