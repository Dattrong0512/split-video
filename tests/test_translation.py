import json
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

from backend.pipeline import PipelineError, gemini_translate, subtitle_pages


class TranslationRecoveryTest(unittest.TestCase):
    def client(self, responses):
        google = ModuleType("google")
        genai = ModuleType("google.genai")
        models = SimpleNamespace(generate_content=MagicMock(side_effect=[
            SimpleNamespace(text=json.dumps(rows)) for rows in responses
        ]))
        genai.Client = lambda **kwargs: SimpleNamespace(models=models)
        genai.types = SimpleNamespace(
            HttpOptions=lambda **kwargs: kwargs, HttpRetryOptions=lambda **kwargs: kwargs,
            GenerateContentConfig=lambda **kwargs: kwargs,
        )
        google.genai = genai
        return patch.dict("sys.modules", {"google": google, "google.genai": genai}), models

    def test_valid_long_translation_does_not_spend_three_attempts_on_layout(self):
        cues = [{"id": 0, "start": 2, "end": 10, "original": "What is your favourite song? I like Mozart."}]
        text = "Bài hát bạn thích nhất là gì? Tôi thích nghe các tác phẩm của Mozart."
        rows = [{"source_ids": [0], "original_corrected": cues[0]["original"], "text_vi": text, "confidence": .95}]
        modules, models = self.client([rows])
        with modules:
            translated = gemini_translate(cues, "test-key")
        self.assertEqual(models.generate_content.call_count, 1)
        self.assertEqual(translated[0]["text_vi"], text)
        self.assertEqual(" ".join(page["text_vi"] for page in subtitle_pages(translated)), text)

    def test_final_repair_uses_atomic_cues_for_impossible_merged_duration(self):
        cues = [{"id": 0, "start": 0, "end": 3, "original": "Hello"},
                {"id": 1, "start": 3, "end": 6, "original": "Goodbye"}]
        invalid = [{"source_ids": [0, 1], "original_corrected": "Hello. Goodbye.", "text_vi": "Chào. Tạm biệt.", "confidence": .9}]
        valid = [{"source_ids": [index], "original_corrected": cue["original"], "text_vi": text, "confidence": .9}
                 for index, (cue, text) in enumerate(zip(cues, ["Xin chào.", "Tạm biệt."]))]
        modules, models = self.client([invalid, invalid, valid])
        with modules:
            translated = gemini_translate(cues, "test-key")
        self.assertEqual([item["source_ids"] for item in translated], [[0], [1]])
        self.assertIn("Lần sửa cuối: không gộp cue", models.generate_content.call_args.kwargs["contents"])

    def test_missing_source_cues_still_fail_without_silent_data_loss(self):
        cues = [{"id": 0, "start": 0, "end": 1, "original": "Hello"}]
        modules, models = self.client([[], [], []])
        with modules, self.assertRaises(PipelineError) as raised:
            gemini_translate(cues, "test-key")
        self.assertEqual(models.generate_content.call_count, 3)
        self.assertEqual(raised.exception.code, "GEMINI_RESPONSE_INVALID")
        self.assertIn("mỗi source id đúng một lần", str(raised.exception))
