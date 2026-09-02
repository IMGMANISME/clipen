"""Bounded FFmpeg audio normalization and deterministic PCM chunking."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import math
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
from typing import Callable
import wave

from clipen.media import InventoryRoot, bounded_file_fingerprint
from clipen.media.discovery import DiscoveryError, open_file_beneath
from clipen.media.models import validate_relative_path

from .models import Rational, SourceSpan


_VERSION_ID = re.compile(r"av_[0-9a-f]{32}\Z")
_POLL_SECONDS = 0.05
_MAX_CAPTURE_BYTES = 1024 * 1024
_NORMALIZED_RATE = 16_000
_NORMALIZED_CHANNELS = 1
_NORMALIZED_SAMPLE_WIDTH = 2


class AudioPreparationError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class AudioPreparationInput:
    root: InventoryRoot
    relative_path: str
    version_id: str
    stream_index: int
    size_bytes: int
    mtime_ns: int
    fingerprint: str
    source_span: SourceSpan

    def __post_init__(self) -> None:
        validate_relative_path(self.relative_path)
        if _VERSION_ID.fullmatch(self.version_id) is None:
            raise ValueError("audio preparation version is invalid")
        if self.source_span.version_id != self.version_id:
            raise ValueError("audio source span does not match its asset version")
        if self.source_span.stream_index != self.stream_index:
            raise ValueError("audio source span does not match its stream")
        if self.stream_index < 0 or self.size_bytes < 0 or self.mtime_ns < 0:
            raise ValueError("audio preparation input metadata is invalid")
        if re.fullmatch(r"[0-9a-f]{64}", self.fingerprint) is None:
            raise ValueError("audio preparation fingerprint is invalid")


@dataclass(frozen=True)
class PreparedAudio:
    path: Path
    sample_rate: int
    channels: int
    sample_width: int
    frame_count: int

    @property
    def duration_seconds(self) -> Fraction:
        return Fraction(self.frame_count, self.sample_rate)


@dataclass(frozen=True)
class PreparedAudioChunk:
    path: Path
    source_span: SourceSpan
    start_sample: int
    end_sample: int

    def __post_init__(self) -> None:
        if self.start_sample < 0 or self.end_sample <= self.start_sample:
            raise ValueError("audio chunk sample range is invalid")


class FFmpegAudioAdapter:
    def __init__(
        self,
        executable: str | None = None,
        *,
        timeout_seconds: float = 6 * 60 * 60,
        maximum_output_bytes: int = 16 * 1024**3,
    ) -> None:
        resolved = executable or shutil.which("ffmpeg")
        if not resolved or not os.path.isabs(resolved):
            raise AudioPreparationError("ffmpeg_unavailable")
        resolved = os.path.realpath(resolved)
        if not os.path.isfile(resolved) or not os.access(resolved, os.X_OK):
            raise AudioPreparationError("ffmpeg_unavailable")
        if timeout_seconds <= 0:
            raise ValueError("FFmpeg timeout must be positive")
        if maximum_output_bytes < 1:
            raise ValueError("FFmpeg output limit must be positive")
        self.executable = resolved
        self.timeout_seconds = timeout_seconds
        self.maximum_output_bytes = maximum_output_bytes

    def version(self) -> str:
        stdout, _, returncode = self._run((self.executable, "-version"), ())
        lines = stdout.decode("utf-8", "replace").splitlines()
        if returncode != 0 or not lines or not lines[0].startswith("ffmpeg version "):
            raise AudioPreparationError("ffmpeg_unsupported")
        return lines[0][:240]

    def extract(
        self,
        source: AudioPreparationInput,
        output: Path,
        cancel: threading.Event | None = None,
    ) -> PreparedAudio:
        output_path = self._validate_empty_output(output)
        try:
            descriptor = open_file_beneath(source.root, source.relative_path)
        except DiscoveryError as error:
            raise AudioPreparationError(error.code) from error
        try:
            before = os.fstat(descriptor)
            observed = (
                before.st_size,
                before.st_mtime_ns,
                bounded_file_fingerprint(descriptor, before.st_size),
            )
            if observed != (source.size_bytes, source.mtime_ns, source.fingerprint):
                raise AudioPreparationError("source_changed")
            command = (
                self.executable,
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                f"/dev/fd/{descriptor}",
                "-map",
                f"0:{source.stream_index}",
                "-vn",
                "-ac",
                str(_NORMALIZED_CHANNELS),
                "-ar",
                str(_NORMALIZED_RATE),
                "-c:a",
                "pcm_s16le",
                "-f",
                "wav",
                "-y",
                str(output_path),
            )
            _, _, returncode = self._run(
                command,
                (descriptor,),
                cancel,
                observed_output=output_path,
            )
            if returncode != 0:
                raise AudioPreparationError("audio_decode_failed")
            after = os.fstat(descriptor)
            if (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                source.fingerprint,
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                bounded_file_fingerprint(descriptor, after.st_size),
            ):
                raise AudioPreparationError("source_changed")
            return inspect_normalized_audio(output_path)
        except Exception:
            output_path.unlink(missing_ok=True)
            raise
        finally:
            os.close(descriptor)

    def _run(
        self,
        command: tuple[str, ...],
        pass_fds: tuple[int, ...],
        cancel: threading.Event | None = None,
        *,
        observed_output: Path | None = None,
    ) -> tuple[bytes, bytes, int]:
        deadline = time.monotonic() + self.timeout_seconds
        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            try:
                process = subprocess.Popen(
                    command,
                    cwd="/",
                    env={"LANG": "C", "LC_ALL": "C"},
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    pass_fds=pass_fds,
                    start_new_session=os.name == "posix",
                )
            except OSError as error:
                raise AudioPreparationError("ffmpeg_unavailable") from error
            while process.poll() is None:
                if cancel is not None and cancel.is_set():
                    _terminate(process)
                    raise AudioPreparationError("cancelled")
                if time.monotonic() >= deadline:
                    _terminate(process)
                    raise AudioPreparationError("audio_decode_timeout")
                if (
                    os.fstat(stdout_file.fileno()).st_size > _MAX_CAPTURE_BYTES
                    or os.fstat(stderr_file.fileno()).st_size > _MAX_CAPTURE_BYTES
                ):
                    _terminate(process)
                    raise AudioPreparationError("ffmpeg_output_too_large")
                if observed_output is not None:
                    try:
                        if observed_output.stat().st_size > self.maximum_output_bytes:
                            _terminate(process)
                            raise AudioPreparationError("audio_artifact_too_large")
                    except FileNotFoundError:
                        pass
                time.sleep(_POLL_SECONDS)
            if (
                os.fstat(stdout_file.fileno()).st_size > _MAX_CAPTURE_BYTES
                or os.fstat(stderr_file.fileno()).st_size > _MAX_CAPTURE_BYTES
            ):
                raise AudioPreparationError("ffmpeg_output_too_large")
            if observed_output is not None and observed_output.stat().st_size > self.maximum_output_bytes:
                raise AudioPreparationError("audio_artifact_too_large")
            stdout_file.seek(0)
            stderr_file.seek(0)
            return stdout_file.read(), stderr_file.read(), process.returncode

    @staticmethod
    def _validate_empty_output(output: Path) -> Path:
        candidate = Path(output)
        if candidate.is_symlink():
            raise AudioPreparationError("audio_output_unsafe")
        try:
            info = os.lstat(candidate)
        except OSError as error:
            raise AudioPreparationError("audio_output_missing") from error
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != 0:
            raise AudioPreparationError("audio_output_unsafe")
        return candidate.resolve(strict=True)


class PCMChunker:
    def __init__(self, *, chunk_seconds: int = 5 * 60, overlap_seconds: int = 2) -> None:
        if not 10 <= chunk_seconds <= 30 * 60:
            raise ValueError("audio chunk duration is outside supported bounds")
        if not 0 <= overlap_seconds < chunk_seconds // 2:
            raise ValueError("audio chunk overlap is outside supported bounds")
        self.chunk_seconds = chunk_seconds
        self.overlap_seconds = overlap_seconds

    def split(
        self,
        prepared: PreparedAudio,
        source_span: SourceSpan,
        output_factory: Callable[[], Path],
    ) -> tuple[PreparedAudioChunk, ...]:
        if (
            prepared.sample_rate != _NORMALIZED_RATE
            or prepared.channels != _NORMALIZED_CHANNELS
            or prepared.sample_width != _NORMALIZED_SAMPLE_WIDTH
        ):
            raise AudioPreparationError("normalized_audio_invalid")
        chunk_frames = self.chunk_seconds * prepared.sample_rate
        overlap_frames = self.overlap_seconds * prepared.sample_rate
        step = chunk_frames - overlap_frames
        chunks: list[PreparedAudioChunk] = []
        with wave.open(str(prepared.path), "rb") as source:
            start = 0
            while start < prepared.frame_count:
                end = min(prepared.frame_count, start + chunk_frames)
                source.setpos(start)
                frames = source.readframes(end - start)
                output = output_factory()
                try:
                    _write_pcm_wav(output, frames, end - start)
                except Exception:
                    output.unlink(missing_ok=True)
                    raise
                chunks.append(
                    PreparedAudioChunk(
                        output,
                        project_samples_to_source(source_span, start, end, prepared.sample_rate),
                        start,
                        end,
                    )
                )
                if end == prepared.frame_count:
                    break
                start += step
        return tuple(chunks)


def inspect_normalized_audio(path: Path) -> PreparedAudio:
    try:
        with wave.open(str(path), "rb") as audio:
            prepared = PreparedAudio(
                path,
                audio.getframerate(),
                audio.getnchannels(),
                audio.getsampwidth(),
                audio.getnframes(),
            )
    except (OSError, EOFError, wave.Error) as error:
        raise AudioPreparationError("normalized_audio_invalid") from error
    if (
        prepared.sample_rate != _NORMALIZED_RATE
        or prepared.channels != _NORMALIZED_CHANNELS
        or prepared.sample_width != _NORMALIZED_SAMPLE_WIDTH
        or prepared.frame_count < 1
    ):
        raise AudioPreparationError("normalized_audio_invalid")
    return prepared


def _write_pcm_wav(path: Path, frames: bytes, frame_count: int) -> None:
    expected = frame_count * _NORMALIZED_CHANNELS * _NORMALIZED_SAMPLE_WIDTH
    if frame_count < 1 or len(frames) != expected:
        raise AudioPreparationError("normalized_audio_invalid")
    with wave.open(str(path), "wb") as output:
        output.setnchannels(_NORMALIZED_CHANNELS)
        output.setsampwidth(_NORMALIZED_SAMPLE_WIDTH)
        output.setframerate(_NORMALIZED_RATE)
        output.writeframes(frames)


def project_samples_to_source(
    source: SourceSpan,
    start_sample: int,
    end_sample: int,
    sample_rate: int,
) -> SourceSpan:
    units_per_second = Fraction(source.time_base.denominator, source.time_base.numerator)
    start_offset = Fraction(start_sample, sample_rate) * units_per_second
    end_offset = Fraction(end_sample, sample_rate) * units_per_second
    start_pts = int(source.start_pts) + math.floor(start_offset)
    end_pts = int(source.start_pts) + math.ceil(end_offset)
    end_pts = min(end_pts, int(source.end_pts))
    if end_pts <= start_pts:
        raise AudioPreparationError("audio_time_mapping_invalid")
    return SourceSpan(
        source.version_id,
        source.stream_index,
        str(start_pts),
        str(end_pts),
        Rational(source.time_base.numerator, source.time_base.denominator),
    )


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
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            process.kill()
        process.wait(timeout=2)
