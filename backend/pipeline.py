from __future__ import annotations

import asyncio
import base64
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path
from typing import Any


class PipelineError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


_whisper_model = None
_omnivoice_model = None
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash-lite"


def _gemini_retry_options() -> dict:
    return {
        "attempts": 6,
        "initial_delay": 2.0,
        "max_delay": 30.0,
        "exp_base": 2.0,
        "jitter": 1.0,
        "http_status_codes": [408, 429, 500, 502, 503, 504],
    }


def update(job: dict, progress: float, message: str, **extra: Any) -> None:
    if job.get("cancelled"):
        raise PipelineError("CANCELLED", "Job đã bị hủy.")
    job.update(progress=progress, message=message, **extra)


def ffmpeg() -> str:
    path = shutil.which("ffmpeg")
    if not path:
        raise PipelineError("FFMPEG_MISSING", "Không tìm thấy FFmpeg trong Colab.")
    return path


def run(command: list[str], code: str = "PROCESSING_FAILED") -> subprocess.CompletedProcess:
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode:
        detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "Lệnh xử lý thất bại."
        raise PipelineError(code, detail[:500])
    return result


def media_duration(path: Path) -> float:
    result = run([ffmpeg(), "-hide_banner", "-i", str(path), "-f", "null", "-"], "INVALID_MEDIA")
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr)
    if not match:
        raise PipelineError("INVALID_MEDIA", "Không đọc được thời lượng media.")
    return int(match[1]) * 3600 + int(match[2]) * 60 + float(match[3])


def video_size(path: Path) -> tuple[int, int]:
    probe = shutil.which("ffprobe")
    if probe:
        result = subprocess.run(
            [probe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "json", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        try:
            stream = json.loads(result.stdout)["streams"][0]
            width, height = int(stream["width"]), int(stream["height"])
            if width > 0 and height > 0:
                return width, height
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
            pass
    result = subprocess.run([ffmpeg(), "-hide_banner", "-i", str(path)], capture_output=True, text=True, encoding="utf-8", errors="replace")
    match = re.search(r"Video:.*?[, ](\d{2,5})x(\d{2,5})(?:[, \[])", result.stderr)
    if not match:
        raise PipelineError("INVALID_MEDIA", "Không đọc được kích thước video.")
    return int(match[1]), int(match[2])


def _whisper():
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        _whisper_model = WhisperModel("large-v3-turbo", device="cuda", compute_type="float16")
    return _whisper_model


def download_douyin(job: dict) -> Path:
    from yt_dlp import YoutubeDL

    directory: Path = job["work_dir"]
    cookie_file = directory / "cookies.txt"
    cookie_file.write_text(job.pop("cookie_text"), encoding="utf-8")
    options = {
        "format": "bv*+ba/b", "merge_output_format": "mp4", "outtmpl": str(directory / "source.%(ext)s"),
        "cookiefile": str(cookie_file), "noplaylist": True, "quiet": True, "no_warnings": True,
        "http_headers": {"Referer": "https://www.douyin.com/"},
    }
    try:
        with YoutubeDL(options) as downloader:
            info = downloader.extract_info(job["canonical_url"], download=True)
            path = Path(downloader.prepare_filename(info))
        if path.suffix.lower() != ".mp4" or not path.exists():
            candidates = list(directory.glob("source*.mp4"))
            if candidates: path = max(candidates, key=lambda item: item.stat().st_mtime)
        if not path.exists(): raise RuntimeError("Không tìm thấy file đã tải.")
        return path
    except Exception as error:
        text = str(error).lower()
        if "cookie" in text or "403" in text or "login" in text:
            raise PipelineError("COOKIE_EXPIRED", "Cookie Douyin đã hết hạn hoặc bị từ chối.") from error
        raise PipelineError("DOWNLOAD_FAILED", f"Không tải được video Douyin: {error}") from error
    finally:
        cookie_file.unlink(missing_ok=True)


def extract_assets(source: Path, directory: Path, duration: float) -> tuple[Path, str]:
    audio = directory / "speech.mp3"
    preview = directory / "preview.jpg"
    run([ffmpeg(), "-y", "-i", str(source), "-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k", str(audio)])
    run([ffmpeg(), "-y", "-ss", f"{max(0, duration * .25):.3f}", "-i", str(source), "-frames:v", "1", "-q:v", "2", str(preview)])
    data_url = "data:image/jpeg;base64," + base64.b64encode(preview.read_bytes()).decode("ascii")
    return audio, data_url


def transcribe(audio: Path) -> list[dict]:
    segments, _ = _whisper().transcribe(
        str(audio), language="zh", vad_filter=True, beam_size=5, condition_on_previous_text=True,
    )
    cues = [{"id": index, "start": float(segment.start), "end": max(float(segment.end), float(segment.start) + .12), "original": segment.text.strip()}
            for index, segment in enumerate(segments) if segment.text and segment.text.strip()]
    if not cues:
        raise PipelineError("NO_SPEECH", "Không nhận diện được lời thoại trong video.")
    return cues


def _translation_schema(cue_count: int) -> dict:
    return {
        "type": "array", "minItems": cue_count, "maxItems": cue_count,
        "items": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "id": {"type": "integer"},
                "original_corrected": {"type": "string", "description": "Chinese transcript corrected from the audio"},
                "text_vi": {"type": "string", "description": "Concise, faithful Vietnamese sentence that fits the cue duration"},
                "speaker": {"type": "string"},
                "gender": {"type": "string", "enum": ["male", "female", "unknown"]},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            },
            "required": ["id", "original_corrected", "text_vi", "speaker", "gender", "confidence"],
        },
    }


