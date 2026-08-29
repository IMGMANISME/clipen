"""Immutable Plan revision lifecycle."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Mapping, AbstractSet

from .operations import InvalidTransition, _require_identifier


class PlanStatus(str, Enum):
    DRAFT = "draft"
    READY_FOR_REVIEW = "ready_for_review"
    CONFIRMED = "confirmed"
    EXECUTING = "executing"
    REVIEWED = "reviewed"
    ACCEPTED = "accepted"
    SUPERSEDED = "superseded"


_PLAN_TRANSITIONS: Mapping[PlanStatus, AbstractSet[PlanStatus]] = {
    PlanStatus.DRAFT: {PlanStatus.READY_FOR_REVIEW, PlanStatus.SUPERSEDED},
    PlanStatus.READY_FOR_REVIEW: {PlanStatus.CONFIRMED, PlanStatus.SUPERSEDED},
    PlanStatus.CONFIRMED: {PlanStatus.EXECUTING, PlanStatus.SUPERSEDED},
    PlanStatus.EXECUTING: {PlanStatus.REVIEWED},
    PlanStatus.REVIEWED: {PlanStatus.ACCEPTED, PlanStatus.SUPERSEDED},
    PlanStatus.ACCEPTED: set(),
    PlanStatus.SUPERSEDED: set(),
}


@dataclass(frozen=True)
class PlanRevision:
    plan_id: str
    project_id: str
    revision: int
    content_hash: str
    material_snapshot_id: str
    analysis_snapshot_id: str
    system_skill_release: str
    status: PlanStatus = PlanStatus.DRAFT

    def __post_init__(self) -> None:
        for field in (
            "plan_id",
            "project_id",
            "material_snapshot_id",
            "analysis_snapshot_id",
            "system_skill_release",
        ):
            _require_identifier(getattr(self, field), field)
        if self.revision < 1:
            raise ValueError("plan revision must start at one")
        if len(self.content_hash) != 64 or any(character not in "0123456789abcdef" for character in self.content_hash):
            raise ValueError("content_hash must be a lowercase SHA-256 digest")

    def transition(self, target: PlanStatus) -> "PlanRevision":
        if target not in _PLAN_TRANSITIONS[self.status]:
            raise InvalidTransition(f"plan cannot move from {self.status} to {target}")
        return replace(self, status=target)

    @property
    def can_execute(self) -> bool:
        return self.status is PlanStatus.CONFIRMED
