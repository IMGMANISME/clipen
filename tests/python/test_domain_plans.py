import json
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator

from clipen.domain import (
    InvalidTransition,
    PlanRevision,
    PlanSection,
    PlanSourceRange,
    PlanStatus,
    Rational,
    SourceSpan,
)


class PlanRevisionTests(unittest.TestCase):
    @staticmethod
    def source(candidate_id: str = "candidate-1") -> PlanSourceRange:
        return PlanSourceRange(
            candidate_id,
            SourceSpan("av_" + "a" * 32, 0, "0", "90000", Rational(1, 90000)),
        )

    def plan(self) -> PlanRevision:
        return PlanRevision.create(
            plan_id="plan-1",
            project_id="project-1",
            revision=1,
            material_snapshot_id="materials-1",
            analysis_snapshot_id="analysis-1",
            system_skill_release="skills-1",
            title="東京旅行初剪",
            summary="依事件順序整理旅程，保留現場感。",
            sections=(
                PlanSection(
                    "section-1",
                    "抵達",
                    "建立地點與旅程起點。",
                    (self.source(),),
                    "以環境聲銜接。",
                    ("確切地點仍需確認",),
                ),
            ),
        )

    def test_confirmed_revision_is_the_only_executable_state(self) -> None:
        ready = self.plan().transition(PlanStatus.READY_FOR_REVIEW)
        confirmed = ready.transition(PlanStatus.CONFIRMED)
        self.assertFalse(ready.can_execute)
        self.assertTrue(confirmed.can_execute)
        self.assertEqual(self.plan().status, PlanStatus.DRAFT)

    def test_accepted_revision_is_terminal(self) -> None:
        plan = self.plan()
        for status in (
            PlanStatus.READY_FOR_REVIEW,
            PlanStatus.CONFIRMED,
            PlanStatus.EXECUTING,
            PlanStatus.REVIEWED,
            PlanStatus.ACCEPTED,
        ):
            plan = plan.transition(status)
        with self.assertRaises(InvalidTransition):
            plan.transition(PlanStatus.SUPERSEDED)

    def test_hash_is_derived_from_editable_content(self) -> None:
        plan = self.plan()
        changed = PlanRevision.create(
            plan_id=plan.plan_id,
            project_id=plan.project_id,
            revision=2,
            material_snapshot_id=plan.material_snapshot_id,
            analysis_snapshot_id=plan.analysis_snapshot_id,
            system_skill_release=plan.system_skill_release,
            title=plan.title,
            summary="改為較短的旅程摘要。",
            sections=plan.sections,
        )
        self.assertNotEqual(plan.content_hash, changed.content_hash)
        with self.assertRaises(ValueError):
            PlanRevision(
                plan.plan_id,
                plan.project_id,
                plan.revision,
                "0" * 64,
                plan.material_snapshot_id,
                plan.analysis_snapshot_id,
                plan.system_skill_release,
                plan.title,
                plan.summary,
                plan.sections,
            )

    def test_plan_payload_matches_public_schema(self) -> None:
        schema = json.loads(
            Path("schemas/v1/plan-revision.schema.json").read_text(encoding="utf-8")
        )
        Draft202012Validator(schema).validate(self.plan().to_payload())

    def test_plan_payload_round_trips_and_rejects_corruption(self) -> None:
        plan = self.plan().transition(PlanStatus.READY_FOR_REVIEW)
        self.assertEqual(PlanRevision.from_payload(plan.to_payload()), plan)

        corrupted = plan.to_payload()
        corrupted["summary"] = "內容已被修改"
        with self.assertRaises(ValueError):
            PlanRevision.from_payload(corrupted)

        wrong_type = plan.to_payload()
        wrong_type["sections"][0]["unknowns"] = [42]
        with self.assertRaises(ValueError):
            PlanRevision.from_payload(wrong_type)

    def test_rejects_empty_duplicate_or_invalid_content(self) -> None:
        with self.assertRaises(ValueError):
            PlanSection("section-empty", "空白", "沒有來源", ())
        with self.assertRaises(ValueError):
            PlanSection(
                "section-duplicate",
                "重複",
                "候選不可重複",
                (self.source(), self.source()),
            )
        plan = self.plan()
        with self.assertRaises(ValueError):
            PlanRevision.create(
                plan_id=plan.plan_id,
                project_id=plan.project_id,
                revision=True,
                material_snapshot_id=plan.material_snapshot_id,
                analysis_snapshot_id=plan.analysis_snapshot_id,
                system_skill_release=plan.system_skill_release,
                title=plan.title,
                summary=plan.summary,
                sections=plan.sections,
            )


if __name__ == "__main__":
    unittest.main()
