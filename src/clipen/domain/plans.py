"""Immutable, user-editable Plan revisions between AI proposals and timelines."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import hashlib
import json
from typing import AbstractSet, Mapping

from .errors import InvalidTransition
from .media_time import SourceSpan
from .validation import require_identifier


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


def _bounded_text(value: object, field: str, maximum: int, *, allow_empty: bool = False) -> None:
    if (
        not isinstance(value, str)
        or "\x00" in value
        or len(value) > maximum
        or (not allow_empty and not value.strip())
    ):
        raise ValueError(f"{field} is invalid")


@dataclass(frozen=True)
class PlanSourceRange:
    """A provider-selected candidate resolved to an exact local source span."""

    candidate_id: str
    source: SourceSpan

    def __post_init__(self) -> None:
        require_identifier(self.candidate_id, "candidate_id")
        if not isinstance(self.source, SourceSpan):
            raise ValueError("plan source range is invalid")

    def to_payload(self) -> dict[str, object]:
        return {"candidate_id": self.candidate_id, **self.source.to_storage()}


@dataclass(frozen=True)
class PlanSection:
    section_id: str
    title: str
    purpose: str
    source_ranges: tuple[PlanSourceRange, ...]
    edit_notes: str = ""
    unknowns: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        require_identifier(self.section_id, "section_id")
        _bounded_text(self.title, "plan section title", 160)
        _bounded_text(self.purpose, "plan section purpose", 2_000)
        _bounded_text(self.edit_notes, "plan section edit notes", 4_000, allow_empty=True)
        ranges = tuple(self.source_ranges)
        unknowns = tuple(self.unknowns)
        if not ranges or len(ranges) > 512:
            raise ValueError("plan section source ranges are invalid")
        if any(not isinstance(item, PlanSourceRange) for item in ranges):
            raise ValueError("plan section contains an invalid source range")
        if len({item.candidate_id for item in ranges}) != len(ranges):
            raise ValueError("plan section candidate IDs must be unique")
        if len(unknowns) > 128:
            raise ValueError("plan section has too many unknowns")
        for unknown in unknowns:
            _bounded_text(unknown, "plan section unknown", 500)
        if len(unknowns) != len(set(unknowns)):
            raise ValueError("plan section unknowns must be unique")
        object.__setattr__(self, "source_ranges", ranges)
        object.__setattr__(self, "unknowns", unknowns)

    def to_payload(self) -> dict[str, object]:
        return {
            "section_id": self.section_id,
            "title": self.title,
            "purpose": self.purpose,
            "source_ranges": [item.to_payload() for item in self.source_ranges],
            "edit_notes": self.edit_notes,
            "unknowns": list(self.unknowns),
        }


def plan_content_hash(title: str, summary: str, sections: tuple[PlanSection, ...]) -> str:
    payload = {
        "title": title,
        "summary": summary,
        "sections": [section.to_payload() for section in sections],
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class PlanRevision:
    plan_id: str
    project_id: str
    revision: int
    content_hash: str
    material_snapshot_id: str
    analysis_snapshot_id: str
    system_skill_release: str
    title: str
    summary: str
    sections: tuple[PlanSection, ...]
    status: PlanStatus = PlanStatus.DRAFT

    def __post_init__(self) -> None:
        for field in (
            "plan_id",
            "project_id",
            "material_snapshot_id",
            "analysis_snapshot_id",
            "system_skill_release",
        ):
            require_identifier(getattr(self, field), field)
        if not isinstance(self.status, PlanStatus):
            raise ValueError("plan status is invalid")
        if (
            isinstance(self.revision, bool)
            or not isinstance(self.revision, int)
            or self.revision < 1
        ):
            raise ValueError("plan revision must start at one")
        _bounded_text(self.title, "plan title", 160)
        _bounded_text(self.summary, "plan summary", 2_000)
        sections = tuple(self.sections)
        if not sections or len(sections) > 256:
            raise ValueError("plan sections are invalid")
        if any(not isinstance(section, PlanSection) for section in sections):
            raise ValueError("plan contains an invalid section")
        identifiers = [section.section_id for section in sections]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("plan section IDs must be unique")
        object.__setattr__(self, "sections", sections)
        if self.content_hash != plan_content_hash(self.title, self.summary, sections):
            raise ValueError("content_hash does not match plan content")

    @classmethod
    def create(
        cls,
        *,
        plan_id: str,
        project_id: str,
        revision: int,
        material_snapshot_id: str,
        analysis_snapshot_id: str,
        system_skill_release: str,
        title: str,
        summary: str,
        sections: tuple[PlanSection, ...],
        status: PlanStatus = PlanStatus.DRAFT,
    ) -> "PlanRevision":
        normalized_sections = tuple(sections)
        return cls(
            plan_id,
            project_id,
            revision,
            plan_content_hash(title, summary, normalized_sections),
            material_snapshot_id,
            analysis_snapshot_id,
            system_skill_release,
            title,
            summary,
            normalized_sections,
            status,
        )

    def transition(self, target: PlanStatus) -> "PlanRevision":
        if not isinstance(target, PlanStatus):
            raise InvalidTransition("plan target status is invalid")
        if target not in _PLAN_TRANSITIONS[self.status]:
            raise InvalidTransition(f"plan cannot move from {self.status} to {target}")
        return replace(self, status=target)

    @property
    def can_execute(self) -> bool:
        return self.status is PlanStatus.CONFIRMED

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "plan_id": self.plan_id,
            "project_id": self.project_id,
            "revision": self.revision,
            "status": self.status.value,
            "content_hash": self.content_hash,
            "material_snapshot_id": self.material_snapshot_id,
            "analysis_snapshot_id": self.analysis_snapshot_id,
            "system_skill_release": self.system_skill_release,
            "title": self.title,
            "summary": self.summary,
            "sections": [section.to_payload() for section in self.sections],
        }
