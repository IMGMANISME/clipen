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
    assess_plan_quality,
    requested_duration_seconds,
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

    @staticmethod
    def blueprint_proposal() -> dict[str, object]:
        return {
            "schema_version": 2,
            "title": "東京旅行 Vlog",
            "summary": "以快節奏開場，再依旅程順序建立故事。",
            "creative_direction": {
                "target_duration_seconds": 600,
                "pacing": "energetic",
                "subtitle_mode": "dynamic",
                "graphics_intensity": "balanced",
                "music": {
                    "mode": "approved_asset_required",
                    "mood": "輕快、日系旅行感",
                },
                "sound_design": "對話時降低配樂，場景切換保留環境聲。",
            },
            "sections": [
                {
                    "section_id": "section-arrival",
                    "title": "抵達",
                    "purpose": "交代旅程起點。",
                    "clips": [
                        {
                            "candidate_id": "candidate-1",
                            "role": "hook",
                            "audio_treatment": "mixed",
                            "effects": [
                                {"effect": "punch_in", "intensity": "subtle"}
                            ],
                        },
                        {
                            "candidate_id": "candidate-2",
                            "role": "story",
                            "audio_treatment": "ambient",
                            "effects": [],
                        },
                    ],
                    "title_cues": [
                        {
                            "cue_id": "title-arrival",
                            "text": "東京旅行 Vlog",
                            "placement": "section_start",
                            "style": "chapter",
                        }
                    ],
                    "sound_cues": [
                        {
                            "cue_id": "sound-arrival",
                            "sound": "whoosh",
                            "placement": "section_start",
                            "intensity": "subtle",
                        }
                    ],
                    "transition_out": "cut",
                    "edit_notes": "先快後慢。",
                    "unknowns": [],
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
        patterns: list[str] = []

        def collect_patterns(value: object) -> None:
            if isinstance(value, dict):
                pattern = value.get("pattern")
                if isinstance(pattern, str):
                    patterns.append(pattern)
                for child in value.values():
                    collect_patterns(child)
            elif isinstance(value, list):
                for child in value:
                    collect_patterns(child)

        collect_patterns(schema)
        self.assertTrue(patterns)
        self.assertTrue(all("(?" not in pattern for pattern in patterns))
        self.assertNotIn("uniqueItems", json.dumps(schema, sort_keys=True))
        invalid = self.proposal()
        invalid["unexpected"] = True
        self.assertTrue(list(Draft202012Validator(schema).iter_errors(invalid)))

    def test_v2_schema_and_resolver_preserve_structured_production_intent(self) -> None:
        schema = json.loads(
            Path("schemas/v2/plan-proposal.schema.json").read_text(encoding="utf-8")
        )
        proposal = self.blueprint_proposal()
        Draft202012Validator(schema).validate(proposal)
        plan = resolve_plan_proposal(
            proposal,
            self.context(),
            plan_id="plan-v2",
            revision=1,
            system_skill_release="skills-1",
        )
        self.assertIsNotNone(plan.blueprint)
        self.assertEqual(plan.blueprint.target_duration_seconds, 600)
        self.assertEqual(plan.blueprint.sections[0].clips[0].role.value, "hook")
        self.assertEqual(plan.blueprint.sections[0].title_cues[0].text, "東京旅行 Vlog")
        self.assertEqual(plan.blueprint.sections[0].sound_cues[0].sound.value, "whoosh")
        self.assertEqual(
            [item.candidate_id for item in plan.sections[0].source_ranges],
            ["candidate-1", "candidate-2"],
        )

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

    def test_quality_reads_explicit_chinese_runtime_and_flags_short_video(self) -> None:
        self.assertEqual(requested_duration_seconds("總時長約莫十分鐘"), 600)
        self.assertIsNone(requested_duration_seconds("每 10 分鐘休息一次"))
        context = PlanningContext(
            "prj_" + "1" * 32,
            "依旅程順序剪成約 10 分鐘的自然 Vlog。",
            "inventory-1",
            "analysis-1",
            (self.candidate(1), self.candidate(2)),
        )
        plan = resolve_plan_proposal(
            self.proposal(),
            context,
            plan_id="plan-1",
            revision=1,
            system_skill_release="skills-1",
        )
        quality = assess_plan_quality(plan, context)
        self.assertEqual(quality.target_duration_seconds, 600)
        self.assertEqual(quality.selected_video_duration_seconds, 4)
        self.assertEqual(quality.issue_code, "plan_video_coverage_too_short")

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
