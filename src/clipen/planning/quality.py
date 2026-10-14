"""Deterministic checks for whether a Plan can satisfy the user's requested runtime."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import re

from clipen.domain import PlanRevision

from .models import PlanningContext


_DURATION_CUE = re.compile(r"(?:總時長|成片|片長|影片長度|剪成|約莫|大約|約)")
_DURATION = re.compile(
    r"([0-9]+(?:\.[0-9]+)?|[零〇一二兩三四五六七八九十百]+)\s*"
    r"(小時|鐘頭|分鐘|秒鐘|秒)"
)
_CHINESE_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "兩": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}


@dataclass(frozen=True, slots=True)
class PlanQuality:
    target_duration_seconds: float | None
    selected_video_duration_seconds: float
    selected_video_count: int
    issue_code: str | None

    def to_public(self) -> dict[str, object]:
        return {
            "target_duration_seconds": self.target_duration_seconds,
            "selected_video_duration_seconds": self.selected_video_duration_seconds,
            "selected_video_count": self.selected_video_count,
            "issue_code": self.issue_code,
        }


def requested_duration_seconds(brief: str) -> Fraction | None:
    """Read an explicit requested runtime while ignoring unrelated bare durations."""

    for match in _DURATION.finditer(brief):
        prefix = brief[max(0, match.start() - 16) : match.start()]
        if not _DURATION_CUE.search(prefix):
            continue
        amount = _number(match.group(1))
        if amount is None or amount <= 0:
            continue
        unit = match.group(2)
        if unit in {"小時", "鐘頭"}:
            return amount * 3600
        if unit == "分鐘":
            return amount * 60
        return amount
    return None


def assess_plan_quality(plan: PlanRevision, context: PlanningContext) -> PlanQuality:
    candidates = context.candidate_map()
    selected_video = []
    for section in plan.sections:
        for source in section.source_ranges:
            candidate = candidates.get(source.candidate_id)
            if candidate is not None and candidate.media_kind == "video":
                selected_video.append(candidate)
    duration = sum(
        (
            Fraction(candidate.source.duration_pts)
            * candidate.source.time_base.numerator
            / candidate.source.time_base.denominator
            for candidate in selected_video
        ),
        Fraction(0),
    )
    target = requested_duration_seconds(context.brief)
    issue = None
    if target is not None and duration < target * Fraction(4, 5):
        issue = "plan_video_coverage_too_short"
    return PlanQuality(
        None if target is None else round(float(target), 3),
        round(float(duration), 3),
        len(selected_video),
        issue,
    )


def _number(value: str) -> Fraction | None:
    if value[0].isdigit():
        try:
            return Fraction(value)
        except (ValueError, ZeroDivisionError):
            return None
    if value == "十":
        return Fraction(10)
    if "百" in value:
        left, _, right = value.partition("百")
        hundreds = _CHINESE_DIGITS.get(left, 1) * 100
        tail = _chinese_under_hundred(right)
        return None if tail is None else Fraction(hundreds + tail)
    parsed = _chinese_under_hundred(value)
    return None if parsed is None else Fraction(parsed)


def _chinese_under_hundred(value: str) -> int | None:
    if not value:
        return 0
    if "十" in value:
        left, _, right = value.partition("十")
        tens = _CHINESE_DIGITS.get(left, 1)
        ones = _CHINESE_DIGITS.get(right, 0)
        return tens * 10 + ones
    digits = [_CHINESE_DIGITS.get(character) for character in value]
    if any(digit is None for digit in digits):
        return None
    return int("".join(str(digit) for digit in digits))
