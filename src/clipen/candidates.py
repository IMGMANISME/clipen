"""Source candidates the editing tools read: path-free, bounded and with transcript reliability."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum

from clipen.domain import SourceSpan
from clipen.domain.validation import require_identifier

_MAX_CANDIDATES = 256
_MAX_CONTEXT_BYTES = 128 * 1024


class TranscriptReliability(str, Enum):
    TRUSTED = "trusted"
    NEEDS_REVIEW = "needs_review"
    UNAVAILABLE = "unavailable"


def _bounded_text(value: object, field: str, maximum: int, *, empty: bool = False) -> None:
    if not isinstance(value, str) or "\x00" in value or len(value) > maximum or (not empty and not value.strip()):
        raise ValueError(f"{field} is invalid")


@dataclass(frozen=True)
class SourceCandidate:
    candidate_id: str
    source: SourceSpan
    display_name: str
    media_kind: str
    evidence_summary: str
    transcript_reliability: TranscriptReliability
    transcript_excerpt: str | None = None

    def __post_init__(self) -> None:
        require_identifier(self.candidate_id, "candidate_id")
        if not isinstance(self.source, SourceSpan):
            raise ValueError("source candidate source is invalid")
        _bounded_text(self.display_name, "candidate display name", 255)
        if self.media_kind not in {"video", "audio"}:
            raise ValueError("candidate media kind is invalid")
        _bounded_text(self.evidence_summary, "candidate evidence summary", 2_000, empty=True)
        if not isinstance(self.transcript_reliability, TranscriptReliability):
            raise ValueError("candidate transcript reliability is invalid")
        if self.transcript_excerpt is not None:
            _bounded_text(self.transcript_excerpt, "candidate transcript excerpt", 500)
        if self.transcript_reliability is not TranscriptReliability.TRUSTED and self.transcript_excerpt is not None:
            raise ValueError("untrusted transcript text cannot enter provider context")

    def to_provider(self) -> dict[str, object]:
        duration = self.source.duration_pts * self.source.time_base.numerator
        duration /= self.source.time_base.denominator
        return {
            "candidate_id": self.candidate_id,
            "display_name": self.display_name,
            "media_kind": self.media_kind,
            "duration_seconds": round(duration, 3),
            "evidence_summary": self.evidence_summary,
            "transcript": {
                "reliability": self.transcript_reliability.value,
                "excerpt": self.transcript_excerpt,
            },
        }

    def to_storage(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "source": self.source.to_storage(),
            "display_name": self.display_name,
            "media_kind": self.media_kind,
            "evidence_summary": self.evidence_summary,
            "transcript_reliability": self.transcript_reliability.value,
            "transcript_excerpt": self.transcript_excerpt,
        }

    @classmethod
    def from_storage(cls, value: object) -> "SourceCandidate":
        if not isinstance(value, dict) or set(value) != {
            "candidate_id",
            "source",
            "display_name",
            "media_kind",
            "evidence_summary",
            "transcript_reliability",
            "transcript_excerpt",
        }:
            raise ValueError("stored source candidate is invalid")
        excerpt = value["transcript_excerpt"]
        if excerpt is not None and not isinstance(excerpt, str):
            raise ValueError("stored source transcript excerpt is invalid")
        for field in (
            "candidate_id",
            "display_name",
            "media_kind",
            "evidence_summary",
            "transcript_reliability",
        ):
            if not isinstance(value[field], str):
                raise ValueError(f"stored source candidate {field} is invalid")
        return cls(
            value["candidate_id"],
            SourceSpan.from_storage(value["source"]),
            value["display_name"],
            value["media_kind"],
            value["evidence_summary"],
            TranscriptReliability(value["transcript_reliability"]),
            excerpt,
        )


@dataclass(frozen=True)
class SourceContext:
    project_id: str
    brief: str
    material_snapshot_id: str
    analysis_snapshot_id: str
    candidates: tuple[SourceCandidate, ...]
    reference_notes: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field in ("project_id", "material_snapshot_id", "analysis_snapshot_id"):
            require_identifier(getattr(self, field), field)
        _bounded_text(self.brief, "source brief", 8_000)
        candidates = tuple(self.candidates)
        reference_notes = tuple(self.reference_notes)
        limitations = tuple(self.limitations)
        if not candidates or len(candidates) > _MAX_CANDIDATES:
            raise ValueError("source candidates are empty or exceed the bound")
        if any(not isinstance(candidate, SourceCandidate) for candidate in candidates):
            raise ValueError("source context contains an invalid candidate")
        identifiers = [candidate.candidate_id for candidate in candidates]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("source candidate IDs must be unique")
        if len(limitations) > 128:
            raise ValueError("source context has too many limitations")
        if len(reference_notes) > 32:
            raise ValueError("source context has too many reference notes")
        for note in reference_notes:
            _bounded_text(note, "planning reference note", 1_000)
        for limitation in limitations:
            _bounded_text(limitation, "planning limitation", 500)
        object.__setattr__(self, "candidates", candidates)
        object.__setattr__(self, "reference_notes", reference_notes)
        object.__setattr__(self, "limitations", limitations)
        if len(self.encoded()) > _MAX_CONTEXT_BYTES:
            raise ValueError("source context exceeds the provider input bound")

    def to_provider(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "brief": self.brief,
            "candidates": [candidate.to_provider() for candidate in self.candidates],
            "reference_notes": list(self.reference_notes),
            "limitations": list(self.limitations),
        }

    def encoded(self) -> bytes:
        return json.dumps(
            self.to_provider(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def to_storage(self) -> dict[str, object]:
        return {
            "project_id": self.project_id,
            "brief": self.brief,
            "material_snapshot_id": self.material_snapshot_id,
            "analysis_snapshot_id": self.analysis_snapshot_id,
            "candidates": [candidate.to_storage() for candidate in self.candidates],
            "reference_notes": list(self.reference_notes),
            "limitations": list(self.limitations),
        }

    @classmethod
    def from_storage(cls, value: object) -> "SourceContext":
        if not isinstance(value, dict) or set(value) != {
            "project_id",
            "brief",
            "material_snapshot_id",
            "analysis_snapshot_id",
            "candidates",
            "reference_notes",
            "limitations",
        }:
            raise ValueError("stored source context is invalid")
        candidates = value["candidates"]
        notes = value["reference_notes"]
        limitations = value["limitations"]
        if not isinstance(candidates, list) or not isinstance(notes, list) or not isinstance(limitations, list):
            raise ValueError("stored source context collections are invalid")
        for field in (
            "project_id",
            "brief",
            "material_snapshot_id",
            "analysis_snapshot_id",
        ):
            if not isinstance(value[field], str):
                raise ValueError(f"stored source context {field} is invalid")
        if any(not isinstance(item, str) for item in (*notes, *limitations)):
            raise ValueError("stored source context text is invalid")
        return cls(
            value["project_id"],
            value["brief"],
            value["material_snapshot_id"],
            value["analysis_snapshot_id"],
            tuple(SourceCandidate.from_storage(item) for item in candidates),
            tuple(notes),
            tuple(limitations),
        )

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.encoded()).hexdigest()

    def candidate_map(self) -> dict[str, SourceCandidate]:
        return {candidate.candidate_id: candidate for candidate in self.candidates}