def _apply_translation_rows(cues: list[dict], rows: Any) -> list[dict]:
    if not isinstance(rows, list):
        raise ValueError("Kết quả không phải JSON array")
    by_id = {int(row["id"]): row for row in rows if isinstance(row, dict)}
    expected_ids = {int(cue["id"]) for cue in cues}
    if len(by_id) != len(rows) or set(by_id) != expected_ids:
        raise ValueError("Gemini đã thay đổi danh sách id")
    for cue in cues:
        row = by_id[int(cue["id"])]
        corrected = str(row["original_corrected"]).strip()
        translated = str(row["text_vi"]).strip()
        if not corrected or not translated:
            raise ValueError("Transcript hoặc bản dịch trống")
        cue["original_corrected"] = corrected
        cue["text_vi"] = translated
        cue["speaker"] = re.sub(r"[^A-Za-z0-9_-]", "", str(row.get("speaker", "S1")))[:20] or "S1"
        gender = str(row.get("gender", "unknown")).lower()
        cue["gender"] = gender if gender in {"male", "female", "unknown"} else "unknown"
        confidence = float(row["confidence"])
        if not 0 <= confidence <= 1:
            raise ValueError("Độ tin cậy nằm ngoài khoảng 0–1")
        cue["confidence"] = confidence
    return cues


