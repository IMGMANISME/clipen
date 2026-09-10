"""Source-bound evidence coverage; sampled frames never imply interval coverage."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .models import SourceSpan


class EvidenceModality(str, Enum):
    VISUAL = "visual"
    AUDIO = "audio"
    SPEECH = "speech"


@dataclass(frozen=True)
class CoverageInterval:
    start_pts: int
    end_pts: int

    def __post_init__(self) -> None:
        if any(type(value) is not int for value in (self.start_pts, self.end_pts)):
            raise ValueError("coverage PTS must be integers")
        if self.end_pts <= self.start_pts:
            raise ValueError("coverage interval must be nonempty")


@dataclass(frozen=True)
class EvidenceCoverage:
    """Coverage of one modality on one immutable source stream.

    Intervals represent continuously inspected source ranges. Frame timestamps
    are separate point observations and do not reduce the uncovered ranges.
    Empty coverage is valid and must never be interpreted as no content.
    """

    source: SourceSpan
    modality: EvidenceModality
    inspected: tuple[CoverageInterval, ...] = ()
    sampled_pts: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.source, SourceSpan):
            raise ValueError("coverage requires a validated source span")
        if not isinstance(self.modality, EvidenceModality):
            raise ValueError("coverage modality is invalid")
        if type(self.inspected) is not tuple or type(self.sampled_pts) is not tuple:
            raise ValueError("coverage collections must be immutable")
        if len(self.inspected) > 100_000 or len(self.sampled_pts) > 100_000:
            raise ValueError("coverage exceeds item limit")
        start, end = int(self.source.start_pts), int(self.source.end_pts)
        for interval in self.inspected:
            if not isinstance(interval, CoverageInterval):
                raise ValueError("invalid inspected interval")
            if interval.start_pts < start or interval.end_pts > end:
                raise ValueError("inspection lies outside source span")
        if self.sampled_pts and self.modality is not EvidenceModality.VISUAL:
            raise ValueError("frame samples require visual modality")
        for point in self.sampled_pts:
            if type(point) is not int or not start <= point < end:
                raise ValueError("sample lies outside source span")
        if tuple(sorted(set(self.sampled_pts))) != self.sampled_pts:
            raise ValueError("frame samples must be unique and ordered")

    @property
    def merged_intervals(self) -> tuple[CoverageInterval, ...]:
        merged: list[CoverageInterval] = []
        for interval in sorted(self.inspected, key=lambda value: value.start_pts):
            if merged and interval.start_pts <= merged[-1].end_pts:
                previous = merged[-1]
                merged[-1] = CoverageInterval(previous.start_pts, max(previous.end_pts, interval.end_pts))
            else:
                merged.append(interval)
        return tuple(merged)

    @property
    def uncovered(self) -> tuple[CoverageInterval, ...]:
        cursor = int(self.source.start_pts)
        missing: list[CoverageInterval] = []
        for interval in self.merged_intervals:
            if cursor < interval.start_pts:
                missing.append(CoverageInterval(cursor, interval.start_pts))
            cursor = interval.end_pts
        end = int(self.source.end_pts)
        if cursor < end:
            missing.append(CoverageInterval(cursor, end))
        return tuple(missing)

    def to_storage(self) -> dict[str, object]:
        def ranges(values: tuple[CoverageInterval, ...]) -> list[dict[str, str]]:
            return [{"start_pts": str(v.start_pts), "end_pts": str(v.end_pts)} for v in values]

        return {
            "schema_version": 1,
            "source": self.source.to_storage(),
            "modality": self.modality.value,
            "inspected": ranges(self.merged_intervals),
            "sampled_pts": [str(value) for value in self.sampled_pts],
            "uncovered": ranges(self.uncovered),
            "complete": not self.uncovered,
        }
