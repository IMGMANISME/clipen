"""Validated records for local media preparation and transcription."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
import re
from typing import Any, Mapping

from clipen.projects.models import validate_project_id


_ARTIFACT_ID = re.compile(r"art_[0-9a-f]{32}\Z")
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