def gemini_translate(cues: list[dict], audio: Path, api_key: str) -> list[dict]:
    client = None
    uploaded = None
    try:
        from google import genai
        from google.genai import types
        client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(
                timeout=300_000,
                retry_options=types.HttpRetryOptions(**_gemini_retry_options()),
            ),
        )
        uploaded = client.files.upload(file=str(audio))
        prompt = (
            "Chỉ dùng âm thanh đính kèm để sửa transcript tiếng Trung và dịch; không suy đoán từ hình ảnh, "
            "không bịa hoặc thêm ý không có trong lời nói. Giữ nguyên từng id, không bỏ, thêm, gộp hay tách cue. "
            "Bản dịch tiếng Việt phải đúng nghĩa, tự nhiên, viết hoa kiểu câu bình thường và đủ ngắn để đọc trong "
            "duration_seconds; ưu tiên rút gọn mà không mất ý. Gán người nói nhất quán S1, S2... và chỉ suy đoán "
            "giới tính khi nghe đủ rõ. Dữ liệu cue: " +
            json.dumps([{
                "id": cue["id"], "start_seconds": round(cue["start"], 3),
                "end_seconds": round(cue["end"], 3), "duration_seconds": round(cue["end"] - cue["start"], 3),
                "whisper_transcript": cue["original"],
            } for cue in cues], ensure_ascii=False)
        )
        response = client.models.generate_content(
            model=os.environ.get("GEMINI_MODEL", DEFAULT_GEMINI_MODEL),
            contents=[prompt, uploaded],
            config=types.GenerateContentConfig(
                response_mime_type="application/json", response_json_schema=_translation_schema(len(cues)), temperature=.1,
            ),
        )
        text = response.text or ""
    except Exception as error:
        value = str(error).lower()
        if "api key" in value or "401" in value or "403" in value:
            raise PipelineError("INVALID_GEMINI_KEY", "Gemini API key không hợp lệ hoặc đã bị khóa.") from error
        raise PipelineError("GEMINI_FAILED", f"Gemini không xử lý được audio: {error}") from error
    finally:
        if client is not None and uploaded is not None and getattr(uploaded, "name", None):
            try:
                client.files.delete(name=uploaded.name)
            except Exception:
                pass
    try:
        rows = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I))
        return _apply_translation_rows(cues, rows)
    except Exception as error:
        raise PipelineError("GEMINI_RESPONSE_INVALID", f"Gemini trả JSON không hợp lệ: {error}") from error


def _iou(a: dict, b: dict) -> float:
    x1, y1 = max(a["x"], b["x"]), max(a["y"], b["y"])
    x2, y2 = min(a["x"] + a["w"], b["x"] + b["w"]), min(a["y"] + a["h"], b["y"] + b["h"])
    intersection = max(0, x2 - x1) * max(0, y2 - y1)
    return intersection / max(1e-9, a["w"] * a["h"] + b["w"] * b["h"] - intersection)


def cluster_rectangles(detections: list[tuple[int, dict]], sample_count: int) -> list[dict]:
    clusters: list[dict] = []
    for frame_index, rect in detections:
        target = next((cluster for cluster in clusters if _iou(cluster["rect"], rect) > .18 or
                       abs((cluster["rect"]["y"] + cluster["rect"]["h"] / 2) - (rect["y"] + rect["h"] / 2)) < .035), None)
        if target is None:
            clusters.append({"rect": dict(rect), "frames": {frame_index}, "count": 1})
        else:
            current = target["rect"]
            x1, y1 = min(current["x"], rect["x"]), min(current["y"], rect["y"])
            x2 = max(current["x"] + current["w"], rect["x"] + rect["w"])
            y2 = max(current["y"] + current["h"], rect["y"] + rect["h"])
            target["rect"] = {"x": x1, "y": y1, "w": x2 - x1, "h": y2 - y1}
            target["count"] += 1; target["frames"].add(frame_index)
    output = []
    for cluster in clusters:
        rect = cluster["rect"]
        persistent = len(cluster["frames"]) >= max(2, math.ceil(sample_count * .18))
        subtitle_band = rect["y"] > .52 and len(cluster["frames"]) >= 2
        if not (persistent or subtitle_band): continue
        padding_x, padding_y = .025, .012
        x, y = max(0, rect["x"] - padding_x), max(0, rect["y"] - padding_y)
        output.append({
            "x": x, "y": y, "w": min(1 - x, rect["w"] + padding_x * 2),
            "h": min(1 - y, rect["h"] + padding_y * 2), "_frames": len(cluster["frames"]),
        })
    output.sort(key=lambda item: (item["y"] < .52, -item["_frames"], -item["w"] * item["h"]))
    for item in output:
        item.pop("_frames", None)
    return output[:8]


