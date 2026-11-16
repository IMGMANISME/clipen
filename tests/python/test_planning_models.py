import unittest

from clipen.domain import Rational, SourceSpan
from clipen.planning import (
    PlanningCandidate,
    PlanningContext,
    TranscriptReliability,
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
                            "effects": [{"effect": "punch_in", "intensity": "subtle"}],
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


if __name__ == "__main__":
    unittest.main()
