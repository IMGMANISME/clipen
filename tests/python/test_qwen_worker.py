from pathlib import Path
import tempfile
import unittest

from clipen.analysis import qwen_worker


class QwenWorkerTests(unittest.TestCase):
    def test_audio_quality_only_requests_enhancement_for_quiet_or_noisy_capture(self) -> None:
        clean = qwen_worker._quality_from_rms(
            [0.001] * 80 + [0.05] * 20, peak=0.4, clipping_ratio=0
        )
        quiet = qwen_worker._quality_from_rms(
            [0.001] * 80 + [0.012] * 20, peak=0.08, clipping_ratio=0
        )
        noisy = qwen_worker._quality_from_rms(
            [0.01] * 80 + [0.025] * 20, peak=0.2, clipping_ratio=0
        )

        self.assertFalse(clean["needs_enhancement"])
        self.assertTrue(quiet["needs_enhancement"])
        self.assertTrue(noisy["needs_enhancement"])

    def test_adaptive_retry_is_bounded_by_duration(self) -> None:
        self.assertEqual(qwen_worker._retry_budget(12 * 16_000), 1)
        self.assertEqual(qwen_worker._retry_budget(60 * 16_000), 1)
        self.assertEqual(qwen_worker._retry_budget(5 * 60 * 16_000), 4)

    def test_capture_quality_controls_retry_even_when_model_is_confident(self) -> None:
        weak_capture = {"needs_enhancement": True}
        clean_capture = {"needs_enhancement": False}

        self.assertTrue(qwen_worker._should_retry(
            {"text": "清楚口語", "accepted": True, "score": 0.9}, weak_capture
        ))
        self.assertTrue(qwen_worker._should_retry(
            {"text": "不確定口語", "accepted": True, "score": 0.72}, weak_capture
        ))
        self.assertTrue(qwen_worker._should_retry(
            {"text": "", "accepted": False, "score": 0.2}, weak_capture
        ))
        self.assertFalse(qwen_worker._should_retry(
            {"text": "清楚口語", "accepted": True, "score": 0.9}, clean_capture
        ))

    def test_enhancement_disagreement_remains_reviewable(self) -> None:
        original = {"text": "我們去玩尼莫", "accepted": True, "score": 0.72}
        enhanced = {"text": "我們去吃檸檬", "accepted": True, "score": 0.9}

        selected = qwen_worker._choose_candidate(original, enhanced)

        self.assertEqual(selected, ("我們去玩尼莫", 0.64))

    def test_enhancement_only_recovery_remains_reviewable(self) -> None:
        original = {"text": "", "accepted": False, "score": 0.1}
        enhanced = {"text": "點一個酪梨吐司", "accepted": True, "score": 0.91}

        selected = qwen_worker._choose_candidate(original, enhanced)

        self.assertEqual(selected, ("點一個酪梨吐司", 0.64))

    def test_matching_original_and_enhanced_candidates_keep_best_confidence(self) -> None:
        original = {"text": "黑咖啡跟拿鐵", "accepted": True, "score": 0.71}
        enhanced = {"text": "黑咖啡，跟拿鐵。", "accepted": True, "score": 0.86}

        selected = qwen_worker._choose_candidate(original, enhanced)

        self.assertEqual(selected, ("黑咖啡，跟拿鐵。", 0.86))

    def test_weak_capture_without_stable_second_reading_requires_review(self) -> None:
        weak = {"needs_enhancement": True}
        clean = {"needs_enhancement": False}
        original = {"text": "我們去玩泥模", "accepted": True, "score": 0.94}
        matching = {"text": "我們去玩泥模。", "accepted": True, "score": 0.91}
        different = {"text": "我們去玩泥莫", "accepted": True, "score": 0.93}

        self.assertTrue(qwen_worker._selection_needs_review(original, None, weak))
        self.assertTrue(
            qwen_worker._selection_needs_review(original, different, weak)
        )
        self.assertFalse(
            qwen_worker._selection_needs_review(original, matching, weak)
        )
        self.assertFalse(
            qwen_worker._selection_needs_review(original, None, clean)
        )

    def test_labelled_pocket3_foreground_speech_is_retained(self) -> None:
        # 0018: 點一個酪梨吐司、水波蛋吐司，黑咖啡跟拿鐵。
        accepted_0018 = qwen_worker._accept_candidate(
            "点一个洛丽吐司、水波蛋吐司，黑咖啡跟拿铁。",
            [0.9] * 17 + [0.4, 0.45],
            ended=True,
        )
        # 0047: 我們現在要去玩尼莫，陳均葦說要吃尼莫，這個不是嗎？
        accepted_0047 = qwen_worker._accept_candidate(
            "我们现在要去玩泥模，很多人说要吃泥模，这个不是吗？",
            [0.96] * 16 + [0.3],
            ended=True,
        )

        self.assertTrue(accepted_0018[0])
        self.assertTrue(accepted_0047[0])

    def test_low_confidence_background_candidate_is_rejected(self) -> None:
        # A short grammatical phrase is still rejected when its token evidence
        # is weak. High-confidence background speech is handled as reviewable.
        accepted, score = qwen_worker._accept_candidate(
            "我就是说，这个。",
            [0.05, 0.08, 0.12, 0.2, 0.32, 0.44],
            ended=True,
        )

        self.assertFalse(accepted)
        self.assertLess(score, 0.68)

    def test_fluent_low_confidence_hallucination_is_rejected(self) -> None:
        accepted, score = qwen_worker._accept_candidate(
            "在日式烤肉餐廳裡，海陸山川的菜餚。",
            [0.8] * 8 + [0.4] * 4 + [0.2] * 3,
            ended=True,
        )

        self.assertFalse(accepted)
        self.assertLess(score, 0.68)

    def test_unfinished_or_repetitive_generation_is_rejected(self) -> None:
        self.assertFalse(
            qwen_worker._accept_candidate("準備" * 20, [0.99] * 40, ended=True)[0]
        )
        self.assertFalse(
            qwen_worker._accept_candidate("今天要去東京", [0.99] * 6, ended=False)[0]
        )

    def test_internal_windows_are_bounded_and_prefer_quiet_cut(self) -> None:
        frame_size = 320
        energies = [0.01] * (30 * 16_000 // frame_size)
        energies[10 * 16_000 // frame_size] = 0

        ranges = qwen_worker._window_ranges_from_energy(
            30 * 16_000, energies, frame_size
        )

        self.assertEqual(ranges[0][1], 10 * 16_000)
        self.assertEqual(ranges[-1][1], 30 * 16_000)
        self.assertTrue(all(end - start <= 12 * 16_000 for start, end in ranges))

    def test_audible_activity_filter_only_skips_silence_and_low_noise(self) -> None:
        self.assertFalse(qwen_worker._audible_from_rms([0.0] * 50, 16_000))
        self.assertFalse(qwen_worker._audible_from_rms([0.001] * 50, 16_000))
        self.assertTrue(qwen_worker._audible_from_rms([0.02] * 10 + [0.0] * 40, 16_000))

    def test_model_contract_rejects_extra_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in qwen_worker._MODEL_FILES:
                (root / name).write_bytes(b"model")
            (root / "unexpected.bin").write_bytes(b"unexpected")

            with self.assertRaisesRegex(ValueError, "model"):
                qwen_worker._model_files(root)


if __name__ == "__main__":
    unittest.main()
