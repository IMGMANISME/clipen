import json
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator

from clipen.domain import (
    AudioTreatment,
    BlueprintClip,
    BlueprintEffect,
    BlueprintPacing,
    BlueprintSection,
    BlueprintSoundCue,
    BlueprintTitleCue,
    ClipRole,
    EditBlueprint,
    EffectIntensity,
    EffectKind,
    GraphicsIntensity,
    MusicMode,
    PlanRevision,
    PlanSection,
    PlanSourceRange,
    Rational,
    SourceSpan,
    SoundKind,
    SoundPlacement,
    SubtitleMode,
    TitlePlacement,
    TitleStyle,
    TransitionKind,
)


class EditBlueprintTests(unittest.TestCase):
    @staticmethod
    def blueprint() -> EditBlueprint:
        return EditBlueprint(
            600,
            BlueprintPacing.ENERGETIC,
            SubtitleMode.DYNAMIC,
            GraphicsIntensity.BALANCED,
            MusicMode.APPROVED_ASSET_REQUIRED,
            "輕快、日系旅行感",
            "對話時降低配樂，場景切換保留環境聲。",
            (
                BlueprintSection(
                    "section-opening",
                    (
                        BlueprintClip(
                            "candidate-001",
                            ClipRole.HOOK,
                            AudioTreatment.MIXED,
                            (
                                BlueprintEffect(
                                    EffectKind.PUNCH_IN,
                                    EffectIntensity.SUBTLE,
                                ),
                            ),
                        ),
                    ),
                    (
                        BlueprintTitleCue(
                            "title-opening",
                            "東京旅行 Vlog",
                            TitlePlacement.SECTION_START,
                            TitleStyle.CHAPTER,
                        ),
                    ),
                    (
                        BlueprintSoundCue(
                            "sound-opening",
                            SoundKind.WHOOSH,
                            SoundPlacement.SECTION_START,
                            EffectIntensity.SUBTLE,
                        ),
                    ),
                    TransitionKind.CUT,
                ),
            ),
        )

    def test_payload_round_trips_without_provider_or_paths(self) -> None:
        blueprint = self.blueprint()
        self.assertEqual(EditBlueprint.from_payload(blueprint.to_payload()), blueprint)
        self.assertNotIn("/Volumes/", repr(blueprint.to_payload()))
        self.assertNotIn("version_id", repr(blueprint.to_payload()))

    def test_plan_hash_includes_blueprint_and_v1_remains_readable(self) -> None:
        source = PlanSourceRange(
            "candidate-001",
            SourceSpan("av_" + "a" * 32, 0, "0", "90000", Rational(1, 90000)),
        )
        section = PlanSection(
            "section-opening",
            "開場",
            "快速建立旅行期待。",
            (source,),
        )
        legacy = PlanRevision.create(
            plan_id="plan-legacy",
            project_id="project-1",
            revision=1,
            material_snapshot_id="materials-1",
            analysis_snapshot_id="analysis-1",
            system_skill_release="skills-1",
            title="東京旅行",
            summary="先建立旅程順序。",
            sections=(section,),
        )
        current = PlanRevision.create(
            plan_id="plan-current",
            project_id="project-1",
            revision=1,
            material_snapshot_id="materials-1",
            analysis_snapshot_id="analysis-1",
            system_skill_release="skills-1",
            title="東京旅行",
            summary="先建立旅程順序。",
            sections=(section,),
            blueprint=self.blueprint(),
        )
        self.assertEqual(legacy.to_payload()["schema_version"], 1)
        self.assertEqual(current.to_payload()["schema_version"], 2)
        schema = json.loads(
            Path("schemas/v2/plan-revision.schema.json").read_text(encoding="utf-8")
        )
        Draft202012Validator(schema).validate(current.to_payload())
        self.assertNotEqual(legacy.content_hash, current.content_hash)
        self.assertEqual(PlanRevision.from_payload(legacy.to_payload()), legacy)
        self.assertEqual(PlanRevision.from_payload(current.to_payload()), current)

    def test_plan_rejects_blueprint_that_drifts_from_selected_clips(self) -> None:
        blueprint = self.blueprint()
        section = PlanSection(
            "section-opening",
            "開場",
            "快速建立旅行期待。",
            (
                PlanSourceRange(
                    "candidate-other",
                    SourceSpan(
                        "av_" + "b" * 32,
                        0,
                        "0",
                        "90000",
                        Rational(1, 90000),
                    ),
                ),
            ),
        )
        with self.assertRaises(ValueError):
            PlanRevision.create(
                plan_id="plan-invalid",
                project_id="project-1",
                revision=1,
                material_snapshot_id="materials-1",
                analysis_snapshot_id="analysis-1",
                system_skill_release="skills-1",
                title="東京旅行",
                summary="測試不一致。",
                sections=(section,),
                blueprint=blueprint,
            )

    def test_rejects_duplicate_effects_and_unresolved_music_without_mood(self) -> None:
        effect = BlueprintEffect(EffectKind.PUNCH_IN, EffectIntensity.SUBTLE)
        with self.assertRaises(ValueError):
            BlueprintClip(
                "candidate-001",
                ClipRole.HOOK,
                AudioTreatment.MIXED,
                (effect, effect),
            )
        with self.assertRaises(ValueError):
            EditBlueprint(
                None,
                BlueprintPacing.BALANCED,
                SubtitleMode.DIALOGUE,
                GraphicsIntensity.MINIMAL,
                MusicMode.APPROVED_ASSET_REQUIRED,
                "",
                "",
                self.blueprint().sections,
            )


if __name__ == "__main__":
    unittest.main()
