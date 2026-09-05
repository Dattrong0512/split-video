import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from backend.pipeline import create_dubbing, group_dubbing_cues, media_duration, plan_dubbing_timeline, render_job, trim_tts_padding


class TimingRecoveryTest(unittest.TestCase):
    def test_screenshot_overflow_extends_picture_without_rushing_voice(self):
        cues = [{"start": 2.16, "end": 2.56}, {"start": 2.60, "end": 4.0}]
        result = plan_dubbing_timeline(cues, [1.6416, 1.4], 5, overflow_mode="extend_video")
        self.assertAlmostEqual(result[0]["speed"], 1.08)
        self.assertAlmostEqual(result[0]["end"] - result[0]["start"], 1.52)
        self.assertAlmostEqual(result[0]["extra"], 1.12)
        self.assertAlmostEqual(result[1]["start"], 3.72)
        self.assertGreaterEqual(result[1]["start"], result[0]["end"] + .04 - 1e-9)

    def test_original_duration_mode_fits_the_same_overflow(self):
        cues = [{"start": 2.16, "end": 2.56}, {"start": 2.60, "end": 4.0}]
        result = plan_dubbing_timeline(cues, [1.6416, 1.4], 5, overflow_mode="fit_audio")
        self.assertAlmostEqual(result[0]["end"], 2.56)
        self.assertEqual(result[1]["start"], 2.60)
        self.assertEqual(result[0]["extra"], 0)

    def test_fragments_merge_only_with_the_same_speaker_and_without_losing_text(self):
        cues = [
            {"id": 0, "start": 0, "end": .4, "speaker": "S1", "text_vi": "Xin chào."},
            {"id": 1, "start": .44, "end": 2, "speaker": "S1", "text_vi": "Bạn khỏe không?"},
            {"id": 2, "start": 2.04, "end": 2.4, "speaker": "S2", "text_vi": "Khỏe."},
        ]
        grouped = group_dubbing_cues(cues)
        self.assertEqual(len(grouped), 2)
        self.assertEqual(grouped[0]["source_ids"], [0, 1])
        self.assertEqual(grouped[0]["text_vi"], "Xin chào. Bạn khỏe không?")
        self.assertEqual(cues[0]["text_vi"], "Xin chào.")

    def test_padding_trim_keeps_breathing_margin_and_all_spoken_audio(self):
        from pydub import AudioSegment
        from pydub.generators import Sine
        with TemporaryDirectory() as directory:
            path = Path(directory) / "voice.wav"
            sound = AudioSegment.silent(duration=400) + Sine(440).to_audio_segment(duration=1000) + AudioSegment.silent(duration=500)
            with path.open("wb") as stream:
                sound.export(stream, format="wav")
            trim_tts_padding(path)
            with path.open("rb") as stream:
                trimmed = AudioSegment.from_wav(stream)
            self.assertGreaterEqual(len(trimmed), 1140)
            self.assertLess(len(trimmed), 1180)

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required")
    def test_preview_matches_full_audio_when_overflow_crosses_thirty_seconds(self):
        from pydub import AudioSegment
        from pydub.generators import Sine
        for mode in ("extend_video", "fit_audio"):
            with self.subTest(mode=mode), TemporaryDirectory() as directory:
                job = {"work_dir": Path(directory), "duration": 33,
                       "cues": [{"id": 0, "start": 29, "end": 29.4, "speaker": "S1", "text_vi": "Một."},
                                {"id": 1, "start": 29.5, "end": 31, "speaker": "S2", "text_vi": "Hai."},
                                {"id": 2, "start": 31.1, "end": 32.5, "speaker": "S1", "text_vi": "Ba."}]}
                request = SimpleNamespace(voiceMap={"*": "edge:vi-VN-HoaiMyNeural"}, speechRate=1, timingMode=mode)

                def synthesize(_text, _voice, raw, _voices, **kwargs):
                    with raw.open("wb") as stream:
                        Sine(660, sample_rate=24000).to_audio_segment(duration=2000).export(stream, format="wav")

                with patch("backend.pipeline.synthesize_cue", side_effect=synthesize) as synth:
                    output, preview_cues = create_dubbing(job, request, {}, duration_limit=30)
                    with output.open("rb") as stream:
                        preview_audio = AudioSegment.from_wav(stream)[:30000].raw_data
                    self.assertEqual(job["render_duration"], 30)
                    output, full_cues = create_dubbing(job, request, {})
                    with output.open("rb") as stream:
                        full_audio = AudioSegment.from_wav(stream)[:30000].raw_data
                self.assertEqual(preview_audio, full_audio)
                self.assertEqual(preview_cues, full_cues[:2])
                self.assertEqual(synth.call_count, 3)

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required")
    def test_changing_clone_timing_mode_does_not_reuse_duration_controlled_take(self):
        from pydub.generators import Sine
        with TemporaryDirectory() as directory:
            job = {"work_dir": Path(directory), "duration": 2,
                   "cues": [{"id": 0, "start": .1, "end": 1.5, "speaker": "S1", "text_vi": "Xin chào."}]}
            request = SimpleNamespace(voiceMap={"*": "clone:test"}, speechRate=1, timingMode="fit_audio")

            def clone(_text, _voice, raw, _voices, _target_duration, **kwargs):
                with raw.open("wb") as stream:
                    Sine(660, sample_rate=24000).to_audio_segment(duration=1000).export(stream, format="wav")

            with patch("backend.pipeline.synthesize_verified_clone", side_effect=clone) as synth:
                create_dubbing(job, request, {})
                self.assertIsNotNone(synth.call_args.kwargs["timing_window"])
                request.timingMode = "extend_video"
                create_dubbing(job, request, {})
                self.assertIsNone(synth.call_args.kwargs["timing_window"])
                create_dubbing(job, request, {})
            self.assertEqual(synth.call_count, 2)

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required")
    def test_real_render_handles_overflow_for_presets_and_clones_in_both_modes(self):
        for mode in ("extend_video", "fit_audio"):
            for voice in ("edge:vi-VN-HoaiMyNeural", "clone:test"):
                with self.subTest(mode=mode, voice=voice), TemporaryDirectory() as directory:
                    root = Path(directory)
                    source, music = root / "source.mp4", root / "music.wav"
                    subprocess.run([
                        shutil.which("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "lavfi", "-i", "color=c=blue:s=160x240:d=2:r=30",
                        "-f", "lavfi", "-i", "sine=frequency=220:duration=2",
                        "-c:v", "libx264", "-c:a", "aac", "-shortest", str(source),
                    ], check=True)
                    subprocess.run([
                        shutil.which("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "lavfi", "-i", "sine=frequency=220:duration=2", str(music),
                    ], check=True)

                    def synthesize(text, _voice, raw, _voices, **kwargs):
                        duration = 1.6416 if text == "Một." else .8
                        subprocess.run([
                            shutil.which("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
                            "-f", "lavfi", "-i", f"sine=frequency=660:duration={duration}",
                            "-ar", "24000", "-ac", "1", str(raw),
                        ], check=True)

                    def clone(text, selected, raw, voices, target_duration, **kwargs):
                        synthesize(text, selected, raw, voices)
                        return .99, False

                    job = {"id": "timing", "work_dir": root, "source": source, "duration": 2, "video_size": (160, 240),
                           "cues": [{"id": 0, "start": .1, "end": .5, "speaker": "S1", "text_vi": "Một."},
                                    {"id": 1, "start": .54, "end": 1.7, "speaker": "S2", "text_vi": "Hai."}]}
                    request = SimpleNamespace(voiceMap={"*": voice}, speechRate=1, timingMode=mode, previewOnly=True,
                                              blurRegions=[], subtitleRect=SimpleNamespace(x=.08, y=.72, w=.84, h=.2))
                    with patch("backend.pipeline.synthesize_cue", side_effect=synthesize), \
                            patch("backend.pipeline.synthesize_verified_clone", side_effect=clone), \
                            patch("backend.pipeline.separate_background", return_value=music):
                        render_job(job, request, {})
                        self.assertEqual(job["status"], "preview_ready")
                        preview_duration = media_duration(job["review_result"])
                        request.previewOnly = False
                        render_job(job, request, {})
                    self.assertEqual(job["status"], "complete")
                    final_duration = media_duration(job["result"])
                    self.assertAlmostEqual(preview_duration, final_duration, delta=.08)
                    if mode == "extend_video":
                        self.assertGreater(final_duration, 3)
                        self.assertGreater(job["timing_adjustment"]["added_seconds"], 1)
                    else:
                        self.assertAlmostEqual(final_duration, 2, delta=.08)
                        self.assertEqual(job["timing_adjustment"]["added_seconds"], 0)
