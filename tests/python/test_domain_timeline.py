import json
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator

from clipen.domain import (
    ClipSource,
    EditTimeline,
    EffectRequest,
    GraphicClip,
    Marker,
    MediaClip,
    Rational,
    SourceSpan,
    TextClip,
    Track,
    TrackKind,
)


class TimelineTests(unittest.TestCase):
    def media(self, item_id: str = "clip-1", start_frame: int = 0) -> MediaClip:
        return MediaClip(
            item_id=item_id,
            start_frame=start_frame,
            duration_frames=120,
            source=ClipSource(
                SourceSpan(
                    "av_" + "a" * 32,
                    0,
                    "18018",
                    "198018",
                    Rational(1, 90_000),
                ),
                handle_before_pts=9_000,
                handle_after_pts=18_000,
            ),
            effects=(EffectRequest("clipen.transform", {"scale_percent": 105}),),
        )

    def timeline(self) -> EditTimeline:
        return EditTimeline(
            timeline_id="timeline-1",
            project_id="project-1",
            plan_id="plan-1",
            plan_revision=3,
            plan_content_hash="a" * 64,
            frame_rate=Rational(30_000, 1_001),
            width=3_840,
            height=2_160,
            tracks=(
                Track("video-1", TrackKind.VIDEO, (self.media(),)),
                Track(
                    "title-1",
                    TrackKind.TITLE,
                    (TextClip("title-item", 0, 90, "東京旅行", "vlog-title"),),
                ),
                Track(
                    "graphic-1",
                    TrackKind.GRAPHIC,
                    (GraphicClip("map-item", 120, 60, "art_" + "b" * 32),),
                ),
            ),
            markers=(Marker("review-1", 42, "需要核對", "背景人聲可能不可靠"),),
        )

    def test_serializes_exact_source_time_and_editable_layers(self) -> None:
        payload = self.timeline().to_payload()
        self.assertEqual(payload["frame_rate"], {"num": 30_000, "den": 1_001})
        source = payload["tracks"][0]["items"][0]["source"]
        self.assertEqual(source["start_pts"], "18018")
        self.assertEqual(source["end_pts"], "198018")
        self.assertEqual(source["time_base"], {"num": 1, "den": 90_000})
        self.assertEqual(
            [track["kind"] for track in payload["tracks"]],
            ["video", "title", "graphic"],
        )

    def test_rejects_overlap_and_wrong_item_kind(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot overlap"):
            Track(
                "video-1",
                TrackKind.VIDEO,
                (self.media("clip-1", 0), self.media("clip-2", 119)),
            )
        with self.assertRaisesRegex(ValueError, "incompatible"):
            Track(
                "video-1",
                TrackKind.VIDEO,
                (TextClip("title-item", 0, 30, "Title", "default"),),
            )

    def test_rejects_unsafe_effect_payloads(self) -> None:
        with self.assertRaisesRegex(ValueError, "effect_id"):
            EffectRequest("script:os.system", {})
        with self.assertRaisesRegex(ValueError, "finite"):
            EffectRequest("clipen.transform", {"scale": float("nan")})
        with self.assertRaisesRegex(ValueError, "JSON values"):
            EffectRequest("clipen.transform", {"callback": object()})
        with self.assertRaisesRegex(ValueError, "keys"):
            EffectRequest("clipen.transform", {1: "silently changed by JSON"})

    def test_effect_parameters_are_an_immutable_snapshot(self) -> None:
        parameters = {"position": {"x": 10}, "tags": ["intro"]}
        effect = EffectRequest("clipen.transform", parameters)
        parameters["position"]["x"] = 99
        parameters["tags"].append("changed")
        self.assertEqual(
            effect.parameters,
            {"position": {"x": 10}, "tags": ["intro"]},
        )

    def test_source_time_uses_the_shared_analysis_representation(self) -> None:
        source = self.media().source.span
        self.assertEqual(SourceSpan.from_storage(source.to_storage()), source)
        self.assertEqual(source.time_base, Rational(1, 90_000))
        self.assertEqual(source.duration_pts, 180_000)

    def test_collection_inputs_are_frozen_as_tuples(self) -> None:
        effects = [EffectRequest("clipen.transform", {})]
        clip = MediaClip("clip-1", 0, 30, self.media().source, effects)
        effects.clear()
        self.assertEqual(len(clip.effects), 1)

        items = [clip]
        track = Track("video-1", TrackKind.VIDEO, items)
        items.clear()
        self.assertEqual(len(track.items), 1)

    def test_items_and_markers_are_serialized_in_canonical_time_order(self) -> None:
        track = Track(
            "video-1",
            TrackKind.VIDEO,
            (self.media("later", 120), self.media("earlier", 0)),
        )
        timeline = self.timeline()
        timeline = EditTimeline(
            timeline.timeline_id,
            timeline.project_id,
            timeline.plan_id,
            timeline.plan_revision,
            timeline.plan_content_hash,
            timeline.frame_rate,
            timeline.width,
            timeline.height,
            (track,),
            (Marker("later", 20, "稍後"), Marker("earlier", 10, "較早")),
        )
        self.assertEqual([item.item_id for item in timeline.tracks[0].items], ["earlier", "later"])
        self.assertEqual([marker.marker_id for marker in timeline.markers], ["earlier", "later"])

    def test_rejects_invalid_track_and_visible_text_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "track kind"):
            Track("video-1", "video", ())
        with self.assertRaisesRegex(ValueError, "text clip"):
            TextClip("title-item", 0, 30, "   ", "default")
        with self.assertRaisesRegex(ValueError, "marker text"):
            Marker("marker-1", 0, "\x00", "")
        with self.assertRaisesRegex(ValueError, "source"):
            MediaClip("clip-1", 0, 30, object())

    def test_requires_globally_unique_item_ids(self) -> None:
        clip = self.media()
        with self.assertRaisesRegex(ValueError, "item IDs"):
            EditTimeline(
                timeline_id="timeline-1",
                project_id="project-1",
                plan_id="plan-1",
                plan_revision=1,
                plan_content_hash="a" * 64,
                frame_rate=Rational(24, 1),
                width=1_920,
                height=1_080,
                tracks=(
                    Track("video-1", TrackKind.VIDEO, (clip,)),
                    Track("audio-1", TrackKind.AUDIO, (clip,)),
                ),
            )

    def test_payload_matches_public_schema(self) -> None:
        schema_path = Path(__file__).parents[2] / "schemas/v1/edit-timeline.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(self.timeline().to_payload())

    def test_public_schema_rejects_text_the_domain_rejects(self) -> None:
        schema_path = Path(__file__).parents[2] / "schemas/v1/edit-timeline.schema.json"
        validator = Draft202012Validator(
            json.loads(schema_path.read_text(encoding="utf-8"))
        )
        payload = self.timeline().to_payload()
        payload["tracks"][1]["items"][0]["text"] = "   "
        self.assertTrue(list(validator.iter_errors(payload)))


if __name__ == "__main__":
    unittest.main()
