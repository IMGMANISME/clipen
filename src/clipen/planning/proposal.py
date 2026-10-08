"""Resolve untrusted provider proposals against Controller-owned candidates."""

from __future__ import annotations

from collections.abc import Mapping

from clipen.domain import PlanRevision, PlanSection, PlanSourceRange, PlanStatus

from .models import PlanningContext


class PlanProposalError(ValueError):
    """A schema-valid proposal still violates local candidate constraints."""


def resolve_plan_proposal(
    payload: Mapping[str, object],
    context: PlanningContext,
    *,
    plan_id: str,
    revision: int,
    system_skill_release: str,
) -> PlanRevision:
    """Bind candidate IDs to exact spans without trusting provider timestamps."""

    try:
        if set(payload) != {"schema_version", "title", "summary", "sections"}:
            raise TypeError("proposal fields are invalid")
        if payload["schema_version"] != 1:
            raise TypeError("proposal schema version is invalid")
        title = payload["title"]
        summary = payload["summary"]
        raw_sections = payload["sections"]
        if not isinstance(title, str) or not isinstance(summary, str):
            raise TypeError("proposal text is invalid")
        if not isinstance(raw_sections, list):
            raise TypeError("proposal sections are invalid")
        available = context.candidate_map()
        used: set[str] = set()
        sections: list[PlanSection] = []
        for raw in raw_sections:
            if not isinstance(raw, dict) or set(raw) != {
                "section_id",
                "title",
                "purpose",
                "candidate_ids",
                "edit_notes",
                "unknowns",
            }:
                raise TypeError("proposal section is invalid")
            candidate_ids = raw["candidate_ids"]
            unknowns = raw["unknowns"]
            if not isinstance(candidate_ids, list) or not isinstance(unknowns, list):
                raise TypeError("proposal section lists are invalid")
            for field in ("section_id", "title", "purpose", "edit_notes"):
                if not isinstance(raw[field], str):
                    raise TypeError("proposal section text is invalid")
            if any(not isinstance(value, str) for value in unknowns):
                raise TypeError("proposal section unknown is invalid")
            ranges = []
            for candidate_id in candidate_ids:
                if not isinstance(candidate_id, str) or candidate_id not in available:
                    raise PlanProposalError("proposal references an unknown candidate")
                if candidate_id in used:
                    raise PlanProposalError("proposal reuses a candidate")
                used.add(candidate_id)
                ranges.append(PlanSourceRange(candidate_id, available[candidate_id].source))
            sections.append(
                PlanSection(
                    raw["section_id"],
                    raw["title"],
                    raw["purpose"],
                    tuple(ranges),
                    raw["edit_notes"],
                    tuple(unknowns),
                )
            )
        return PlanRevision.create(
            plan_id=plan_id,
            project_id=context.project_id,
            revision=revision,
            material_snapshot_id=context.material_snapshot_id,
            analysis_snapshot_id=context.analysis_snapshot_id,
            system_skill_release=system_skill_release,
            title=title,
            summary=summary,
            sections=tuple(sections),
            status=PlanStatus.READY_FOR_REVIEW,
        )
    except PlanProposalError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise PlanProposalError("proposal content is invalid") from error
