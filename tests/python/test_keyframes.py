import unittest

from clipen.analysis.keyframes import AdaptiveKeyframeSelector, KeyframeCandidate


class AdaptiveKeyframeSelectorTests(unittest.TestCase):
    def setUp(self):
        self.selector = AdaptiveKeyframeSelector()

    def test_uniform_candidates_use_bin_centers_and_stay_bounded(self):
        candidates = self.selector.uniform(100, 900, 8)
        self.assertEqual([item.pts for item in candidates], [150, 250, 350, 450, 550, 650, 750, 850])
        self.assertTrue(all(item.source == "uniform" for item in candidates))

    def test_flat_scores_preserve_temporal_coverage(self):
        candidates = self.selector.uniform(0, 1_000, 32)
        selection = self.selector.select(0, 1_000, candidates, 8)
        points = [item.pts for item in selection.candidates]
        self.assertEqual(len(points), 8)
        self.assertLess(points[0], 125)
        self.assertGreater(points[-1], 875)
        self.assertTrue(all(b > a for a, b in zip(points, points[1:])))

    def test_strong_relevance_can_focus_selection(self):
        candidates = list(self.selector.uniform(0, 1_000, 32))
        candidates.extend(
            KeyframeCandidate(point, 1.0, "semantic")
            for point in (410, 420, 430, 440)
        )
        selection = self.selector.select(0, 1_000, candidates, 4)
        self.assertEqual([item.pts for item in selection.candidates], [410, 420, 430, 440])
        self.assertTrue(selection.semantic_relevance)

    def test_scene_candidates_are_preferred_without_destroying_coverage(self):
        candidates = list(self.selector.uniform(0, 800, 16))
        candidates.extend(
            KeyframeCandidate(point, score, "scene")
            for point, score in ((50, 0.4), (250, 0.8), (450, 0.7), (650, 0.9))
        )
        selection = self.selector.select(0, 800, candidates, 4)
        self.assertEqual([item.pts for item in selection.candidates], [50, 250, 450, 650])

    def test_duplicate_pts_keep_the_more_informative_candidate(self):
        selection = self.selector.select(
            0,
            100,
            (
                KeyframeCandidate(50, 0.1, "uniform"),
                KeyframeCandidate(50, 0.8, "scene"),
            ),
            1,
        )
        self.assertEqual(selection.candidates[0].source, "scene")

    def test_invalid_inputs_are_rejected(self):
        invalid = (
            lambda: KeyframeCandidate(True, 0.5, "scene"),
            lambda: KeyframeCandidate(1, float("nan"), "scene"),
            lambda: KeyframeCandidate(1, 0.5, "other"),
            lambda: self.selector.select(10, 10, (), 1),
            lambda: self.selector.select(0, 10, (KeyframeCandidate(10, 0.5, "scene"),), 1),
            lambda: self.selector.select(0, 10, (KeyframeCandidate(1, 0.5, "scene"),), 0),
        )
        for operation in invalid:
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                operation()


if __name__ == "__main__":
    unittest.main()
