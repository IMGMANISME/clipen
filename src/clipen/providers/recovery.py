"""Durable, bounded receipts for reconciling one-shot provider attempts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import uuid


_RECEIPT_SCHEMA = 1
_MAX_RECEIPT_BYTES = 64 * 1024
_SHA256_HEX_LENGTH = 64


class AttemptReceiptError(RuntimeError):
    """A persisted provider receipt is missing, unsafe, or malformed."""


class ProviderAttemptState(str, Enum):
    PREPARED = "prepared"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    INTERRUPTED = "interrupted"
    UNKNOWN = "unknown"


class ProviderArtifactRole(str, Enum):
    RECEIPT = "receipt"
    OUTPUT_SCHEMA = "output_schema"
    OUTPUT = "output"
    EVENTS = "events"
    DIAGNOSTICS = "diagnostics"


class ProviderArtifactDisposition(str, Enum):
    RETAIN_RECOVERY = "retain_recovery"
    RETAIN_RESULT = "retain_result"
    RETAIN_DIAGNOSTIC = "retain_diagnostic"
    CLEANABLE = "cleanable"


@dataclass(frozen=True)
class ProviderArtifactDecision:
    role: ProviderArtifactRole
    filename: str
    disposition: ProviderArtifactDisposition
    reason: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.filename, str)
            or not self.filename
            or Path(self.filename).name != self.filename
        ):
            raise ValueError("provider artifact filename must be one local name")
        if not isinstance(self.reason, str) or not self.reason or len(self.reason) > 128:
            raise ValueError("provider artifact reason must be bounded")


@dataclass(frozen=True)
class ProviderUsage:
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int

    def __post_init__(self) -> None:
        for value in (
            self.input_tokens,
            self.cached_input_tokens,
            self.output_tokens,
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError("provider token usage must contain non-negative integers")
        if self.cached_input_tokens > self.input_tokens:
            raise ValueError("cached input tokens cannot exceed input tokens")

    def to_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "output_tokens": self.output_tokens,
        }

    @classmethod
    def from_dict(cls, value: object) -> "ProviderUsage":
        if not isinstance(value, dict) or set(value) != {
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
        }:
            raise ValueError("provider token usage is malformed")
        return cls(
            input_tokens=value["input_tokens"],
            cached_input_tokens=value["cached_input_tokens"],
            output_tokens=value["output_tokens"],
        )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path, limit: int) -> str:
    if path.is_symlink() or not path.is_file():
        raise AttemptReceiptError("provider artifact is missing or unsafe")
    size = path.stat().st_size
    if size > limit:
        raise AttemptReceiptError("provider artifact exceeds its size limit")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        remaining = size
        while remaining:
            block = stream.read(min(64 * 1024, remaining))
            if not block:
                raise AttemptReceiptError("provider artifact changed while hashing")
            digest.update(block)
            remaining -= len(block)
    return digest.hexdigest()


def _optional_hash(value: object, field: str) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or len(value) != _SHA256_HEX_LENGTH
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field} must be a SHA-256 hex digest")
    return value


def _optional_string(value: object, field: str, limit: int = 128) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > limit:
        raise ValueError(f"{field} must be a bounded string")
    return value


def _optional_integer(value: object, field: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


@dataclass(frozen=True)
class ProviderAttemptReceipt:
    attempt_id: str
    state: ProviderAttemptState
    prompt_sha256: str
    schema_sha256: str
    started_at: str | None = None
    finished_at: str | None = None
    pid: int | None = None
    return_code: int | None = None
    output_sha256: str | None = None
    events_sha256: str | None = None
    diagnostics_sha256: str | None = None
    usage: ProviderUsage | None = None
    error_code: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.attempt_id, str) or not self.attempt_id or len(self.attempt_id) > 128:
            raise ValueError("attempt_id must be a bounded string")
        _optional_hash(self.prompt_sha256, "prompt_sha256")
        _optional_hash(self.schema_sha256, "schema_sha256")
        _optional_hash(self.output_sha256, "output_sha256")
        _optional_hash(self.events_sha256, "events_sha256")
        _optional_hash(self.diagnostics_sha256, "diagnostics_sha256")
        _optional_string(self.started_at, "started_at", 64)
        _optional_string(self.finished_at, "finished_at", 64)
        _optional_integer(self.pid, "pid")
        if self.return_code is not None and (
            not isinstance(self.return_code, int) or isinstance(self.return_code, bool)
        ):
            raise ValueError("return_code must be an integer")
        _optional_string(self.error_code, "error_code", 64)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": _RECEIPT_SCHEMA,
            "provider": "codex",
            "attempt_id": self.attempt_id,
            "state": self.state.value,
            "prompt_sha256": self.prompt_sha256,
            "schema_sha256": self.schema_sha256,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "pid": self.pid,
            "return_code": self.return_code,
            "output_sha256": self.output_sha256,
            "events_sha256": self.events_sha256,
            "diagnostics_sha256": self.diagnostics_sha256,
            "usage": None if self.usage is None else self.usage.to_dict(),
            "error_code": self.error_code,
        }

    @classmethod
    def from_dict(cls, value: object) -> "ProviderAttemptReceipt":
        expected = {
            "schema",
            "provider",
            "attempt_id",
            "state",
            "prompt_sha256",
            "schema_sha256",
            "started_at",
            "finished_at",
            "pid",
            "return_code",
            "output_sha256",
            "events_sha256",
            "diagnostics_sha256",
            "usage",
            "error_code",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError("provider attempt receipt has unexpected fields")
        if value["schema"] != _RECEIPT_SCHEMA or value["provider"] != "codex":
            raise ValueError("provider attempt receipt version is unsupported")
        usage = None if value["usage"] is None else ProviderUsage.from_dict(value["usage"])
        return cls(
            attempt_id=value["attempt_id"],
            state=ProviderAttemptState(value["state"]),
            prompt_sha256=value["prompt_sha256"],
            schema_sha256=value["schema_sha256"],
            started_at=value["started_at"],
            finished_at=value["finished_at"],
            pid=value["pid"],
            return_code=value["return_code"],
            output_sha256=value["output_sha256"],
            events_sha256=value["events_sha256"],
            diagnostics_sha256=value["diagnostics_sha256"],
            usage=usage,
            error_code=value["error_code"],
        )


class AttemptReceiptStore:
    """Atomically replace one private receipt inside an attempt workspace."""

    def __init__(self, workspace: Path, attempt_id: str) -> None:
        if workspace.is_symlink():
            raise AttemptReceiptError("attempt workspace cannot be a symbolic link")
        self.workspace = workspace.resolve(strict=True)
        if not self.workspace.is_dir():
            raise AttemptReceiptError("attempt workspace must be a directory")
        self.path = self.workspace / f"provider-attempt-{attempt_id}.json"

    def write(self, receipt: ProviderAttemptReceipt) -> None:
        if self.path.is_symlink():
            raise AttemptReceiptError("provider attempt receipt cannot be a symbolic link")
        encoded = json.dumps(
            receipt.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        if len(encoded) > _MAX_RECEIPT_BYTES:
            raise AttemptReceiptError("provider attempt receipt exceeds its size limit")
        temporary = self.workspace / f".receipt-{receipt.attempt_id}-{uuid.uuid4().hex}"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
            directory = os.open(self.workspace, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if temporary.exists() and not temporary.is_symlink():
                temporary.unlink()

    def read(self) -> ProviderAttemptReceipt:
        if self.path.is_symlink() or not self.path.is_file():
            raise AttemptReceiptError("provider attempt receipt is missing or unsafe")
        if self.path.stat().st_size > _MAX_RECEIPT_BYTES:
            raise AttemptReceiptError("provider attempt receipt exceeds its size limit")
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            receipt = ProviderAttemptReceipt.from_dict(value)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
            raise AttemptReceiptError("provider attempt receipt is malformed") from error
        if self.path.stat().st_mode & 0o077:
            raise AttemptReceiptError("provider attempt receipt permissions are unsafe")
        return receipt
