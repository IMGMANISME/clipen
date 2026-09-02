"""Bounded ffprobe adapter operating on already-open regular file descriptors."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
from typing import Mapping, Any

from .discovery import DiscoveredAsset, DiscoveryError, open_file_beneath
from .models import AssetKind, InventoryRoot, MediaStream


_MAX_CAPTURE_BYTES = 4 * 1024 * 1024
_MAX_STREAMS = 64
_POLL_SECONDS = 0.05
_CODEC = re.compile(r"[A-Za-z0-9_.-]{1,80}\Z")


class MediaProbeError(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        fingerprint: str | None = None,
        size_bytes: int | None = None,
        mtime_ns: int | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.fingerprint = fingerprint
        self.size_bytes = size_bytes
        self.mtime_ns = mtime_ns

    @property
    def has_version(self) -> bool:
        return all(
            value is not None
            for value in (self.fingerprint, self.size_bytes, self.mtime_ns)
        )


@dataclass(frozen=True)
class MediaProbeResult:
    kind: AssetKind
    fingerprint: str
    size_bytes: int
    mtime_ns: int
    summary: Mapping[str, object]
    streams: tuple[MediaStream, ...]


class FFprobeAdapter:
    def __init__(
        self,
        executable: str | None = None,
        *,
        timeout_seconds: float = 30,
    ) -> None:
        resolved = executable or shutil.which("ffprobe")
        if not resolved or not os.path.isabs(resolved):
            raise MediaProbeError("probe_unavailable")
        resolved = os.path.realpath(resolved)
        if not os.path.isfile(resolved) or not os.access(resolved, os.X_OK):
            raise MediaProbeError("probe_unavailable")
        if timeout_seconds <= 0:
            raise ValueError("ffprobe timeout must be positive")
        self.executable = resolved
        self.timeout_seconds = timeout_seconds

    def version(self) -> str:
        stdout, _, returncode = self._run((self.executable, "-version"), ())
        if returncode != 0:
            raise MediaProbeError("probe_unavailable")
        first = stdout.decode("utf-8", "replace").splitlines()
        if not first or not first[0].startswith("ffprobe version "):
            raise MediaProbeError("probe_unsupported")
        return first[0][:240]

    def probe(
        self,
        root: InventoryRoot,
        asset: DiscoveredAsset,
        cancel: threading.Event | None = None,
    ) -> MediaProbeResult:
        if not asset.should_probe:
            raise ValueError("only recognized regular media can be probed")
        try:
            descriptor = open_file_beneath(root, asset.relative_path)
        except DiscoveryError as error:
            raise MediaProbeError(error.code) from error
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise MediaProbeError("entry_not_regular")
            if before.st_size != asset.size_bytes or before.st_mtime_ns != asset.mtime_ns:
                raise MediaProbeError("changed")
            fingerprint = bounded_file_fingerprint(descriptor, before.st_size)
            command = (
                self.executable,
                "-v",
                "error",
                "-show_format",
                "-show_streams",
                "-of",
                "json",
                f"/dev/fd/{descriptor}",
            )
            try:
                stdout, _, returncode = self._run(command, (descriptor,), cancel)
            except MediaProbeError as error:
                raise MediaProbeError(
                    error.code,
                    fingerprint=fingerprint,
                    size_bytes=before.st_size,
                    mtime_ns=before.st_mtime_ns,
                ) from error
            after = os.fstat(descriptor)
            if (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ):
                raise MediaProbeError(
                    "changed",
                    fingerprint=fingerprint,
                    size_bytes=before.st_size,
                    mtime_ns=before.st_mtime_ns,
                )
            if fingerprint != bounded_file_fingerprint(descriptor, after.st_size):
                raise MediaProbeError(
                    "changed",
                    fingerprint=fingerprint,
                    size_bytes=before.st_size,
                    mtime_ns=before.st_mtime_ns,
                )
            if returncode != 0:
                raise MediaProbeError(
                    "unreadable",
                    fingerprint=fingerprint,
                    size_bytes=before.st_size,
                    mtime_ns=before.st_mtime_ns,
                )
            try:
                summary, streams = _parse_probe(stdout, asset.kind)
            except MediaProbeError as error:
                raise MediaProbeError(
                    error.code,
                    fingerprint=fingerprint,
                    size_bytes=before.st_size,
                    mtime_ns=before.st_mtime_ns,
                ) from error
            return MediaProbeResult(
                asset.kind,
                fingerprint,
                before.st_size,
                before.st_mtime_ns,
                summary,
                streams,
            )
        finally:
            os.close(descriptor)

    def _run(
        self,
        command: tuple[str, ...],
        pass_fds: tuple[int, ...],
        cancel: threading.Event | None = None,
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
                raise MediaProbeError("probe_unavailable") from error
            while process.poll() is None:
                if cancel is not None and cancel.is_set():
                    _terminate(process)
                    raise MediaProbeError("cancelled")
                if time.monotonic() >= deadline:
                    _terminate(process)
                    raise MediaProbeError("timeout")
                if (
                    os.fstat(stdout_file.fileno()).st_size > _MAX_CAPTURE_BYTES
                    or os.fstat(stderr_file.fileno()).st_size > _MAX_CAPTURE_BYTES
                ):
                    _terminate(process)
                    raise MediaProbeError("probe_output_too_large")
                time.sleep(_POLL_SECONDS)
            if (
                os.fstat(stdout_file.fileno()).st_size > _MAX_CAPTURE_BYTES
                or os.fstat(stderr_file.fileno()).st_size > _MAX_CAPTURE_BYTES
            ):
                raise MediaProbeError("probe_output_too_large")
            stdout_file.seek(0)
            stderr_file.seek(0)
            return stdout_file.read(), stderr_file.read(), process.returncode


def bounded_file_fingerprint(descriptor: int, size: int) -> str:
    chunk_size = 64 * 1024
    first = os.pread(descriptor, min(size, chunk_size), 0)
    last_offset = max(0, size - chunk_size)
    last = os.pread(descriptor, min(size, chunk_size), last_offset)
    digest = hashlib.sha256()
    digest.update(b"clipen-bounded-file-fingerprint-v1\0")
    digest.update(str(size).encode("ascii"))
    digest.update(b"\0")
    digest.update(first)
    digest.update(b"\0")
    digest.update(last)
    return digest.hexdigest()


def _parse_probe(
    raw: bytes, expected_kind: AssetKind
) -> tuple[dict[str, object], tuple[MediaStream, ...]]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MediaProbeError("invalid_probe_output") from error
    allowed_top_level = {"format", "streams", "chapters", "programs", "stream_groups"}
    if not isinstance(value, dict) or set(value) - allowed_top_level:
        raise MediaProbeError("invalid_probe_output")
    raw_streams = value.get("streams", [])
    if not isinstance(raw_streams, list) or len(raw_streams) > _MAX_STREAMS:
        raise MediaProbeError("invalid_probe_output")
    streams: list[MediaStream] = []
    for raw_stream in raw_streams:
        if not isinstance(raw_stream, dict):
            raise MediaProbeError("invalid_probe_output")
        kind = raw_stream.get("codec_type")
        if kind not in {"video", "audio"}:
            continue
        index = _integer(raw_stream.get("index"), minimum=0, maximum=4096)
        codec = raw_stream.get("codec_name")
        if not isinstance(codec, str) or _CODEC.fullmatch(codec) is None:
            raise MediaProbeError("invalid_probe_output")
        details: dict[str, object] = {}
        for key in ("time_base", "avg_frame_rate", "r_frame_rate"):
            candidate = raw_stream.get(key)
            if candidate not in (None, "0/0"):
                details[key] = _fraction(candidate, positive=True)
        for key in ("start_pts", "duration_ts"):
            candidate = raw_stream.get(key)
            if candidate is not None:
                details[key] = str(
                    _integer(candidate, minimum=-(2**63), maximum=2**63 - 1)
                )
        if kind == "video":
            details["width"] = _integer(
                raw_stream.get("width"), minimum=1, maximum=131_072
            )
            details["height"] = _integer(
                raw_stream.get("height"), minimum=1, maximum=131_072
            )
            rotation = _rotation(raw_stream)
            if rotation is not None:
                details["rotation"] = rotation
            for key in ("color_space", "color_transfer", "color_primaries", "color_range"):
                candidate = raw_stream.get(key)
                if isinstance(candidate, str) and 1 <= len(candidate) <= 80:
                    details[key] = candidate
        else:
            details["sample_rate"] = _integer(
                raw_stream.get("sample_rate"), minimum=1, maximum=768_000
            )
            details["channels"] = _integer(
                raw_stream.get("channels"), minimum=1, maximum=64
            )
        streams.append(MediaStream(index, kind, codec, details))
    if not streams:
        raise MediaProbeError("unsupported")
    if expected_kind is AssetKind.AUDIO and not any(
        stream.kind == "audio" for stream in streams
    ):
        raise MediaProbeError("unsupported")
    if expected_kind in {AssetKind.VIDEO, AssetKind.IMAGE} and not any(
        stream.kind == "video" for stream in streams
    ):
        raise MediaProbeError("unsupported")
    raw_format = value.get("format", {})
    if not isinstance(raw_format, dict):
        raise MediaProbeError("invalid_probe_output")
    summary: dict[str, object] = {}
    format_name = raw_format.get("format_name")
    if isinstance(format_name, str) and 1 <= len(format_name) <= 240:
        summary["format_names"] = [part for part in format_name.split(",") if part][:16]
    for source, target in (("duration", "duration_seconds"), ("start_time", "start_seconds")):
        candidate = raw_format.get(source)
        if candidate not in (None, "N/A"):
            summary[target] = _decimal(candidate, non_negative=(source == "duration"))
    bit_rate = raw_format.get("bit_rate")
    if bit_rate not in (None, "N/A"):
        summary["bit_rate"] = _integer(bit_rate, minimum=0, maximum=10**13)
    return summary, tuple(streams)


def _integer(value: object, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise MediaProbeError("invalid_probe_output")
    try:
        result = int(value)
    except (TypeError, ValueError) as error:
        raise MediaProbeError("invalid_probe_output") from error
    if str(result) != str(value).strip() or not minimum <= result <= maximum:
        raise MediaProbeError("invalid_probe_output")
    return result


def _fraction(value: object, *, positive: bool) -> str:
    if not isinstance(value, str) or len(value) > 80:
        raise MediaProbeError("invalid_probe_output")
    try:
        result = Fraction(value)
    except (ValueError, ZeroDivisionError) as error:
        raise MediaProbeError("invalid_probe_output") from error
    if positive and result <= 0:
        raise MediaProbeError("invalid_probe_output")
    if abs(result) > 1_000_000:
        raise MediaProbeError("invalid_probe_output")
    return f"{result.numerator}/{result.denominator}"


def _decimal(value: object, *, non_negative: bool) -> str:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise MediaProbeError("invalid_probe_output") from error
    if (
        not result.is_finite()
        or (non_negative and result < 0)
        or abs(result) > Decimal("1e12")
    ):
        raise MediaProbeError("invalid_probe_output")
    return format(result.normalize(), "f")


def _rotation(stream: Mapping[str, Any]) -> int | None:
    tags = stream.get("tags")
    if isinstance(tags, dict) and "rotate" in tags:
        return _integer(tags["rotate"], minimum=-360_000, maximum=360_000) % 360
    side_data = stream.get("side_data_list")
    if isinstance(side_data, list):
        for item in side_data[:16]:
            if isinstance(item, dict) and "rotation" in item:
                return _integer(item["rotation"], minimum=-360_000, maximum=360_000) % 360
    return None


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
