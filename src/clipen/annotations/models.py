"""Validated annotation values shared by storage and planning."""

from __future__ import annotations

from enum import Enum


class DayPart(str, Enum):
    DAWN = "dawn"
    MORNING = "morning"
    NOON = "noon"
    AFTERNOON = "afternoon"
    EVENING = "evening"
    NIGHT = "night"
    LATE_NIGHT = "late_night"


class MaterialUsage(str, Enum):
    NORMAL = "normal"
    MUST_USE = "must_use"
    EXCLUDE = "exclude"
    AMBIENT_ONLY = "ambient_only"
