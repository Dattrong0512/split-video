import unittest

from backend.pipeline import PipelineError, _atempo, cluster_rectangles


class PipelineHelpersTest(unittest.TestCase):
    def test_atempo_is_valid_for_extreme_speed(self):
        self.assertEqual(_atempo(4.0), "atempo=2,atempo=2.000000")
        self.assertEqual(_atempo(0.25), "atempo=.5,atempo=0.500000")

    def test_clusters_changing_subtitle_boxes_by_position(self):
        detections = [
            (0, {"x": .20, "y": .78, "w": .60, "h": .08}),
            (1, {"x": .18, "y": .775, "w": .64, "h": .085}),
            (2, {"x": .25, "y": .79, "w": .50, "h": .07}),
        ]
        regions = cluster_rectangles(detections, 10)
        self.assertEqual(len(regions), 1)
        self.assertGreater(regions[0]["w"], .5)
        self.assertGreater(regions[0]["y"], .7)

    def test_ignores_single_scene_text_detection(self):
        regions = cluster_rectangles([(0, {"x": .2, "y": .2, "w": .2, "h": .05})], 24)
        self.assertEqual(regions, [])

    def test_pipeline_error_keeps_public_code(self):
        error = PipelineError("COOKIE_EXPIRED", "expired")
        self.assertEqual(error.code, "COOKIE_EXPIRED")


if __name__ == "__main__":
    unittest.main()
