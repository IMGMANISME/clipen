import unittest

from clipen.domain import Rational, SourceSpan


class MediaTimeTests(unittest.TestCase):
    def test_source_span_round_trips_without_losing_exact_pts(self) -> None:
        span = SourceSpan(
            "av_" + "a" * 32,
            2,
            "-18018",
            "161982",
            Rational(1, 90_000),
        )
        self.assertEqual(SourceSpan.from_storage(span.to_storage()), span)
        self.assertEqual(span.duration_pts, 180_000)

    def test_time_base_preserves_the_source_representation(self) -> None:
        self.assertNotEqual(Rational(60_000, 2_002), Rational(30_000, 1_001))

    def test_rejects_invalid_time_contracts(self) -> None:
        with self.assertRaises(ValueError):
            SourceSpan("av_" + "a" * 32, 0, "20", "20", Rational(1, 1_000))
        with self.assertRaises(ValueError):
            SourceSpan("wrong", 0, "0", "20", Rational(1, 1_000))
        with self.assertRaises(ValueError):
            Rational(0, 1_000)
        with self.assertRaises(ValueError):
            Rational(1, 0)
        with self.assertRaises(ValueError):
            SourceSpan(
                "av_" + "a" * 32,
                0,
                str(-(2**63) - 1),
                "0",
                Rational(1, 1_000),
            )

    def test_storage_decoder_does_not_coerce_wrong_types(self) -> None:
        valid = SourceSpan(
            "av_" + "a" * 32,
            0,
            "0",
            "20",
            Rational(1, 1_000),
        ).to_storage()
        valid["stream_index"] = True
        with self.assertRaises(ValueError):
            SourceSpan.from_storage(valid)

        valid["stream_index"] = 0

        with self.assertRaises(ValueError):
            Rational.from_storage({"num": "1", "den": 1_000})
        with self.assertRaises(ValueError):
            Rational.from_storage({"num": 1, "den": 1_000, "ignored": True})
        with self.assertRaises(ValueError):
            SourceSpan.from_storage({key: value for key, value in valid.items() if key != "end_pts"})
        with self.assertRaises(ValueError):
            SourceSpan.from_storage({**valid, "ignored": True})


if __name__ == "__main__":
    unittest.main()
