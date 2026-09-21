"""Exact media time values shared by analysis, timelines, and exporters."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping


_DECIMAL_INTEGER = re.compile(r"-?(?:0|[1-9][0-9]*)\Z")
_VERSION_ID = re.compile(r"av_[0-9a-f]{32}\Z")
_MAX_TIME_BASE_COMPONENT = 1_000_000_000
_MAX_STREAM_INDEX = 2**31 - 1
_MIN_PTS = -(2**63)
_MAX_PTS = 2**63 - 1


def _pts(value: object, field: str) -> int:
    if not isinstance(value, str) or _DECIMAL_INTEGER.fullmatch(value) is None:
        raise ValueError(f"{field} must be a decimal integer string")
    parsed = int(value)
    if not _MIN_PTS <= parsed <= _MAX_PTS:
        raise ValueError(f"{field} is outside supported bounds")
    return parsed


@dataclass(frozen=True)
class Rational:
    """A bounded positive rational that preserves the source representation."""

    numerator: int
    denominator: int

    def __post_init__(self) -> None:
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 1
            for value in (self.numerator, self.denominator)
        ):
            raise ValueError("time base must contain positive integers")
        if (
            self.numerator > _MAX_TIME_BASE_COMPONENT
            or self.denominator > _MAX_TIME_BASE_COMPONENT
        ):
            raise ValueError("time base is outside supported bounds")

    def to_storage(self) -> dict[str, int]:
        return {"num": self.numerator, "den": self.denominator}

    @classmethod
    def from_storage(cls, value: Mapping[str, Any]) -> "Rational":
        if not isinstance(value, Mapping) or set(value) != {"num", "den"}:
            raise ValueError("stored time base is invalid")
        return cls(value["num"], value["den"])


@dataclass(frozen=True)
class SourceSpan:
    """A nonempty half-open PTS interval on one immutable media stream."""

    version_id: str
    stream_index: int
    start_pts: str
    end_pts: str
    time_base: Rational

    def __post_init__(self) -> None:
        if (
            not isinstance(self.version_id, str)
            or _VERSION_ID.fullmatch(self.version_id) is None
        ):
            raise ValueError("source span version is invalid")
        if (
            isinstance(self.stream_index, bool)
            or not isinstance(self.stream_index, int)
            or not 0 <= self.stream_index <= _MAX_STREAM_INDEX
        ):
            raise ValueError("source stream index is invalid")
        start = _pts(self.start_pts, "start_pts")
        end = _pts(self.end_pts, "end_pts")
        if end <= start:
            raise ValueError("source span must be a non-empty half-open interval")
        if not isinstance(self.time_base, Rational):
            raise ValueError("source span time base is invalid")

    def to_storage(self) -> dict[str, object]:
        return {
            "version_id": self.version_id,
            "stream_index": self.stream_index,
            "start_pts": self.start_pts,
            "end_pts": self.end_pts,
            "time_base": self.time_base.to_storage(),
        }

    @property
    def duration_pts(self) -> int:
        return int(self.end_pts) - int(self.start_pts)

    @classmethod
    def from_storage(cls, value: Mapping[str, Any]) -> "SourceSpan":
        if not isinstance(value, Mapping) or set(value) != {
            "version_id",
            "stream_index",
            "start_pts",
            "end_pts",
            "time_base",
        }:
            raise ValueError("stored source span is invalid")
        raw_time_base = value["time_base"]
        if not isinstance(raw_time_base, Mapping):
            raise ValueError("stored time base is invalid")
        return cls(
            value["version_id"],
            value["stream_index"],
            value["start_pts"],
            value["end_pts"],
            Rational.from_storage(raw_time_base),
        )
