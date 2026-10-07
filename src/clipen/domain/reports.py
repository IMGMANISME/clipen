"""Stable, machine-readable validation findings for UI and operation gates."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re

from .validation import require_identifier


_RULE_ID = re.compile(r"[a-z][a-z0-9_.-]{0,127}\Z")


class ValidationSeverity(str, Enum):
    WARNING = "warning"
    BLOCKING = "blocking"


class ValidationStatus(str, Enum):
    PASS = "pass"
    WARNING = "warning"
    BLOCKING = "blocking"


def _bounded_text(value: object, field: str, maximum: int) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\x00" in value
        or len(value) > maximum
    ):
        raise ValueError(f"{field} is invalid")


@dataclass(frozen=True)
class ValidationIssue:
    rule_id: str
    severity: ValidationSeverity
    location: str
    message: str
    suggestion: str
    item_id: str | None = None
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.rule_id, str) or _RULE_ID.fullmatch(self.rule_id) is None:
            raise ValueError("validation rule ID is invalid")
        if not isinstance(self.severity, ValidationSeverity):
            raise ValueError("validation severity is invalid")
        if (
            not isinstance(self.location, str)
            or (self.location and not self.location.startswith("/"))
            or "\x00" in self.location
            or len(self.location) > 1_000
        ):
            raise ValueError("validation location is invalid")
        _bounded_text(self.message, "validation message", 2_000)
        _bounded_text(self.suggestion, "validation suggestion", 2_000)
        if self.item_id is not None:
            require_identifier(self.item_id, "item_id")
        references = tuple(self.evidence_refs)
        if len(references) > 128:
            raise ValueError("validation issue has too many evidence references")
        for reference in references:
            require_identifier(reference, "evidence_ref")
        if len(references) != len(set(references)):
            raise ValueError("validation evidence references must be unique")
        object.__setattr__(self, "evidence_refs", references)

    def to_payload(self) -> dict[str, object]:
        return {
            "rule_id": self.rule_id,
            "severity": self.severity.value,
            "location": self.location,
            "item_id": self.item_id,
            "message": self.message,
            "suggestion": self.suggestion,
            "evidence_refs": list(self.evidence_refs),
        }


@dataclass(frozen=True)
class ValidationReport:
    report_id: str
    timeline_id: str
    capability_profile_id: str
    capability_content_hash: str
    issues: tuple[ValidationIssue, ...] = ()

    def __post_init__(self) -> None:
        for field in ("report_id", "timeline_id", "capability_profile_id"):
            require_identifier(getattr(self, field), field)
        if (
            not isinstance(self.capability_content_hash, str)
            or re.fullmatch(r"[0-9a-f]{64}", self.capability_content_hash) is None
        ):
            raise ValueError("capability content hash is invalid")
        issues = tuple(self.issues)
        if len(issues) > 100_000 or any(
            not isinstance(issue, ValidationIssue) for issue in issues
        ):
            raise ValueError("validation issues are invalid")
        keys = {(issue.rule_id, issue.location, issue.item_id) for issue in issues}
        if len(keys) != len(issues):
            raise ValueError("validation issues must have unique locations")
        object.__setattr__(self, "issues", issues)

    @property
    def status(self) -> ValidationStatus:
        if any(issue.severity is ValidationSeverity.BLOCKING for issue in self.issues):
            return ValidationStatus.BLOCKING
        if self.issues:
            return ValidationStatus.WARNING
        return ValidationStatus.PASS

    @property
    def can_execute(self) -> bool:
        # Warnings require an explicit, separately persisted user acceptance.
        return self.status is ValidationStatus.PASS

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "report_id": self.report_id,
            "timeline_id": self.timeline_id,
            "capability_profile_id": self.capability_profile_id,
            "capability_content_hash": self.capability_content_hash,
            "status": self.status.value,
            "issues": [issue.to_payload() for issue in self.issues],
        }
