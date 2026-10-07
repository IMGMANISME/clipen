import time
import unittest
from unittest.mock import patch

from clipen.domain import (
    CapabilityProfile,
    ClipSource,
    DeliveryMode,
    EditTimeline,
    EffectCapability,
    EffectRequest,
    ElementCapability,
    GraphicClip,
    MediaClip,
    PlanRevision,
    PlanStatus,
    Rational,
    SourceSpan,
    TextClip,
    TimelineReferences,
    TimelineValidator,
    Track,
    TrackKind,
    ValidationStatus,
    VerifiedDelivery,
)


class TimelineValidatorTests(unittest.TestCase):
    def delivery(self, mode: DeliveryMode) -> VerifiedDelivery:
        return VerifiedDelivery(mode, f"compiler-{mode.value}", f"test-{mode.value}")

    def profile(self, *, both: bool = True) -> CapabilityProfile:
        deliveries = (self.delivery(DeliveryMode.MOVIE),)
        if both:
            deliveries += (self.delivery(DeliveryMode.RESOLVE_PROJECT),)
        return CapabilityProfile(
            "test-profile",
            1,
            elements=tuple(
                ElementCapability(kind, deliveries)
                for kind in TrackKind
            ),
            effects=(
                EffectCapability(
                    "clipen.transform",
                    1,
                    (TrackKind.VIDEO,),
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["scale_percent"],
                        "properties": {
                            "scale_percent": {
                                "type": "number",
                                "minimum": 1,
                                "maximum": 400,
                            }
                        },
                    },
                    deliveries,
                ),
            ),
        )

    def plan(self, *, status: PlanStatus = PlanStatus.CONFIRMED) -> PlanRevision:
        return PlanRevision(
            "plan-1",
            "project-1",
            1,
            "a" * 64,
            "materials-1",
            "analysis-1",
            "skills-1",
            status,
        )

    def media(
        self,
        item_id: str,
        kind: TrackKind,
        start_frame: int,
        duration_frames: int,
        *,
        effect: EffectRequest | None = None,
    ) -> Track:
        time_base = Rational(1, 90_000 if kind is TrackKind.VIDEO else 48_000)
        duration_pts = (
            duration_frames * time_base.denominator // 30
        )
        clip = MediaClip(
            item_id,
            start_frame,
            duration_frames,
            ClipSource(
                SourceSpan(
                    "av_" + ("a" if kind is TrackKind.VIDEO else "b") * 32,
                    0 if kind is TrackKind.VIDEO else 1,
                    "0",
                    str(duration_pts),
                    time_base,
                )
            ),
            () if effect is None else (effect,),
        )
        return Track(f"{kind.value}-1", kind, (clip,))

    def timeline(self, *, effect: EffectRequest | None = None) -> EditTimeline:
        return EditTimeline(
            "timeline-1",
            "project-1",
            "plan-1",
            1,
            "a" * 64,
            Rational(30, 1),
            1920,
            1080,
            60,
            (
                self.media("video-clip", TrackKind.VIDEO, 0, 60, effect=effect),
                self.media("audio-clip", TrackKind.AUDIO, 0, 60),
                Track(
                    "title-1",
                    TrackKind.TITLE,
                    (TextClip("title-clip", 0, 30, "東京", "title-default"),),
                ),
                Track(
                    "graphic-1",
                    TrackKind.GRAPHIC,
                    (GraphicClip("graphic-clip", 30, 30, "art_" + "c" * 32),),
                ),
            ),
        )

    def references(self, plan: PlanRevision | None = None) -> TimelineReferences:
        return TimelineReferences(
            "project-1",
            self.plan() if plan is None else plan,
            version_ids=frozenset(("av_" + "a" * 32, "av_" + "b" * 32)),
            artifact_ids=frozenset(("art_" + "c" * 32,)),
            style_ids=frozenset(("title-default",)),
        )

    def validate(
        self,
        timeline: EditTimeline | None = None,
        profile: CapabilityProfile | None = None,
        references: TimelineReferences | None = None,
    ):
        return TimelineValidator().validate(
            "report-1",
            self.timeline() if timeline is None else timeline,
            self.profile() if profile is None else profile,
            self.references() if references is None else references,
        )

    def test_valid_timeline_passes_both_delivery_gate(self) -> None:
        report = self.validate(
            self.timeline(
                effect=EffectRequest("clipen.transform", {"scale_percent": 105})
            )
        )
        self.assertEqual(report.status, ValidationStatus.PASS)
        self.assertTrue(report.can_execute)

    def test_missing_resolve_proof_blocks_every_used_capability(self) -> None:
        report = self.validate(
            self.timeline(
                effect=EffectRequest("clipen.transform", {"scale_percent": 100})
            ),
            profile=self.profile(both=False),
        )
        self.assertEqual(report.status, ValidationStatus.BLOCKING)
        rules = [issue.rule_id for issue in report.issues]
        self.assertEqual(rules.count("timeline.capability.element_unverified"), 4)
        self.assertIn("timeline.capability.effect_delivery_unverified", rules)

    def test_missing_references_and_unconfirmed_plan_are_blocking(self) -> None:
        references = TimelineReferences(
            "project-1",
            self.plan(status=PlanStatus.DRAFT),
        )
        report = self.validate(references=references)
        rules = {issue.rule_id for issue in report.issues}
        self.assertIn("timeline.reference.plan_not_confirmed", rules)
        self.assertIn("timeline.reference.source_missing", rules)
        self.assertIn("timeline.reference.artifact_missing", rules)
        self.assertIn("timeline.reference.style_missing", rules)

    def test_video_gaps_and_missing_audio_require_user_action(self) -> None:
        timeline = self.timeline()
        late_video = self.media("late", TrackKind.VIDEO, 30, 30)
        without_audio = EditTimeline(
            timeline.timeline_id,
            timeline.project_id,
            timeline.plan_id,
            timeline.plan_revision,
            timeline.plan_content_hash,
            timeline.frame_rate,
            timeline.width,
            timeline.height,
            timeline.duration_frames,
            (late_video,),
        )
        report = self.validate(timeline=without_audio)
        rules = {issue.rule_id for issue in report.issues}
        self.assertIn("timeline.content.video_gap", rules)
        self.assertIn("timeline.content.audio_missing", rules)
        self.assertFalse(report.can_execute)

    def test_unknown_wrong_track_and_invalid_effect_parameters_block(self) -> None:
        unknown = self.validate(
            self.timeline(effect=EffectRequest("clipen.unknown", {}))
        )
        self.assertIn(
            "timeline.capability.effect_unknown",
            {issue.rule_id for issue in unknown.issues},
        )

        invalid = self.validate(
            self.timeline(
                effect=EffectRequest("clipen.transform", {"scale_percent": 999})
            )
        )
        self.assertIn(
            "timeline.capability.effect_parameters_invalid",
            {issue.rule_id for issue in invalid.issues},
        )

        base = self.timeline()
        audio_effect = self.media(
            "audio-clip",
            TrackKind.AUDIO,
            0,
            60,
            effect=EffectRequest("clipen.transform", {"scale_percent": 100}),
        )
        wrong_track = EditTimeline(
            base.timeline_id,
            base.project_id,
            base.plan_id,
            base.plan_revision,
            base.plan_content_hash,
            base.frame_rate,
            base.width,
            base.height,
            base.duration_frames,
            (base.tracks[0], audio_effect, *base.tracks[2:]),
        )
        unsupported = self.validate(wrong_track)
        self.assertIn(
            "timeline.capability.effect_track_unsupported",
            {issue.rule_id for issue in unsupported.issues},
        )

    def test_large_timeline_validation_remains_linear_and_bounded(self) -> None:
        version_id = "av_" + "a" * 32
        source = ClipSource(
            SourceSpan(version_id, 0, "0", "3000", Rational(1, 90_000))
        )
        effect = EffectRequest("clipen.transform", {"scale_percent": 100})
        video_items = tuple(
            MediaClip(f"clip-{index}", index, 1, source, (effect,))
            for index in range(5_000)
        )
        audio = MediaClip(
            "audio-full",
            0,
            5_000,
            ClipSource(
                SourceSpan(
                    "av_" + "b" * 32,
                    1,
                    "0",
                    "8000000",
                    Rational(1, 48_000),
                )
            ),
        )
        timeline = EditTimeline(
            "timeline-large",
            "project-1",
            "plan-1",
            1,
            "a" * 64,
            Rational(30, 1),
            1920,
            1080,
            5_000,
            (
                Track("video-1", TrackKind.VIDEO, video_items),
                Track("audio-1", TrackKind.AUDIO, (audio,)),
            ),
        )
        started = time.monotonic()
        report = self.validate(timeline=timeline)
        elapsed = time.monotonic() - started
        self.assertEqual(report.status, ValidationStatus.PASS)
        self.assertLess(elapsed, 5)

    def test_issue_output_is_bounded_and_reports_truncation(self) -> None:
        with patch("clipen.domain.timeline_validation._MAX_REPORTED_ISSUES", 3):
            report = self.validate(profile=CapabilityProfile("empty", 1))
        self.assertEqual(len(report.issues), 3)
        self.assertEqual(
            report.issues[-1].rule_id,
            "timeline.validation.issue_limit",
        )
        self.assertEqual(report.status, ValidationStatus.BLOCKING)


if __name__ == "__main__":
    unittest.main()
