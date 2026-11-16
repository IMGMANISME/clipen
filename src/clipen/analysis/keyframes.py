"""Deterministic, bounded keyframe selection for local visual evidence.

The selector follows the relevance-versus-coverage principle described by
Adaptive Keyframe Sampling (Tang et al., CVPR 2025), but is an independent
implementation. It consumes scores produced by Clipen; it does not contain
or copy code from the unlicensed reference repository.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True, slots=True)
class KeyframeCandidate:
    pts: int
    score: float
    source: str

    def __post_init__(self) -> None:
        if isinstance(self.pts, bool) or not isinstance(self.pts, int):
            raise ValueError("keyframe pts must be an integer")
        if not isinstance(self.score, (int, float)) or isinstance(self.score, bool):
            raise ValueError("keyframe score must be numeric")
        if not math.isfinite(float(self.score)) or not 0 <= float(self.score) <= 1:
            raise ValueError("keyframe score must be between zero and one")
        if self.source not in {"codec", "uniform", "semantic"}:
            raise ValueError("unknown keyframe candidate source")


@dataclass(frozen=True, slots=True)
class KeyframeSelection:
    candidates: tuple[KeyframeCandidate, ...]
    method: str
    semantic_relevance: bool


class AdaptiveKeyframeSelector:
    """Balance scored moments with temporal coverage under a bounded budget."""

    MAXIMUM_BUDGET = 48

    def __init__(self, *, split_threshold: float = 0.22, maximum_depth: int = 6):
        if not 0 <= split_threshold <= 1:
            raise ValueError("split threshold must be between zero and one")
        if isinstance(maximum_depth, bool) or not 1 <= maximum_depth <= 16:
            raise ValueError("maximum depth is invalid")
        self.split_threshold = split_threshold
        self.maximum_depth = maximum_depth

    def select(
        self,
        start_pts: int,
        end_pts: int,
        candidates: Iterable[KeyframeCandidate],
        budget: int,
    ) -> KeyframeSelection:
        if (
            isinstance(start_pts, bool)
            or isinstance(end_pts, bool)
            or not isinstance(start_pts, int)
            or not isinstance(end_pts, int)
            or end_pts <= start_pts
        ):
            raise ValueError("keyframe source span is invalid")
        if isinstance(budget, bool) or not isinstance(budget, int) or not 1 <= budget <= self.MAXIMUM_BUDGET:
            raise ValueError("keyframe budget is invalid")

        deduplicated: dict[int, KeyframeCandidate] = {}
        for candidate in candidates:
            if not start_pts <= candidate.pts < end_pts:
                raise ValueError("keyframe candidate lies outside source span")
            previous = deduplicated.get(candidate.pts)
            if previous is None or self._priority(candidate) > self._priority(previous):
                deduplicated[candidate.pts] = candidate
        ordered = tuple(sorted(deduplicated.values(), key=lambda item: item.pts))
        if not ordered:
            raise ValueError("at least one keyframe candidate is required")

        semantic_available = any(item.source == "semantic" for item in ordered)
        chosen = self._choose(
            ordered,
            min(budget, len(ordered)),
            start_pts,
            end_pts,
            0,
            semantic_available,
        )
        semantic = any(item.source == "semantic" for item in chosen)
        method = "adaptive_relevance_coverage_v1" if len(ordered) > budget else "bounded_all_v1"
        return KeyframeSelection(tuple(sorted(chosen, key=lambda item: item.pts)), method, semantic)

    @staticmethod
    def budget_for_duration(duration_seconds: float) -> int:
        """Return a bounded upper limit; visual deduplication may retain fewer frames."""

        if not isinstance(duration_seconds, (int, float)) or isinstance(duration_seconds, bool):
            raise ValueError("keyframe duration must be numeric")
        if not math.isfinite(float(duration_seconds)) or duration_seconds <= 0:
            raise ValueError("keyframe duration must be positive")
        if duration_seconds <= 2:
            return 1
        return min(
            AdaptiveKeyframeSelector.MAXIMUM_BUDGET,
            math.ceil(duration_seconds / 8) + 1,
        )

    @staticmethod
    def uniform(start_pts: int, end_pts: int, count: int) -> tuple[KeyframeCandidate, ...]:
        if end_pts <= start_pts or isinstance(count, bool) or not 1 <= count <= 256:
            raise ValueError("uniform keyframe request is invalid")
        duration = end_pts - start_pts
        points = {start_pts + duration * (index * 2 + 1) // (count * 2) for index in range(count)}
        return tuple(KeyframeCandidate(point, 0.0, "uniform") for point in sorted(points))

    def _choose(
        self,
        candidates: tuple[KeyframeCandidate, ...],
        quota: int,
        lower: int,
        upper: int,
        depth: int,
        allow_relevance_focus: bool,
    ) -> tuple[KeyframeCandidate, ...]:
        if quota >= len(candidates):
            return candidates
        center = lower + (upper - lower) / 2
        if quota == 1:
            return (max(candidates, key=lambda item: (item.score, -abs(item.pts - center), -item.pts)),)

        top = sorted(candidates, key=lambda item: (-item.score, abs(item.pts - center), item.pts))[:quota]
        advantage = sum(item.score for item in top) / quota - sum(item.score for item in candidates) / len(candidates)
        if (allow_relevance_focus and advantage >= self.split_threshold) or depth >= self.maximum_depth:
            return tuple(top)

        midpoint = lower + (upper - lower) // 2
        left = tuple(item for item in candidates if item.pts < midpoint)
        right = tuple(item for item in candidates if item.pts >= midpoint)
        if not left or not right:
            return tuple(top)

        left_quota = quota // 2
        right_quota = quota - left_quota
        if len(left) < left_quota:
            right_quota += left_quota - len(left)
            left_quota = len(left)
        if len(right) < right_quota:
            left_quota += right_quota - len(right)
            right_quota = len(right)
        return self._choose(left, left_quota, lower, midpoint, depth + 1, allow_relevance_focus) + self._choose(
            right, right_quota, midpoint, upper, depth + 1, allow_relevance_focus
        )

    @staticmethod
    def _priority(candidate: KeyframeCandidate) -> tuple[float, int]:
        source_priority = {"uniform": 0, "codec": 1, "semantic": 2}
        return float(candidate.score), source_priority[candidate.source]
