import unittest
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from backend.pipeline import (
    DEFAULT_GEMINI_MODEL, TTS_CACHE_VERSION, PipelineError, _apply_translation_rows, _atempo, _gemini_retry_options,
    _old_ocr_rows, _screen_subtitle_text, _translation_cue_payload, _translation_prompt, _translation_schema,
    _v3_ocr_boxes, _v3_ocr_rows, audio_mix_filter, cluster_rectangles, encoding_options, media_duration,
    ensure_portrait_subtitle_blur, plan_dubbing_timeline, transcribe,
    render_job, repeated_tts_tail_cutoff, separate_background, synthesize_cue, synthesize_verified_clone, tts_text_score,
    video_filter, write_ass,
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

    def test_reads_paddleocr_v3_text_and_confidence(self):
        result = {"res": {
            "rec_boxes": [[20, 60, 180, 80]], "rec_texts": ["这是原字幕"], "rec_scores": [.97],
        }}
        rows = _v3_ocr_rows([result], 200, 100)
        self.assertEqual(rows[0]["text"], "这是原字幕")
        self.assertAlmostEqual(rows[0]["score"], .97)
        self.assertEqual(rows[0]["rect"], {"x": .1, "y": .6, "w": .8, "h": .2})

    def test_reads_nested_legacy_paddleocr_text(self):
        result = [[[[(20, 60), (180, 60), (180, 80), (20, 80)], ("原字幕", .91)]]]
        rows = _old_ocr_rows(result, 200, 100)
        self.assertEqual(rows[0]["text"], "原字幕")
        self.assertAlmostEqual(rows[0]["score"], .91)

    def test_screen_subtitle_prefers_lower_chinese_and_rejects_watermarks(self):
        rows = [
            {"rect": {"x": .1, "y": .1, "w": .3, "h": .05}, "text": "顶部水印", "score": .99},
            {"rect": {"x": .2, "y": .68, "w": .6, "h": .05}, "text": "这是原来的字幕", "score": .96},
            {"rect": {"x": .2, "y": .76, "w": .6, "h": .05}, "text": "第二行", "score": .95},
            {"rect": {"x": .2, "y": .7, "w": .6, "h": .05}, "text": "English only", "score": .99},
            {"rect": {"x": .01, "y": .82, "w": .12, "h": .04}, "text": "角落水印", "score": .99},
        ]
        self.assertEqual(_screen_subtitle_text(rows), "这是原来的字幕\n第二行")

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

    def test_one_voice_mode_forces_every_cue_to_the_same_speaker_slot(self):
        cues = [
            {"id": 0, "start": 0, "end": 1, "original": "你好"},
            {"id": 1, "start": 1, "end": 2, "original": "再见"},
        ]
        rows = [
            {"id": 0, "original_corrected": "你好", "text_vi": "Xin chào.", "confidence": .9},
            {"id": 1, "original_corrected": "再见", "text_vi": "Tạm biệt.", "confidence": .9},
        ]

        translated = _apply_translation_rows(cues, rows, speaker_count=1)

        self.assertEqual([cue["speaker"] for cue in translated], ["S1", "S1"])
        self.assertEqual([cue["gender"] for cue in translated], ["unknown", "unknown"])

    def test_multi_voice_mode_restricts_speakers_to_selected_slots(self):
        cues = [
            {"id": 0, "start": 0, "end": 1, "original": "甲"},
            {"id": 1, "start": 1, "end": 2, "original": "乙"},
        ]
        rows = [
            {"id": 0, "original_corrected": "甲", "text_vi": "Một.", "speaker": "S2", "gender": "male", "confidence": .9},
            {"id": 1, "original_corrected": "乙", "text_vi": "Hai.", "speaker": "S9", "gender": "female", "confidence": .9},
        ]

        translated = _apply_translation_rows(cues, rows, speaker_count=2)

        self.assertEqual([cue["speaker"] for cue in translated], ["S2", "S1"])

    def test_translation_payload_uses_speech_and_preserves_timestamps(self):
        payload = _translation_cue_payload({
            "id": 2, "start": 1, "end": 2, "original": "需要修改的听写", "screen_text": "不应发送给Gemini",
        })
        self.assertNotIn("on_screen_subtitle_ocr", payload)
        self.assertEqual(payload["whisper_transcript"], "需要修改的听写")
        self.assertEqual(payload["start_seconds"], 1)
        self.assertEqual(payload["end_seconds"], 2)
        self.assertEqual(payload["target_vi_characters"], 18)

    def test_gemini_can_merge_whisper_fragments_into_complete_sentences(self):
        cues = [
            {"id": 0, "start": 0.0, "end": .45, "original": "今天来河边"},
            {"id": 1, "start": .45, "end": 1.2, "original": "钓鱼"},
            {"id": 2, "start": 1.4, "end": 2.4, "original": "还没掉到呢"},
        ]
        rows = [
            {"source_ids": [0, 1], "original_corrected": "今天来河边钓鱼。", "text_vi": "Hôm nay ra bờ sông câu cá.", "confidence": .95},
            {"source_ids": [2], "original_corrected": "还没钓到呢。", "text_vi": "Vẫn chưa câu được đâu.", "confidence": .9},
        ]

        translated = _apply_translation_rows(cues, rows, speaker_count=1)

        self.assertEqual(len(translated), 2)
        self.assertEqual(translated[0]["source_ids"], [0, 1])
        self.assertEqual((translated[0]["start"], translated[0]["end"]), (0.0, 1.2))
        self.assertEqual(translated[1]["text_vi"], "Vẫn chưa câu được đâu.")

    def test_gemini_sentence_groups_must_cover_source_cues_once_in_order(self):
        cues = [
            {"id": 0, "start": 0, "end": 1, "original": "甲"},
            {"id": 1, "start": 1, "end": 2, "original": "乙"},
        ]
        rows = [
            {"source_ids": [1], "original_corrected": "乙", "text_vi": "Hai.", "confidence": .9},
            {"source_ids": [0], "original_corrected": "甲", "text_vi": "Một.", "confidence": .9},
        ]

        with self.assertRaises(ValueError):
            _apply_translation_rows(cues, rows, speaker_count=1)

    def test_translation_prompt_uses_only_whisper_not_audio_or_ocr(self):
        prompt = _translation_prompt([{
            "id": 2, "start": 1, "end": 2, "original": "只使用语音识别文字", "screen_text": "不要使用OCR",
        }])
        self.assertIn("只使用语音识别文字", prompt)
        self.assertNotIn("不要使用OCR", prompt)
        self.assertNotIn("audio đính kèm", prompt)
        self.assertIn("start_seconds", prompt)
        self.assertIn("end_seconds", prompt)

    def test_translation_prompt_requests_contextual_homophone_correction_and_sentence_grouping(self):
        prompt = _translation_prompt([
            {"id": 0, "start": 0, "end": 1, "original": "今天来河边钓鱼"},
            {"id": 1, "start": 1, "end": 2, "original": "还没掉到呢"},
        ], 1)

        self.assertIn("source_ids", prompt)
        self.assertIn("từ đồng âm", prompt)
        self.assertIn("gộp các cue liền kề", prompt)
        self.assertIn("今天来河边钓鱼", prompt)
        self.assertIn("还没掉到呢", prompt)

    def test_translation_schema_stays_simple_for_flash_lite(self):
        schema = _translation_schema()
        self.assertNotIn("minItems", schema)
        self.assertNotIn("maxItems", schema)
        self.assertNotIn("additionalProperties", schema["items"])
        self.assertEqual(schema["items"]["properties"]["confidence"], {"type": "number"})

    def test_one_voice_translation_schema_and_prompt_do_not_ask_gemini_to_cast(self):
        schema = _translation_schema(1)
        properties = schema["items"]["properties"]
        prompt = _translation_prompt([{"id": 0, "start": 0, "end": 1, "original": "你好"}], 1)

        self.assertNotIn("speaker", properties)
        self.assertNotIn("gender", properties)
        self.assertNotIn("speaker", prompt.lower())
        self.assertNotIn("gender", prompt.lower())
        self.assertIn("chỉ sửa transcript và viết lại phụ đề", prompt.lower())

    def test_two_voice_translation_requires_exactly_two_cast_slots(self):
        schema = _translation_schema(2)
        prompt = _translation_prompt([{"id": 0, "start": 0, "end": 1, "original": "你好"}], 2)

        self.assertIn("speaker", schema["items"]["properties"])
        self.assertIn("S1, S2", prompt)
        self.assertIn("đúng 2 vai nói", prompt)

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

    def test_short_and_long_tts_for_one_character_share_a_natural_speed(self):
        cues = [{"start": 0, "end": 2}, {"start": 2.1, "end": 3}]
        timeline = plan_dubbing_timeline(cues, [.3, 3], 3.2)
        self.assertAlmostEqual(timeline[0]["speed"], timeline[1]["speed"])
        self.assertTrue(.95 <= timeline[0]["speed"] <= 1.08)

    def test_automatic_speed_does_not_exceed_natural_limit_to_force_exact_fit(self):
        cue = [{"start": 1, "end": 3}]
        timeline = plan_dubbing_timeline(cue, [2.2], 4)
        self.assertAlmostEqual(timeline[0]["speed"], 1.08)
        self.assertAlmostEqual(timeline[0]["end"] - timeline[0]["start"], 2.2 / 1.08)

    def test_global_speech_rate_adjusts_fitted_voice_after_automatic_timing(self):
        cue = [{"start": 0, "end": 2}]
        normal = plan_dubbing_timeline(cue, [2.2], 3, speech_rate=1.0)[0]
        faster = plan_dubbing_timeline(cue, [2.2], 3, speech_rate=1.2)[0]
        self.assertAlmostEqual(normal["speed"], 1.08)
        self.assertAlmostEqual(faster["speed"], 1.296)
        self.assertLess(faster["end"], normal["end"])

    def test_each_character_uses_one_uniform_automatic_speech_rate(self):
        cues = [
            {"start": 0, "end": 1, "speaker": "S1"},
            {"start": 1.5, "end": 3.5, "speaker": "S1"},
            {"start": 4, "end": 5, "speaker": "S2"},
        ]
        timeline = plan_dubbing_timeline(cues, [.5, 3.0, 1.0], 6)

        self.assertAlmostEqual(timeline[0]["speed"], timeline[1]["speed"])
        self.assertTrue(all(.90 <= item["speed"] <= 1.15 for item in timeline))

    def test_clone_synthesis_uses_cue_duration_and_full_quality_steps(self):
        model = SimpleNamespace(
            sampling_rate=24000,
            generate=MagicMock(return_value=[[0.0, 0.0]]),
        )
        voices = {"voice-id": {
            "path": Path("reference.wav"), "transcript": "Đây là giọng tham chiếu.",
        }}
        with patch("backend.pipeline._omnivoice", return_value=model), patch("soundfile.write") as write:
            synthesize_cue(
                "Đây là câu cần nói.", "clone:voice-id", Path("raw.wav"), voices,
                target_duration=1.7,
            )
        options = model.generate.call_args.kwargs
        self.assertEqual(options["duration"], 1.7)
        self.assertEqual(options["num_step"], 32)
        self.assertTrue(options["postprocess_output"])
        write.assert_called_once()

    def test_repeated_unwritten_tts_tail_is_cut_after_expected_sentence(self):
        words = [
            SimpleNamespace(word="Tôi", start=0, end=.2),
            SimpleNamespace(word="không", start=.22, end=.45),
            SimpleNamespace(word="biết", start=.47, end=.7),
            SimpleNamespace(word="tại", start=.72, end=.9),
            SimpleNamespace(word="sao", start=.92, end=1.1),
            SimpleNamespace(word="sao", start=1.13, end=1.3),
            SimpleNamespace(word="sao", start=1.33, end=1.5),
        ]
        self.assertAlmostEqual(repeated_tts_tail_cutoff("Tôi không biết tại sao.", words), 1.16)

    def test_legitimate_non_repeated_sentence_ending_is_not_cut(self):
        words = [
            SimpleNamespace(word="Tại", start=0, end=.2),
            SimpleNamespace(word="sao", start=.22, end=.45),
        ]
        self.assertIsNone(repeated_tts_tail_cutoff("Tại sao?", words))

    def test_transcript_gate_rejects_repeated_sao_even_after_long_correct_line(self):
        expected = "Tôi không biết vì sao chuyện này lại xảy ra và mọi người đều rất bất ngờ."
        recognized = expected + " Sao sao sao."
        self.assertLess(tts_text_score(expected, recognized), .68)

    def test_transcript_gate_rejects_mid_sentence_stutter(self):
        expected = "Tôi không biết vì sao chuyện này lại xảy ra."
        recognized = "Tôi không biết vì sao sao sao sao chuyện này lại xảy ra."
        self.assertLess(tts_text_score(expected, recognized), .68)

    def test_transcript_gate_accepts_legitimate_repeated_word(self):
        text = "Ba ba ba đều là những người cha tốt."
        self.assertGreater(tts_text_score(text, text), .8)

    def test_transcript_gate_accepts_clean_vietnamese_line(self):
        text = "Tôi không biết tại sao."
        self.assertEqual(tts_text_score(text, text), 1.0)

    def test_bad_clone_attempts_fail_instead_of_switching_to_another_voice(self):
        with TemporaryDirectory() as directory:
            raw = Path(directory) / "raw.wav"
            attempted_voices = []

            def fake_synthesize(_text, voice, path, _voices, target_duration=None):
                attempted_voices.append(voice)
                path.write_bytes(b"clone")

            with patch("backend.pipeline.synthesize_cue", side_effect=fake_synthesize), \
                    patch("backend.pipeline.trim_repeated_tts_tail", return_value=False), \
                    patch("backend.pipeline.tts_transcript_score", side_effect=[.31, .44, .38]):
                with self.assertRaises(PipelineError) as raised:
                    synthesize_verified_clone("Đây là câu đúng.", "clone:test", raw, {}, 1.2)

            self.assertEqual(raised.exception.code, "VOICE_FAILED")
            self.assertEqual(attempted_voices, ["clone:test"] * 3)

    def test_verified_clone_is_used_without_fallback(self):
        with TemporaryDirectory() as directory:
            raw = Path(directory) / "raw.wav"

            def fake_synthesize(_text, _voice, path, _voices, target_duration=None):
                path.write_bytes(b"clean-clone")

            with patch("backend.pipeline.synthesize_cue", side_effect=fake_synthesize), \
                    patch("backend.pipeline.trim_repeated_tts_tail", return_value=True), \
                    patch("backend.pipeline.tts_transcript_score", return_value=.91):
                score, fallback = synthesize_verified_clone(
                    "Đây là câu đúng.", "clone:test", raw, {}, 1.2,
                )
            self.assertFalse(fallback)
            self.assertAlmostEqual(score, .91)
            self.assertEqual(raw.read_bytes(), b"clean-clone")

    def test_verified_clone_is_allowed_to_finish_the_full_sentence_before_timing(self):
        attempted_durations = []
        with TemporaryDirectory() as directory:
            raw = Path(directory) / "raw.wav"

            def fake_synthesize(_text, _voice, path, _voices, target_duration=None):
                attempted_durations.append(target_duration)
                path.write_bytes(b"complete-sentence")

            with patch("backend.pipeline.synthesize_cue", side_effect=fake_synthesize), \
                    patch("backend.pipeline.trim_repeated_tts_tail", return_value=True), \
                    patch("backend.pipeline.tts_transcript_score", return_value=.95):
                synthesize_verified_clone("Đây là một câu đầy đủ.", "clone:test", raw, {}, 1.2)

        self.assertEqual(attempted_durations, [None])

    def test_overflowing_cues_compact_into_pauses_before_video_end(self):
        cues = [{"start": 1, "end": 2}, {"start": 4, "end": 4.5}]
        timeline = plan_dubbing_timeline(cues, [2, 2], 5)
        self.assertLessEqual(timeline[-1]["end"], 5)
        self.assertGreaterEqual(timeline[0]["start"], 0)
        self.assertGreaterEqual(timeline[1]["start"], timeline[0]["end"] + .04 - 1e-9)
        self.assertTrue(all(item["speed"] <= 1.15 for item in timeline))

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

    def test_subtitle_rect_gets_blurred_translucent_panel(self):
        rect = SimpleNamespace(x=.1, y=.4, w=.8, h=.16)
        filters = video_filter([], Path("subtitles.ass"), rect)
        self.assertIn("gblur=sigma=10:steps=2", filters)
        self.assertIn("color=white@0.16:t=fill", filters)
        self.assertNotIn("color=black", filters)

    def test_background_uses_music_without_original_voice(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            stems = root / "separated" / "htdemucs" / "source"
            stems.mkdir(parents=True)
            no_vocals = stems / "no_vocals.wav"
            vocals = stems / "vocals.wav"
            no_vocals.write_bytes(b"music")
            vocals.write_bytes(b"voice")
            job = {"work_dir": root, "source": root / "source.mp4"}

            with patch("backend.pipeline.subprocess.run", return_value=SimpleNamespace(returncode=0)), patch(
                "backend.pipeline.ffmpeg", return_value="ffmpeg"
            ), patch("backend.pipeline.run") as run_command:
                background = separate_background(job)

            self.assertEqual(background, no_vocals)
            run_command.assert_not_called()
            self.assertNotIn("original", background.name)

    def test_background_fallback_uses_silence_instead_of_original_voice(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            job = {"work_dir": root, "source": root / "source.mp4"}

            with patch("backend.pipeline.subprocess.run", return_value=SimpleNamespace(returncode=1)), patch(
                "backend.pipeline.ffmpeg", return_value="ffmpeg"
            ), patch("backend.pipeline.run") as run_command:
                background = separate_background(job)

            self.assertEqual(background, root / "background.wav")
            command = run_command.call_args.args[0]
            self.assertIn("anullsrc", command)
            self.assertNotIn(str(job["source"]), command)
            self.assertIn("không giữ giọng gốc", job["warning"])

    def test_render_audio_and_container_are_limited_to_source_duration(self):
        filters = audio_mix_filter(74.1)
        self.assertIn("amix=inputs=2:duration=first", filters)
        self.assertEqual(filters.count("atrim=0:74.100"), 3)
        options = encoding_options((720, 1280), 74.1)
        self.assertEqual(options[options.index("-t") + 1], "74.100")
        self.assertEqual(options[options.index("-maxrate") + 1], "3000k")
        self.assertEqual(options[options.index("-b:a") + 1], "128k")

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required for render integration")
    def test_preview_then_full_render_reuses_synthesized_voice(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source, background = root / "source.mp4", root / "background.wav"
            subprocess.run([
                shutil.which("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "color=c=blue:s=320x568:d=2",
                "-f", "lavfi", "-i", "sine=frequency=220:duration=2",
                "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(source),
            ], check=True)
            subprocess.run([
                shutil.which("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "sine=frequency=330:duration=2", str(background),
            ], check=True)

            generated_targets = []

            def fake_synthesize(_text, _voice, raw, _voices, target_duration=None):
                generated_targets.append(target_duration)
                subprocess.run([
                    shutil.which("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "sine=frequency=660:duration=1", "-ar", "24000", "-ac", "1", str(raw),
                ], check=True)

            job = {
                "id": "preview", "work_dir": root, "source": source, "duration": 2.0, "video_size": (320, 568),
                "cues": [{"id": 0, "start": 0, "end": 1, "text_vi": "Xin chào.", "speaker": "S1"}],
                "cancelled": False,
            }
            request = SimpleNamespace(
                voiceMap={"*": "edge:vi-VN-HoaiMyNeural"}, speechRate=1.1, previewOnly=True,
                blurRegions=[], subtitleRect=SimpleNamespace(x=.08, y=.72, w=.84, h=.16),
            )
            with patch("backend.pipeline.synthesize_cue", side_effect=fake_synthesize) as synthesize, \
                    patch("backend.pipeline.separate_background", return_value=background):
                render_job(job, request, {})
                self.assertEqual(job["status"], "preview_ready")
                self.assertTrue(job["review_result"].exists())
                self.assertNotIn("result", job)
                self.assertAlmostEqual(media_duration(job["review_result"]), 2.0, places=1)
                first_review = job["review_result"]
                request.speechRate = 1.2
                render_job(job, request, {})
                self.assertNotEqual(job["review_result"], first_review)
                self.assertTrue(first_review.exists())
                request.previewOnly = False
                render_job(job, request, {})
            self.assertEqual(job["status"], "complete")
            self.assertTrue(job["result"].exists())
            self.assertEqual(synthesize.call_count, 1)
            self.assertEqual(generated_targets, [1.0])
            self.assertEqual(job["tts_cache"]["0"]["version"], TTS_CACHE_VERSION)


if __name__ == "__main__":
    unittest.main()
