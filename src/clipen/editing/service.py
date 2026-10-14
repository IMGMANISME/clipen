"""Application service for building and validating the current edit timeline."""

from __future__ import annotations

import hashlib

from clipen.domain import (
    CapabilityProfile,
    PlanStatus,
    TimelineReferences,
    TimelineValidator,
)
from clipen.planning import PlanningRepository
from clipen.projects import ProjectError, TimelineValidationRepository

from .compiler import OutputSettings, TimelineCompiler
from .storage import TimelineRecord, TimelineRepository


class TimelineService:
    """Turn one confirmed Plan revision into one immutable executable timeline."""

    def __init__(
        self,
        timelines: TimelineRepository,
        planning: PlanningRepository,
        validations: TimelineValidationRepository,
        capability_profile: CapabilityProfile,
        *,
        compiler: TimelineCompiler | None = None,
        validator: TimelineValidator | None = None,
    ) -> None:
        self.timelines = timelines
        self.planning = planning
        self.validations = validations
        self.capability_profile = capability_profile
        self.compiler = compiler or TimelineCompiler()
        self.validator = validator or TimelineValidator()

    def build(
        self,
        project_id: str,
        request_id: object,
        expected_plan_id: object,
        expected_plan_revision: object,
        output_preset: object,
        frame_rate: object,
    ) -> dict[str, object]:
        plan, context = self.planning.execution_input(project_id)
        if (
            not isinstance(expected_plan_id, str)
            or isinstance(expected_plan_revision, bool)
            or not isinstance(expected_plan_revision, int)
            or plan.plan_id != expected_plan_id
            or plan.revision != expected_plan_revision
        ):
            raise ProjectError(
                "plan_revision_conflict",
                "剪輯計畫已更新，請重新整理後再建立剪輯版本。",
                status_code=409,
            )
        try:
            settings = OutputSettings(output_preset, frame_rate)
            timeline = self.compiler.compile(plan, context, settings)
        except (TypeError, ValueError) as error:
            raise ProjectError(
                "timeline_content_invalid",
                "無法從這份剪輯計畫建立有效的剪輯版本。",
                status_code=422,
            ) from error

        record, applied = self.timelines.save(
            project_id,
            request_id,
            plan.plan_id,
            plan.revision,
            settings,
            timeline,
        )
        if not applied:
            current = self.timelines.current(project_id)
            if (
                current is None
                or current.timeline.timeline_id != record.timeline.timeline_id
            ):
                return self.status(project_id)
        references = TimelineReferences(
            project_id,
            plan,
            frozenset(candidate.source.version_id for candidate in context.candidates),
        )
        report_identity = (
            f"{timeline.timeline_id}:{self.capability_profile.content_hash}"
        ).encode("utf-8")
        report_id = "validation_" + hashlib.sha256(report_identity).hexdigest()[:32]
        report = self.validator.validate(
            report_id,
            timeline,
            self.capability_profile,
            references,
        )
        self.validations.publish(project_id, report)
        return self._public_state(project_id, record)

    def status(self, project_id: str) -> dict[str, object]:
        record = self.timelines.current(project_id)
        if record is None:
            return {
                "state": "not_ready",
                "can_execute": False,
                "timeline": None,
                "validation": self.validations.get_state(project_id).to_public(),
            }
        return self._public_state(project_id, record)

    def _public_state(
        self, project_id: str, record: TimelineRecord
    ) -> dict[str, object]:
        plan = self.planning.current_plan(project_id)
        timeline = record.timeline
        if (
            plan is None
            or plan.status is not PlanStatus.CONFIRMED
            or plan.plan_id != timeline.plan_id
            or plan.revision != timeline.plan_revision
            or plan.content_hash != timeline.plan_content_hash
        ):
            return {
                "state": "stale",
                "can_execute": False,
                "timeline": record.to_public(),
                "validation": None,
            }
        validation = self.validations.get_state(project_id).to_public()
        report = validation.get("report")
        if (
            not isinstance(report, dict)
            or report.get("timeline_id") != timeline.timeline_id
        ):
            return {
                "state": "validation_required",
                "can_execute": False,
                "timeline": record.to_public(),
                "validation": None,
            }
        return {
            "state": validation["state"],
            "can_execute": validation["can_execute"],
            "timeline": record.to_public(),
            "validation": validation,
        }