def ensure_portrait_subtitle_blur(
    regions: list[dict], width: int, height: int, subtitle_evidence: list[dict] | None = None,
) -> list[dict]:
    regions = [dict(region) for region in regions]
    if height <= width * 1.2:
        return regions
    evidence = subtitle_evidence if subtitle_evidence is not None else regions
    credible_lower_band = any(
        region["w"] >= .22 and region["y"] < .82 and region["y"] + region["h"] > .58
        for region in evidence
    )
    if not credible_lower_band:
        regions.insert(0, {"x": .035, "y": .655, "w": .93, "h": .115})
    return regions[:8]


def _old_ocr_boxes(result: Any, width: int, height: int) -> list[dict]:
    boxes = []
    rows = result[0] if isinstance(result, list) and result and isinstance(result[0], list) else result
    for row in rows or []:
        if not isinstance(row, (list, tuple)) or not row: continue
        polygon = row[0]
        if not isinstance(polygon, (list, tuple)) or len(polygon) < 4: continue
        xs, ys = [float(p[0]) for p in polygon], [float(p[1]) for p in polygon]
        boxes.append({"x": min(xs)/width, "y": min(ys)/height, "w": (max(xs)-min(xs))/width, "h": (max(ys)-min(ys))/height})
    return boxes


def _v3_ocr_boxes(results: Any, width: int, height: int) -> list[dict]:
    boxes = []
    for result in results or []:
        payload = getattr(result, "json", result)
        if callable(payload):
            payload = payload()
        if not isinstance(payload, dict):
            continue
        data = payload.get("res", payload)
        rows = data.get("rec_boxes") if isinstance(data, dict) else None
        if rows is None:
            continue
        for row in rows:
            if len(row) < 4:
                continue
            x1, y1, x2, y2 = (float(value) for value in row[:4])
            boxes.append({"x": x1/width, "y": y1/height, "w": max(0, x2-x1)/width, "h": max(0, y2-y1)/height})
    return boxes


def detect_blur_regions(source: Path, duration: float) -> list[dict]:
    width, height = video_size(source)
    try:
        import cv2
    except Exception:
        return ensure_portrait_subtitle_blur([], width, height)
    try:
        from paddleocr import PaddleOCR
        ocr = PaddleOCR(
            lang="ch", ocr_version="PP-OCRv5",
            text_detection_model_name="PP-OCRv5_mobile_det",
            text_recognition_model_name="PP-OCRv5_mobile_rec",
            use_doc_orientation_classify=False, use_doc_unwarping=False,
            use_textline_orientation=False, device="cpu",
        )
    except Exception:
        ocr = None
    capture = cv2.VideoCapture(str(source)); sample_count = 24
    text_detections: list[tuple[int, dict]] = []
    color_detections: list[tuple[int, dict]] = []
    try:
        for index in range(sample_count):
            capture.set(cv2.CAP_PROP_POS_MSEC, (duration * (index + .5) / sample_count) * 1000)
            ok, frame = capture.read()
            if not ok: continue
            frame_height, frame_width = frame.shape[:2]
            boxes = []
            if ocr is not None:
                try:
                    boxes = _v3_ocr_boxes(ocr.predict(frame), frame_width, frame_height)
                except Exception:
                    try: boxes = _old_ocr_boxes(ocr.ocr(frame), frame_width, frame_height)
                    except Exception: boxes = []
            text_detections.extend((index, rect) for rect in boxes if rect["w"] > .015 and rect["h"] > .008)
            hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
            mask = cv2.inRange(hsv, (85, 55, 45), (140, 255, 255))
            contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            color_boxes = []
            for x, y, w, h in (cv2.boundingRect(contour) for contour in contours):
                if w * h > frame_width * frame_height * .00012 and w > 8 and h > 5:
                    color_boxes.append({"x": x/frame_width, "y": y/frame_height, "w": w/frame_width, "h": h/frame_height})
            color_detections.extend((index, rect) for rect in color_boxes if rect["w"] > .015 and rect["h"] > .008)
    finally: capture.release()
    text_regions = cluster_rectangles(text_detections, sample_count)
    color_regions = cluster_rectangles(color_detections, sample_count)
    regions = (text_regions + color_regions)[:8]
    return ensure_portrait_subtitle_blur(regions, width, height, text_regions)


