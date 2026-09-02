"""Validated records for local media preparation and transcription."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
import re
from typing import Any, Mapping

from clipen.projects.models import validate_project_id


_ARTIFACT_ID = re.compile(r"art_[0-9a-f]{32}\Z")
_PREPARATION_ID = re.compile(r"prep_[0-9a-f]{32}\Z")
_OPERATION_ID = re.compile(r"op_[0-9a-f]{32}\Z")
_ATTEMPT_ID = re.compile(r"att_[0-9a-f]{32}\Z")
_VERSION_ID = re.compile(r"av_[0-9a-f]{32}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_RECIPE_ID = re.compile(r"[a-z][a-z0-9_.-]{0,127}\Z")
_INTEGER = re.compile(r"-?(?:0|[1-9][0-9]*)\Z")


class ArtifactKind(str, Enum):
    ANALYSIS_AUDIO = "analysis_audio"
    TRANSCRIPT = "transcript"
    COVERAGE_MANIFEST = "coverage_manifest"
    CHECKPOINT = "checkpoint"


class PreparationState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    CANCELLING = "cancelling"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class PreparationItemStatus(str, Enum):
    PENDING = "pending"
    PREPARED = "prepared"
    TRANSCRIBED = "transcribed"
    NO_AUDIO = "no_audio"
    NO_SPEECH = "no_speech"
    FAILED = "failed"


@dataclass(frozen=True)
class PreparationCounts:
    total: int = 0
    prepared: int = 0
    transcribed: int = 0
    no_audio: int = 0
    no_speech: int = 0
    issues: int = 0

    def __post_init__(self) -> None:
        values = (
            self.total,
            self.prepared,
            self.transcribed,
            self.no_audio,
            self.no_speech,
            self.issues,
        )
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values):
            raise ValueError("preparation counts must be non-negative integers")
        if self.transcribed + self.no_audio + self.no_speech + self.issues > self.total:
            raise ValueError("terminal preparation counts exceed total inputs")
        if self.prepared > self.total:
            raise ValueError("prepared count exceeds total inputs")

    def to_public(self) -> dict[str, int]:
        return {
            "total": self.total,
            "prepared": self.prepared,
            "transcribed": self.transcribed,
            "no_audio": self.no_audio,
            "no_speech": self.no_speech,
            "issues": self.issues,
        }


@dataclass(frozen=True)
class PreparationRun:
    run_id: str
    project_id: str
    project_revision: int
    inventory_run_id: str
    scope_id: str
    operation_id: str
    attempt_id: str
    attempt_number: int
    input_fingerprint: str
    state: PreparationState
    phase: str
    counts: PreparationCounts
    error_code: str | None
    created_at: str
    started_at: str | None = None
    completed_at: str | None = None

    def __post_init__(self) -> None:
        if _PREPARATION_ID.fullmatch(self.run_id) is None:
            raise ValueError("preparation run ID is invalid")
        validate_project_id(self.project_id)
        if self.project_revision < 1:
            raise ValueError("preparation project revision is invalid")
        if re.fullmatch(r"inv_[0-9a-f]{32}", self.inventory_run_id) is None:
            raise ValueError("preparation inventory ID is invalid")
        if re.fullmatch(r"scope_[0-9a-f]{32}", self.scope_id) is None:
            raise ValueError("preparation scope ID is invalid")
        validate_operation_id(self.operation_id)
        validate_attempt_id(self.attempt_id)
        if self.attempt_number < 1 or _SHA256.fullmatch(self.input_fingerprint) is None:
            raise ValueError("preparation input identity is invalid")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", self.phase):
            raise ValueError("preparation phase is invalid")
        if self.error_code is not None and not re.fullmatch(
            r"[a-z][a-z0-9_]{0,63}", self.error_code
        ):
            raise ValueError("preparation error code is invalid")
        terminal = self.state in {
            PreparationState.SUCCEEDED,
            PreparationState.FAILED,
            PreparationState.CANCELLED,
            PreparationState.INTERRUPTED,
        }
        if terminal != (self.completed_at is not None):
            raise ValueError("preparation completion timestamp does not match state")
        if self.state is not PreparationState.QUEUED and self.started_at is None:
            raise ValueError("started preparation state requires timestamp")

    def to_public(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "project_revision": self.project_revision,
            "inventory_run_id": self.inventory_run_id,
            "scope_id": self.scope_id,
            "state": self.state.value,
            "phase": self.phase,
            "counts": self.counts.to_public(),
            "error_code": self.error_code,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }


@dataclass(frozen=True)
class Rational:
    numerator: int
    denominator: int

    def __post_init__(self) -> None:
        if (
            isinstance(self.numerator, bool)
            or not isinstance(self.numerator, int)
            or isinstance(self.denominator, bool)
            or not isinstance(self.denominator, int)
            or self.denominator <= 0
        ):
            raise ValueError("time base must contain bounded integers")
        if abs(self.numerator) > 1_000_000_000 or self.denominator > 1_000_000_000:
            raise ValueError("time base is outside supported bounds")

    def to_storage(self) -> dict[str, int]:
        return {"num": self.numerator, "den": self.denominator}

    @classmethod
    def from_storage(cls, value: Mapping[str, Any]) -> "Rational":
        return cls(int(value["num"]), int(value["den"]))


@dataclass(frozen=True)
class SourceSpan:
    version_id: str
    stream_index: int
    start_pts: str
    end_pts: str
    time_base: Rational

    def __post_init__(self) -> None:
        if _VERSION_ID.fullmatch(self.version_id) is None:
            raise ValueError("source span version is invalid")
        if (
            isinstance(self.stream_index, bool)
            or not isinstance(self.stream_index, int)
            or self.stream_index < 0
        ):
            raise ValueError("source stream index is invalid")
        if _INTEGER.fullmatch(self.start_pts) is None or _INTEGER.fullmatch(self.end_pts) is None:
            raise ValueError("source timestamps must be decimal integers")
        if int(self.end_pts) <= int(self.start_pts):
            raise ValueError("source span must be a non-empty half-open interval")

    def to_storage(self) -> dict[str, object]:
        return {
            "version_id": self.version_id,
            "stream_index": self.stream_index,
            "start_pts": self.start_pts,
            "end_pts": self.end_pts,
            "time_base": self.time_base.to_storage(),
        }

    @classmethod
    def from_storage(cls, value: Mapping[str, Any]) -> "SourceSpan":
        raw_time_base = value["time_base"]
        if not isinstance(raw_time_base, Mapping):
            raise ValueError("stored time base is invalid")
        return cls(
            str(value["version_id"]),
            int(value["stream_index"]),
            str(value["start_pts"]),
            str(value["end_pts"]),
            Rational.from_storage(raw_time_base),
        )


@dataclass(frozen=True)
class ArtifactRecord:
    artifact_id: str
    project_id: str
    kind: ArtifactKind
    relative_path: str
    sha256: str
    size_bytes: int
    producer_operation_id: str
    producer_attempt_id: str
    recipe_id: str
    lineage: Mapping[str, object]
    created_at: str
    source_span: SourceSpan | None = None

    def __post_init__(self) -> None:
        if _ARTIFACT_ID.fullmatch(self.artifact_id) is None:
            raise ValueError("artifact ID is invalid")
        validate_project_id(self.project_id)
        if not self.relative_path.startswith("artifacts/sha256/") or ".." in self.relative_path.split("/"):
            raise ValueError("artifact path is outside the managed store")
        if _SHA256.fullmatch(self.sha256) is None:
            raise ValueError("artifact digest is invalid")
        if isinstance(self.size_bytes, bool) or not 0 <= self.size_bytes <= 64 * 1024**3:
            raise ValueError("artifact size is invalid")
        if _OPERATION_ID.fullmatch(self.producer_operation_id) is None:
            raise ValueError("artifact producer operation is invalid")
        if _ATTEMPT_ID.fullmatch(self.producer_attempt_id) is None:
            raise ValueError("artifact producer attempt is invalid")
        if _RECIPE_ID.fullmatch(self.recipe_id) is None:
            raise ValueError("artifact recipe is invalid")
        encoded = json.dumps(self.lineage, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 64 * 1024:
            raise ValueError("artifact lineage is too large")
        if not self.created_at:
            raise ValueError("artifact creation time is missing")


def validate_operation_id(value: object) -> str:
    if not isinstance(value, str) or _OPERATION_ID.fullmatch(value) is None:
        raise ValueError("operation ID is invalid")
    return value


def validate_attempt_id(value: object) -> str:
    if not isinstance(value, str) or _ATTEMPT_ID.fullmatch(value) is None:
        raise ValueError("attempt ID is invalid")
    return value


def validate_recipe_id(value: object) -> str:
    if not isinstance(value, str) or _RECIPE_ID.fullmatch(value) is None:
        raise ValueError("recipe ID is invalid")
    return value
