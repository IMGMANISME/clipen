from pathlib import Path
import tempfile
import unittest

from clipen.analysis import qwen_worker


class QwenWorkerTests(unittest.TestCase):
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

    def test_labelled_background_only_sample_is_rejected(self) -> None:
        # 0025 contains background sound only. The decoder's short phrase must
        # not become a usable transcript merely because it is grammatical.
        accepted, score = qwen_worker._accept_candidate(
            "我就是说，这个。",
            [0.05, 0.08, 0.12, 0.2, 0.32, 0.44],
            ended=True,
        )

        self.assertFalse(accepted)
        self.assertLess(score, 0.68)

    def test_fluent_background_hallucination_below_labelled_gap_is_rejected(self) -> None:
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