def analyze_job(job: dict, _voices: dict) -> None:
    update(job, 4, "Đang tải video Douyin…", status="downloading")
    source = download_douyin(job); job["source"] = source
    update(job, 16, "Đang tách audio và ảnh xem trước…", status="analyzing")
    duration = media_duration(source); dimensions = video_size(source); audio, preview = extract_assets(source, job["work_dir"], duration)
    update(job, 24, "Whisper đang nhận diện lời thoại…")
    cues = transcribe(audio)
    update(job, 36, "Gemini đang dịch và phân biệt người nói…")
    cues = gemini_translate(cues, audio, job.pop("gemini_key"))
    regions = []
    if job["blur_mode"] == "auto":
        update(job, 46, "Đang tự tìm subtitle gốc và watermark xanh…")
        regions = detect_blur_regions(source, duration)
    speakers_by_id = {}
    for cue in cues: speakers_by_id.setdefault(cue["speaker"], cue["gender"])
    speakers = [{"id": key, "gender": value} for key, value in sorted(speakers_by_id.items())]
    job["cues"] = cues; job["duration"] = duration; job["video_size"] = dimensions
    analysis = {"previewDataUrl": preview, "blurRegions": regions, "subtitleRect": {"x": .08, "y": .78, "w": .84, "h": .16}, "speakers": speakers, "cueCount": len(cues)}
    update(job, 55, "Đã phân tích. Hãy chọn giọng và kiểm tra khung.", status="analysis_ready", analysis=analysis)


def probe_reference(path: Path) -> tuple[str, float]:
    duration = media_duration(path)
    if duration < 2.8 or duration > 10.7:
        raise PipelineError("INVALID_VOICE", "Clip clone phải dài từ 3 đến 10 giây.")
    segments, _ = _whisper().transcribe(str(path), vad_filter=True, beam_size=3)
    transcript = " ".join(segment.text.strip() for segment in segments if segment.text.strip())
    if not transcript: raise PipelineError("INVALID_VOICE", "Clip clone không có lời nói rõ ràng.")
    return transcript, duration


def _atempo(speed: float) -> str:
    parts = []
    while speed > 2: parts.append("atempo=2"); speed /= 2
    while speed < .5: parts.append("atempo=.5"); speed /= .5
    parts.append(f"atempo={speed:.6f}")
    return ",".join(parts)


def plan_dubbing_timeline(cues: list[dict], generated_durations: list[float], video_duration: float, gap: float = .04) -> list[dict]:
    if len(cues) != len(generated_durations):
        raise ValueError("Mỗi cue phải có đúng một audio TTS")
    timeline = []
    cursor = 0.0
    for index, (cue, generated) in enumerate(zip(cues, generated_durations)):
        start = max(float(cue["start"]), cursor)
        next_start = float(cues[index + 1]["start"]) if index + 1 < len(cues) else video_duration
        available = max(.25, max(float(cue["end"]), next_start - gap) - start)
        original_duration = max(.25, float(cue["end"]) - float(cue["start"]))
        generated = max(.05, float(generated))
        ideal_for_original = generated / original_duration
        if ideal_for_original < .90:
            speed = 1.0
        elif ideal_for_original <= 1.15:
            speed = ideal_for_original
        else:
            speed = min(1.15, max(1.0, generated / available))
        end = start + generated / speed
        timeline.append({"start": start, "end": end, "speed": speed})
        cursor = end + gap
    return timeline


def _omnivoice():
    global _omnivoice_model
    if _omnivoice_model is None:
        import torch
        from omnivoice import OmniVoice
        if not torch.cuda.is_available(): raise PipelineError("GPU_UNAVAILABLE", "OmniVoice cần GPU Colab.")
        _omnivoice_model = OmniVoice.from_pretrained("k2-fsa/OmniVoice", device_map="cuda:0", dtype=torch.float16)
    return _omnivoice_model


