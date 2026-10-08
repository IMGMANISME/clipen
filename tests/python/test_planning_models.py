import json
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator

from clipen.domain import PlanStatus, Rational, SourceSpan
from clipen.planning import (
    PlanProposalError,
    PlanningCandidate,
    PlanningContext,
    TranscriptReliability,
    resolve_plan_proposal,
)


class PlanningModelsTests(unittest.TestCase):
    @staticmethod
    def candidate(
        index: int = 1,
        *,
        reliability: TranscriptReliability = TranscriptReliability.TRUSTED,
        excerpt: str | None = "我們抵達東京。",
    ) -> PlanningCandidate:
        return PlanningCandidate(
            f"candidate-{index}",
            SourceSpan(
                "av_" + f"{index:032x}",
                0,
                "0",
                "180000",
                Rational(1, 90000),
            ),
            f"DJI_{index:04d}.MP4",
            "video",
            "戶外移動畫面；確切地點未知。",
            reliability,
            excerpt,
        )

    def context(self) -> PlanningContext:
        return PlanningContext(
            "prj_" + "1" * 32,
            "依旅程順序剪成自然的敘事 Vlog。",
            "inventory-1",
            "analysis-1",
            (self.candidate(1), self.candidate(2)),
            ("只有使用者明確同意過的視覺觀察可供規劃。",),
        )

    @staticmethod
    def proposal() -> dict[str, object]:
        return {
            "schema_version": 1,
            "title": "東京旅行初剪",
            "summary": "依事件順序建立旅程脈絡。",
            "sections": [
                {
                    "section_id": "section-arrival",
                    "title": "抵達",
                    "purpose": "交代旅程起點。",
                    "candidate_ids": ["candidate-1", "candidate-2"],
                    "edit_notes": "保留環境聲。",
                    "unknowns": ["確切地點需要確認"],
                }
            ],
        }

    def test_context_is_path_free_bounded_and_has_conservative_estimate(self) -> None:
        context = self.context()
        payload = context.to_provider()
        encoded = context.encoded()
        self.assertNotIn("version_id", repr(payload))
        self.assertNotIn("start_pts", repr(payload))
        self.assertNotIn("/Volumes/", repr(payload))
        self.assertEqual(context.estimated_input_tokens, (len(encoded) + 2) // 3)
        self.assertEqual(len(context.content_hash), 64)

    def test_untrusted_transcript_text_cannot_enter_context(self) -> None:
        with self.assertRaises(ValueError):
            self.candidate(
                reliability=TranscriptReliability.NEEDS_REVIEW,
                excerpt="可能聽錯的內容",
            )
        candidate = self.candidate(
            reliability=TranscriptReliability.NEEDS_REVIEW,
            excerpt=None,
        )
        self.assertIsNone(candidate.to_provider()["transcript"]["excerpt"])

    def test_provider_proposal_schema_is_bounded(self) -> None:
        schema = json.loads(
            Path("schemas/v1/plan-proposal.schema.json").read_text(encoding="utf-8")
        )
        Draft202012Validator(schema).validate(self.proposal())
        invalid = self.proposal()
        invalid["unexpected"] = True
        self.assertTrue(list(Draft202012Validator(schema).iter_errors(invalid)))

    def test_resolver_uses_local_spans_and_creates_reviewable_plan(self) -> None:
        context = self.context()
        plan = resolve_plan_proposal(
            self.proposal(),
            context,
            plan_id="plan-1",
            revision=1,
            system_skill_release="skills-1",
        )
        self.assertEqual(plan.status, PlanStatus.READY_FOR_REVIEW)
        self.assertEqual(
            plan.sections[0].source_ranges[0].source,
            context.candidates[0].source,
        )
        self.assertEqual(plan.material_snapshot_id, "inventory-1")

    def test_resolver_rejects_unknown_or_reused_candidates(self) -> None:
        unknown = self.proposal()
        unknown["sections"][0]["candidate_ids"] = ["candidate-missing"]
        with self.assertRaises(PlanProposalError):
            resolve_plan_proposal(
                unknown,
                self.context(),
                plan_id="plan-1",
                revision=1,
                system_skill_release="skills-1",
            )

        reused = self.proposal()
        reused["sections"].append(
            {
                "section_id": "section-repeat",
                "title": "重複",
                "purpose": "不應重複使用來源。",
                "candidate_ids": ["candidate-1"],
                "edit_notes": "",
                "unknowns": [],
            }
        )
        with self.assertRaises(PlanProposalError):
            resolve_plan_proposal(
                reused,
                self.context(),
                plan_id="plan-1",
                revision=1,
                system_skill_release="skills-1",
            )


if __name__ == "__main__":
    unittest.main()
