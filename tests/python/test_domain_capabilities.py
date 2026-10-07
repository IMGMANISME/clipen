import json
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator

from clipen.domain import (
    CapabilityProfile,
    DeliveryMode,
    EffectCapability,
    ElementCapability,
    TrackKind,
    VerifiedDelivery,
)


class CapabilityProfileTests(unittest.TestCase):
    def delivery(self, mode: DeliveryMode) -> VerifiedDelivery:
        return VerifiedDelivery(
            mode,
            f"implementation-{mode.value}",
            f"test-{mode.value}",
        )

    def profile(self) -> CapabilityProfile:
        movie = self.delivery(DeliveryMode.MOVIE)
        resolve = self.delivery(DeliveryMode.RESOLVE_PROJECT)
        return CapabilityProfile(
            "desktop-macos",
            1,
            elements=(
                ElementCapability(TrackKind.VIDEO, (movie, resolve)),
                ElementCapability(TrackKind.TITLE, (resolve, movie)),
            ),
            effects=(
                EffectCapability(
                    "clipen.transform",
                    1,
                    (TrackKind.VIDEO, TrackKind.GRAPHIC),
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "scale_percent": {
                                "type": "number",
                                "minimum": 1,
                                "maximum": 400,
                            }
                        },
                    },
                    (resolve, movie),
                ),
            ),
        )

    def test_records_only_explicitly_verified_delivery_paths(self) -> None:
        profile = self.profile()
        video = profile.element(TrackKind.VIDEO)
        self.assertTrue(video.supports(DeliveryMode.MOVIE))
        self.assertTrue(video.supports(DeliveryMode.RESOLVE_PROJECT))
        self.assertIsNone(profile.element(TrackKind.AUDIO))

        safe_default = CapabilityProfile("unverified", 1)
        self.assertEqual(safe_default.elements, ())
        self.assertEqual(safe_default.effects, ())

    def test_profile_is_canonical_and_immutable(self) -> None:
        profile = self.profile()
        payload = profile.to_payload()
        self.assertEqual(payload["content_hash"], profile.content_hash)
        self.assertEqual(
            [item["kind"] for item in payload["elements"]],
            ["title", "video"],
        )
        schema = profile.effects[0].parameter_schema
        schema["properties"]["scale_percent"]["maximum"] = 999
        self.assertEqual(
            profile.effects[0].parameter_schema["properties"]["scale_percent"]["maximum"],
            400,
        )

    def test_rejects_unproven_or_ambiguous_capability_data(self) -> None:
        movie = self.delivery(DeliveryMode.MOVIE)
        with self.assertRaisesRegex(ValueError, "unique"):
            ElementCapability(TrackKind.VIDEO, (movie, movie))
        with self.assertRaisesRegex(ValueError, "external references"):
            EffectCapability(
                "clipen.transform",
                1,
                (TrackKind.VIDEO,),
                {"type": "object", "$ref": "https://example.invalid/schema"},
                (movie,),
            )
        with self.assertRaisesRegex(ValueError, "versions"):
            effect = self.profile().effects[0]
            CapabilityProfile("duplicate", 1, effects=(effect, effect))

    def test_payload_matches_public_schema(self) -> None:
        schema_path = Path(__file__).parents[2] / "schemas/v1/capability-profile.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(self.profile().to_payload())


if __name__ == "__main__":
    unittest.main()