def synthesize_cue(text: str, voice: str, raw: Path, voices: dict) -> None:
    if voice.startswith("edge:"):
        import edge_tts
        asyncio.run(edge_tts.Communicate(text, voice=voice[5:]).save(str(raw.with_suffix(".mp3"))))
        raw.with_suffix(".mp3").replace(raw)
        return
    if not voice.startswith("clone:") or voice[6:] not in voices:
        raise PipelineError("VOICE_FAILED", "Giọng đã chọn không tồn tại trong phiên Colab.")
    import soundfile as sf
    reference = voices[voice[6:]]
    model = _omnivoice()
    audio = model.generate(text=text, language="vi", ref_audio=str(reference["path"]), ref_text=reference["transcript"], num_step=16)[0]
    sf.write(str(raw), audio, model.sampling_rate)


def create_dubbing(job: dict, request, voices: dict) -> Path:
    from pydub import AudioSegment
    AudioSegment.converter = ffmpeg()
    directory: Path = job["work_dir"] / "tts"; directory.mkdir(exist_ok=True)
    total = len(job["cues"])
    generated_files = []
    generated_durations = []
    for index, cue in enumerate(job["cues"], 1):
        update(job, 62 + index / total * 12, f"Đang tạo giọng Việt {index}/{total} ({cue['speaker']})…")
        voice = request.voiceMap.get(cue["speaker"]) or request.voiceMap.get("*") or "edge:vi-VN-HoaiMyNeural"
        raw = directory / f"{index:04d}.wav"
        synthesize_cue(cue["text_vi"], voice, raw, voices)
        generated_files.append(raw)
        generated_durations.append(media_duration(raw))
    timeline = plan_dubbing_timeline(job["cues"], generated_durations, float(job["duration"]))
    rendered_segments = []
    for index, (cue, raw, timing) in enumerate(zip(job["cues"], generated_files, timeline), 1):
        update(job, 74 + index / total * 4, f"Đang căn thời gian giọng Việt {index}/{total}…")
        fitted = directory / f"{index:04d}-fit.wav"
        run([ffmpeg(), "-y", "-i", str(raw), "-af", _atempo(timing["speed"]), "-ar", "24000", "-ac", "1", str(fitted)], "VOICE_FAILED")
        segment = AudioSegment.from_file(fitted).set_frame_rate(24000).set_channels(1)
        cue["start"] = timing["start"]
        cue["end"] = timing["start"] + len(segment) / 1000
        cue["speech_speed"] = timing["speed"]
        rendered_segments.append((cue["start"], segment))
    final_duration = max(float(job["duration"]), max(cue["end"] for cue in job["cues"]))
    final = AudioSegment.silent(duration=math.ceil(final_duration * 1000) + 250, frame_rate=24000).set_channels(1)
    for start, segment in rendered_segments:
        final = final.overlay(segment, position=max(0, round(start * 1000)))
    output = job["work_dir"] / "dubbing.wav"; final.export(output, format="wav")
    return output


def separate_background(job: dict) -> Path:
    output = job["work_dir"] / "separated"
    update(job, 79, "Đang tách lời gốc để giữ nhạc và hiệu ứng…")
    result = subprocess.run([sys.executable, "-m", "demucs", "--two-stems", "vocals", "-n", "htdemucs", "-o", str(output), str(job["source"])], capture_output=True, text=True)
    candidates = list(output.glob("**/no_vocals.wav"))
    if result.returncode or not candidates:
        fallback = job["work_dir"] / "background.wav"
        run([ffmpeg(), "-y", "-i", str(job["source"]), "-vn", "-af", "volume=0.12", str(fallback)], "AUDIO_SEPARATION_FAILED")
        job["warning"] = "Demucs không tách được vocal; đã dùng âm thanh gốc ở mức 12%."
        return fallback
    return candidates[0]


