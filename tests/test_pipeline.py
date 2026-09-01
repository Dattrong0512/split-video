import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from backend.pipeline import (
    DEFAULT_GEMINI_MODEL, PipelineError, _apply_translation_rows, _atempo, _gemini_retry_options,
    _translation_schema, _v3_ocr_boxes,
    cluster_rectangles, ensure_portrait_subtitle_blur, plan_dubbing_timeline, transcribe, write_ass,
)


class PipelineHelpersTest(unittest.TestCase):
    def test_cheap_flash_lite_is_default_with_503_retry(self):
        self.assertEqual(DEFAULT_GEMINI_MODEL, "gemini-2.5-flash-lite")
        retry = _gemini_retry_options()
        self.assertEqual(retry["attempts"], 6)
        self.assertIn(503, retry["http_status_codes"])

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

    def test_cluster_uses_union_instead_of_shrinking_changing_text(self):
        detections = [
            (0, {"x": .10, "y": .70, "w": .30, "h": .05}),
            (1, {"x": .55, "y": .70, "w": .35, "h": .05}),
        ]
        region = cluster_rectangles(detections, 10)[0]
        self.assertLessEqual(region["x"], .075001)
        self.assertGreaterEqual(region["x"] + region["w"], .925)

    def test_portrait_video_gets_fallback_when_ocr_is_empty(self):
        self.assertEqual(
            ensure_portrait_subtitle_blur([], 720, 1280),
            [{"x": .035, "y": .655, "w": .93, "h": .115}],
        )

    def test_portrait_fallback_does_not_replace_detected_subtitle_band(self):
        detected = [{"x": .2, "y": .68, "w": .6, "h": .08}]
        self.assertEqual(ensure_portrait_subtitle_blur(detected, 720, 1280), detected)

    def test_color_watermark_does_not_suppress_missing_ocr_fallback(self):
        watermark = {"x": .7, "y": .7, "w": .25, "h": .04}
        regions = ensure_portrait_subtitle_blur([watermark], 720, 1280, subtitle_evidence=[])
        self.assertEqual(regions[0], {"x": .035, "y": .655, "w": .93, "h": .115})
        self.assertIn(watermark, regions)

    def test_landscape_video_does_not_get_broad_fallback(self):
        self.assertEqual(ensure_portrait_subtitle_blur([], 1920, 1080), [])

    def test_ignores_single_scene_text_detection(self):
        regions = cluster_rectangles([(0, {"x": .2, "y": .2, "w": .2, "h": .05})], 24)
        self.assertEqual(regions, [])

    def test_pipeline_error_keeps_public_code(self):
        error = PipelineError("COOKIE_EXPIRED", "expired")
        self.assertEqual(error.code, "COOKIE_EXPIRED")

    def test_reads_paddleocr_v3_boxes(self):
        class Result:
            json = {"res": {"rec_boxes": [[10, 20, 110, 70]]}}
        boxes = _v3_ocr_boxes([Result()], 200, 100)
        self.assertEqual(boxes, [{"x": .05, "y": .2, "w": .5, "h": .5}])

    def test_whisper_is_constrained_to_chinese_for_douyin(self):
        model = SimpleNamespace(transcribe=MagicMock(return_value=(
            [SimpleNamespace(start=0, end=1, text="你好")], None,
        )))
        with patch("backend.pipeline._whisper", return_value=model):
            cues = transcribe(Path("audio.mp3"))
        self.assertEqual(cues[0]["original"], "你好")
        self.assertEqual(model.transcribe.call_args.kwargs["language"], "zh")

    def test_translation_rows_preserve_ids_and_store_correction_confidence(self):
        cues = [{"id": 7, "start": 0, "end": 1, "original": "泥好"}]
        rows = [{
            "id": 7, "original_corrected": "你好", "text_vi": "Xin chào.",
            "speaker": "S1", "gender": "female", "confidence": .96,
        }]
        translated = _apply_translation_rows(cues, rows)
        self.assertEqual(translated[0]["original_corrected"], "你好")
        self.assertEqual(translated[0]["text_vi"], "Xin chào.")
        self.assertEqual(translated[0]["confidence"], .96)
        self.assertEqual(_translation_schema(1)["maxItems"], 1)

    def test_translation_rows_reject_changed_ids(self):
        with self.assertRaises(ValueError):
            _apply_translation_rows(
                [{"id": 0}],
                [{"id": 1, "original_corrected": "好", "text_vi": "Được.", "confidence": 1}],
            )

    def test_dubbing_speed_is_bounded_and_close_cues_do_not_overlap(self):
        cues = [{"start": 0, "end": 1}, {"start": 1.05, "end": 1.5}, {"start": 3, "end": 3.3}]
        timeline = plan_dubbing_timeline(cues, [2.5, .2, .3], 4)
        self.assertTrue(all(.90 <= item["speed"] <= 1.15 for item in timeline))
        self.assertGreaterEqual(timeline[1]["start"], timeline[0]["end"] + .04 - 1e-9)
        self.assertGreaterEqual(timeline[2]["start"], timeline[1]["end"] + .04 - 1e-9)

    def test_short_and_long_tts_use_natural_speed_limits(self):
        cues = [{"start": 0, "end": 2}, {"start": 2.1, "end": 3}]
        timeline = plan_dubbing_timeline(cues, [.3, 3], 3.2)
        self.assertAlmostEqual(timeline[0]["speed"], 1.0)
        self.assertAlmostEqual(timeline[1]["speed"], 1.15)

    def test_ass_uses_portrait_resolution_clean_font_and_wraps(self):
        rect = SimpleNamespace(x=.08, y=.78, w=.84, h=.16)
        cues = [{"start": 1, "end": 2.4, "text_vi": "Đây là một câu tiếng Việt khá dài cần được ngắt dòng gọn gàng và dễ đọc."}]
        with TemporaryDirectory() as directory:
            path = Path(directory) / "subtitles.ass"
            write_ass(path, cues, rect, (720, 1280))
            content = path.read_text(encoding="utf-8-sig")
        self.assertIn("PlayResX: 720\nPlayResY: 1280", content)
        self.assertIn("Style: Vietnamese,Noto Sans", content)
        self.assertIn(",1,2,1,2,", content)
        self.assertNotIn("DejaVu Sans", content)
        self.assertIn("\\N", content)


if __name__ == "__main__":
    unittest.main()