def ass_time(value: float) -> str:
    centiseconds = max(0, round(value * 100)); hours, rem = divmod(centiseconds, 360000); minutes, rem = divmod(rem, 6000); seconds, cs = divmod(rem, 100)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{cs:02d}"


def _wrapped_ass_text(value: str, max_chars: int) -> str:
    normalized = " ".join(str(value).split())
    lines = textwrap.wrap(normalized, width=max_chars, break_long_words=False, break_on_hyphens=False) or [""]
    return "\\N".join(lines)


def write_ass(path: Path, cues: list[dict], rect, dimensions: tuple[int, int] = (1920, 1080)) -> None:
    width, height = dimensions
    size = max(24, min(64, round(min(width, height) * .052)))
    x = round((rect.x + rect.w/2) * width); y = round((rect.y + rect.h/2) * height)
    max_chars = max(16, int(rect.w * width / (size * .56)))
    header = f"[Script Info]\nScriptType: v4.00+\nPlayResX: {width}\nPlayResY: {height}\nWrapStyle: 0\nScaledBorderAndShadow: yes\n\n[V4+ Styles]\nFormat: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding\n"
    header += f"Style: Vietnamese,Noto Sans,{size},&H00FFFFFF,&H000000FF,&H00101010,&H80000000,-1,0,0,0,100,100,0,0,1,2,1,2,20,20,20,1\n\n[Events]\nFormat: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n"
    events = []
    for cue in cues:
        text = cue["text_vi"].replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")
        text = _wrapped_ass_text(text, max_chars)
        events.append(f"Dialogue: 0,{ass_time(cue['start'])},{ass_time(cue['end'])},Vietnamese,,0,0,0,,{{\\an5\\q2\\pos({x},{y})}}{text}")
    path.write_text(header + "\n".join(events) + "\n", encoding="utf-8-sig")


def video_filter(regions: list, ass_path: Path) -> str:
    parts = []; current = "0:v"
    for index, rect in enumerate(regions):
        base, crop, blur, out = f"base{index}", f"crop{index}", f"blur{index}", f"v{index}"
        parts.append(f"[{current}]split=2[{base}][{crop}]")
        parts.append(f"[{crop}]crop=iw*{rect.w:.7f}:ih*{rect.h:.7f}:iw*{rect.x:.7f}:ih*{rect.y:.7f},gblur=sigma=20:steps=3[{blur}]")
        parts.append(f"[{base}][{blur}]overlay=main_w*{rect.x:.7f}:main_h*{rect.y:.7f}[{out}]")
        current = out
    escaped = str(ass_path).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
    parts.append(f"[{current}]ass=filename='{escaped}'[vout]")
    return ";".join(parts)


def render_job(job: dict, request, voices: dict) -> None:
    update(job, 58, "Đang chuẩn bị lồng tiếng…", status="rendering")
    dubbing = create_dubbing(job, request, voices)
    background = separate_background(job)
    update(job, 91, "Đang blur, chèn phụ đề và xuất MP4…")
    dimensions = job.get("video_size") or video_size(job["source"])
    ass = job["work_dir"] / "subtitles.ass"; write_ass(ass, job["cues"], request.subtitleRect, dimensions)
    result = job["work_dir"] / "result.mp4"
    filters = video_filter(request.blurRegions, ass) + ";[1:a]volume=0.92[bg];[2:a]volume=1.15[dub];[bg][dub]amix=inputs=2:duration=longest:normalize=0,alimiter=limit=.95[aout]"
    run([ffmpeg(), "-y", "-i", str(job["source"]), "-i", str(background), "-i", str(dubbing), "-filter_complex", filters,
         "-map", "[vout]", "-map", "[aout]", "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-shortest", str(result)], "RENDER_FAILED")
    job["result"] = result
    update(job, 100, "Hoàn tất. Đang chuẩn bị tải xuống…", status="complete")
