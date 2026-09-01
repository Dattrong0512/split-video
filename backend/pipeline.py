from __future__ import annotations

import asyncio
import base64
import difflib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unicodedata
from pathlib import Path
from typing import Any


class PipelineError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


_whisper_model = None
_omnivoice_model = None
_ocr_model = None
TTS_CACHE_VERSION = 3
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


def _translation_schema() -> dict:
    return {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "original_corrected": {"type": "string"},
                "text_vi": {"type": "string"},
                "speaker": {"type": "string"},
                "gender": {"type": "string"},
                "confidence": {"type": "number"},
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


def _translation_cue_payload(cue: dict) -> dict:
    duration = max(.12, float(cue["end"]) - float(cue["start"]))
    return {
        "id": cue["id"],
        "start_seconds": round(float(cue["start"]), 3),
        "end_seconds": round(float(cue["end"]), 3),
        "duration_seconds": round(duration, 3),
        "target_vi_characters": max(12, round(duration * 18)),
        "whisper_transcript": cue["original"],
    }


def _translation_prompt(cues: list[dict]) -> str:
    return (
        "Dựa trên whisper_transcript tiếng Trung, sửa lỗi nhận dạng rồi viết lại thành bản dịch tiếng Việt chính xác, tự nhiên. "
        "Chỉ dùng nội dung trong transcript; tuyệt đối không sáng tác, suy diễn hoặc thêm kiến thức ngoài lời nói. "
        "original_corrected phải là transcript tiếng Trung đã sửa. Mọi id, start_seconds, end_seconds và "
        "duration_seconds là bất biến: không bỏ, thêm, gộp, tách hoặc đổi thời gian cue. text_vi phải đủ nghĩa, "
        "viết hoa kiểu câu bình thường và hướng tới target_vi_characters để đọc vừa thời lượng gốc; chỉ rút gọn cách diễn đạt, "
        "không được làm mất chủ thể, hành động, con số hay sự kiện chính. Dùng speaker S1 và gender unknown nếu transcript "
        "không có bằng chứng rõ ràng về người nói khác. Dữ liệu cue: " +
        json.dumps([_translation_cue_payload(cue) for cue in cues], ensure_ascii=False)
    )


def gemini_translate(cues: list[dict], api_key: str) -> list[dict]:
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
        response = client.models.generate_content(
            model=os.environ.get("GEMINI_MODEL", DEFAULT_GEMINI_MODEL),
            contents=_translation_prompt(cues),
            config=types.GenerateContentConfig(
                response_mime_type="application/json", response_json_schema=_translation_schema(), temperature=.1,
            ),
        )
        text = response.text or ""
    except Exception as error:
        value = str(error).lower()
        if "api key" in value or "401" in value or "403" in value:
            raise PipelineError("INVALID_GEMINI_KEY", "Gemini API key không hợp lệ hoặc đã bị khóa.") from error
        raise PipelineError("GEMINI_FAILED", f"Gemini không xử lý được transcript: {error}") from error
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


def _old_ocr_rows(result: Any, width: int, height: int) -> list[dict]:
    output = []
    rows = result
    if isinstance(result, list) and result and isinstance(result[0], list):
        first = result[0]
        first_is_row = (len(first) >= 1 and isinstance(first[0], (list, tuple)) and len(first[0]) >= 4
                        and isinstance(first[0][0], (list, tuple)))
        if not first_is_row:
            rows = first
    for row in rows or []:
        if not isinstance(row, (list, tuple)) or not row: continue
        polygon = row[0]
        if not isinstance(polygon, (list, tuple)) or len(polygon) < 4: continue
        xs, ys = [float(p[0]) for p in polygon], [float(p[1]) for p in polygon]
        recognition = row[1] if len(row) > 1 else ()
        text = str(recognition[0]).strip() if isinstance(recognition, (list, tuple)) and recognition else ""
        try:
            score = float(recognition[1]) if len(recognition) > 1 else 0.0
        except (TypeError, ValueError):
            score = 0.0
        output.append({
            "rect": {"x": min(xs)/width, "y": min(ys)/height,
                     "w": (max(xs)-min(xs))/width, "h": (max(ys)-min(ys))/height},
            "text": text, "score": score,
        })
    return output


def _old_ocr_boxes(result: Any, width: int, height: int) -> list[dict]:
    return [row["rect"] for row in _old_ocr_rows(result, width, height)]


def _v3_ocr_rows(results: Any, width: int, height: int) -> list[dict]:
    output = []
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
        texts = data.get("rec_texts", [])
        scores = data.get("rec_scores", [])
        for index, row in enumerate(rows):
            if len(row) < 4:
                continue
            x1, y1, x2, y2 = (float(value) for value in row[:4])
            try:
                score = float(scores[index]) if index < len(scores) else 0.0
            except (TypeError, ValueError):
                score = 0.0
            output.append({
                "rect": {"x": x1/width, "y": y1/height,
                         "w": max(0, x2-x1)/width, "h": max(0, y2-y1)/height},
                "text": str(texts[index]).strip() if index < len(texts) else "", "score": score,
            })
    return output


def _v3_ocr_boxes(results: Any, width: int, height: int) -> list[dict]:
    return [row["rect"] for row in _v3_ocr_rows(results, width, height)]


def _screen_subtitle_text(rows: list[dict]) -> str:
    candidates = []
    for row in rows:
        rect = row["rect"]
        text = re.sub(r"\s+", "", str(row.get("text", "")))
        center_y = rect["y"] + rect["h"] / 2
        center_x = rect["x"] + rect["w"] / 2
        if (float(row.get("score", 0)) >= .35 and re.search(r"[\u3400-\u9fff]", text)
                and .50 <= center_y <= .93 and .18 <= center_x <= .82
                and rect["w"] >= .04 and rect["h"] >= .009):
            candidates.append((center_y, rect["x"], text))
    if not candidates:
        return ""
    candidates.sort()
    lines: list[list[tuple[float, str]]] = []
    line_centers: list[float] = []
    for center_y, x, text in candidates:
        target = next((index for index, value in enumerate(line_centers) if abs(value - center_y) <= .035), None)
        if target is None:
            lines.append([(x, text)]); line_centers.append(center_y)
        else:
            lines[target].append((x, text))
    output = []
    lowest_line = max(line_centers)
    for line_index, line in enumerate(lines):
        if line_centers[line_index] < lowest_line - .14:
            continue
        value = "".join(text for _x, text in sorted(line))
        if value and value not in output:
            output.append(value)
    return "\n".join(output)


def _paddle_ocr():
    global _ocr_model
    if _ocr_model is None:
        from paddleocr import PaddleOCR
        _ocr_model = PaddleOCR(
            lang="ch", ocr_version="PP-OCRv5",
            text_detection_model_name="PP-OCRv5_mobile_det",
            text_recognition_model_name="PP-OCRv5_mobile_rec",
            use_doc_orientation_classify=False, use_doc_unwarping=False,
            use_textline_orientation=False, device="cpu",
        )
    return _ocr_model


def detect_blur_regions(source: Path, duration: float) -> list[dict]:
    width, height = video_size(source)
    try:
        import cv2
    except Exception:
        return ensure_portrait_subtitle_blur([], width, height)
    try:
        ocr = _paddle_ocr()
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
            rows = []
            if ocr is not None:
                try:
                    rows = _v3_ocr_rows(ocr.predict(frame), frame_width, frame_height)
                except Exception:
                    try: rows = _old_ocr_rows(ocr.ocr(frame), frame_width, frame_height)
                    except Exception: rows = []
            boxes = [row["rect"] for row in rows]
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
    update(job, 36, "Gemini đang sửa lời thoại và viết lại tiếng Việt theo thời gian gốc…")
    cues = gemini_translate(cues, job.pop("gemini_key"))
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


def _spoken_tokens(value: str) -> list[str]:
    value = unicodedata.normalize("NFD", str(value).lower())
    value = "".join(character for character in value if unicodedata.category(character) != "Mn")
    return re.findall(r"[^\W_]+", value, flags=re.UNICODE)


def repeated_tts_tail_cutoff(text: str, words: list) -> float | None:
    recognized = []
    for word in words:
        tokens = _spoken_tokens(getattr(word, "word", ""))
        if not tokens:
            continue
        recognized.append({
            "token": tokens[-1], "start": float(word.start), "end": float(word.end),
        })
    if len(recognized) < 2:
        return None
    tail_token = recognized[-1]["token"]
    run_start = len(recognized) - 1
    while run_start > 0 and recognized[run_start - 1]["token"] == tail_token:
        run_start -= 1
    run_length = len(recognized) - run_start
    if run_length < 2:
        return None

    expected = _spoken_tokens(text)
    expected_suffix = 0
    for token in reversed(expected):
        if token != tail_token:
            break
        expected_suffix += 1
    if run_length <= expected_suffix:
        return None

    expected_prefix = expected[:-expected_suffix] if expected_suffix else expected
    recognized_prefix = [item["token"] for item in recognized[:run_start]]
    if expected_prefix:
        matching = sum(
            block.size for block in difflib.SequenceMatcher(None, expected_prefix, recognized_prefix).get_matching_blocks()
        )
        if matching / len(expected_prefix) < .55:
            return None

    if expected_suffix:
        last_kept = recognized[run_start + min(expected_suffix, run_length) - 1]
        return last_kept["end"] + .06
    return max(.05, recognized[run_start]["start"] - .03)


def trim_repeated_tts_tail(path: Path, text: str) -> bool:
    try:
        segments, _ = _whisper().transcribe(
            str(path), language="vi", vad_filter=True, beam_size=1,
            word_timestamps=True, condition_on_previous_text=False,
        )
        words = [word for segment in segments for word in (segment.words or [])]
        cutoff = repeated_tts_tail_cutoff(text, words)
        if cutoff is None or cutoff >= media_duration(path) - .04:
            return False
        cleaned = path.with_name(f"{path.stem}-clean.wav")
        fade_start = max(0, cutoff - .05)
        run([
            ffmpeg(), "-y", "-i", str(path), "-af",
            f"atrim=0:{cutoff:.3f},afade=t=out:st={fade_start:.3f}:d=.05",
            "-ar", "24000", "-ac", "1", str(cleaned),
        ], "VOICE_FAILED")
        cleaned.replace(path)
        return True
    except Exception:
        return False


def plan_dubbing_timeline(
    cues: list[dict], generated_durations: list[float], video_duration: float,
    gap: float = .04, speech_rate: float = 1.0,
) -> list[dict]:
    if len(cues) != len(generated_durations):
        raise ValueError("Mỗi cue phải có đúng một audio TTS")
    timeline = []
    cursor = 0.0
    for cue, generated in zip(cues, generated_durations):
        start = max(float(cue["start"]), cursor)
        original_duration = max(.25, float(cue["end"]) - float(cue["start"]))
        generated = max(.05, float(generated))
        ideal_for_original = generated / original_duration
        automatic_speed = min(1.15, max(.90, ideal_for_original))
        speed = min(1.5, max(.72, automatic_speed * speech_rate))
        end = start + generated / speed
        timeline.append({"start": start, "end": end, "speed": speed})
        cursor = end + gap
    if timeline and timeline[-1]["end"] > video_duration:
        compacted = [dict(item) for item in timeline]
        latest_end = video_duration
        for item in reversed(compacted):
            speech_duration = item["end"] - item["start"]
            item["end"] = min(item["end"], latest_end)
            item["start"] = item["end"] - speech_duration
            latest_end = item["start"] - gap
        if compacted[0]["start"] >= 0:
            timeline = compacted
    return timeline


def _omnivoice():
    global _omnivoice_model
    if _omnivoice_model is None:
        import torch
        from omnivoice import OmniVoice
        if not torch.cuda.is_available(): raise PipelineError("GPU_UNAVAILABLE", "OmniVoice cần GPU Colab.")
        _omnivoice_model = OmniVoice.from_pretrained("k2-fsa/OmniVoice", device_map="cuda:0", dtype=torch.float16)
    return _omnivoice_model


def synthesize_cue(
    text: str, voice: str, raw: Path, voices: dict, target_duration: float | None = None,
) -> None:
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
    generation_options = {"num_step": 32, "postprocess_output": True}
    if target_duration is not None:
        generation_options["duration"] = max(.35, float(target_duration))
    audio = model.generate(
        text=text, language="vi", ref_audio=str(reference["path"]),
        ref_text=reference["transcript"], **generation_options,
    )[0]
    sf.write(str(raw), audio, model.sampling_rate)


def create_dubbing(job: dict, request, voices: dict, duration_limit: float | None = None) -> tuple[Path, list[dict]]:
    from pydub import AudioSegment
    AudioSegment.converter = ffmpeg()
    directory: Path = job["work_dir"] / "tts"; directory.mkdir(exist_ok=True)
    cues = [dict(cue) for cue in job["cues"] if duration_limit is None or float(cue["start"]) < duration_limit]
    total = len(cues)
    cache = job.setdefault("tts_cache", {})
    generated_files = []
    generated_durations = []
    for index, cue in enumerate(cues, 1):
        voice = request.voiceMap.get(cue["speaker"]) or request.voiceMap.get("*") or "edge:vi-VN-HoaiMyNeural"
        cache_key = str(cue["id"])
        cached = cache.get(cache_key, {})
        raw = directory / f"raw-{int(cue['id']):04d}.wav"
        if (cached.get("version") != TTS_CACHE_VERSION or cached.get("voice") != voice
                or cached.get("text") != cue["text_vi"] or not raw.exists()):
            update(job, 62 + index / max(1, total) * 12, f"Đang tạo giọng Việt {index}/{total} ({cue['speaker']})…")
            cue_duration = max(.35, float(cue["end"]) - float(cue["start"]))
            synthesize_cue(cue["text_vi"], voice, raw, voices, target_duration=cue_duration)
            if voice.startswith("clone:"):
                trim_repeated_tts_tail(raw, cue["text_vi"])
            cache[cache_key] = {
                "version": TTS_CACHE_VERSION, "voice": voice,
                "text": cue["text_vi"], "duration": media_duration(raw),
            }
        generated_files.append(raw)
        generated_durations.append(float(cache[cache_key]["duration"]))
    output_duration = min(float(job["duration"]), duration_limit) if duration_limit is not None else float(job["duration"])
    timeline = plan_dubbing_timeline(cues, generated_durations, output_duration, speech_rate=float(request.speechRate))
    rendered_segments = []
    for index, (cue, raw, timing) in enumerate(zip(cues, generated_files, timeline), 1):
        update(job, 74 + index / total * 4, f"Đang căn thời gian giọng Việt {index}/{total}…")
        fitted = directory / f"fit-{int(cue['id']):04d}.wav"
        run([ffmpeg(), "-y", "-i", str(raw), "-af", _atempo(timing["speed"]), "-ar", "24000", "-ac", "1", str(fitted)], "VOICE_FAILED")
        with fitted.open("rb") as fitted_stream:
            segment = AudioSegment.from_file(fitted_stream, format="wav").set_frame_rate(24000).set_channels(1)
        cue["start"] = timing["start"]
        cue["end"] = timing["start"] + len(segment) / 1000
        cue["speech_speed"] = timing["speed"]
        rendered_segments.append((cue["start"], segment))
    final_duration = max(output_duration, max((cue["end"] for cue in cues), default=0))
    final = AudioSegment.silent(duration=math.ceil(final_duration * 1000) + 250, frame_rate=24000).set_channels(1)
    for start, segment in rendered_segments:
        final = final.overlay(segment, position=max(0, round(start * 1000)))
    output = job["work_dir"] / "dubbing.wav"
    with output.open("wb") as output_stream:
        final.export(output_stream, format="wav")
    return output, cues


def original_bed_filter() -> str:
    return (
        "[0:a]volume=1.0[background]"
        ";[1:a]volume=0.12[original_voice]"
        ";[background][original_voice]amix=inputs=2:duration=longest:normalize=0,"
        "alimiter=limit=.95[bed]"
    )


def separate_background(job: dict) -> Path:
    cached = job.get("background")
    if cached and Path(cached).exists():
        return Path(cached)
    output = job["work_dir"] / "separated"
    update(job, 79, "Đang giữ nhạc, hiệu ứng và một ít giọng nói gốc…")
    result = subprocess.run([sys.executable, "-m", "demucs", "--two-stems", "vocals", "-n", "htdemucs", "-o", str(output), str(job["source"])], capture_output=True, text=True)
    candidates = list(output.glob("**/no_vocals.wav"))
    background = candidates[0] if candidates else None
    vocals = background.with_name("vocals.wav") if background is not None else None
    if result.returncode or background is None or vocals is None or not vocals.exists():
        fallback = job["work_dir"] / "background.wav"
        run([ffmpeg(), "-y", "-i", str(job["source"]), "-vn", "-af", "volume=0.12", str(fallback)], "AUDIO_SEPARATION_FAILED")
        job["warning"] = "Demucs không tách được vocal; đã dùng âm thanh gốc ở mức 12%."
        job["background"] = fallback
        return fallback
    bed = job["work_dir"] / "background-with-original-voice.wav"
    run([
        ffmpeg(), "-y", "-i", str(background), "-i", str(vocals),
        "-filter_complex", original_bed_filter(), "-map", "[bed]", str(bed),
    ], "AUDIO_SEPARATION_FAILED")
    job["background"] = bed
    return bed


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


def video_filter(regions: list, ass_path: Path, subtitle_rect=None) -> str:
    parts = []; current = "0:v"
    for index, rect in enumerate(regions):
        base, crop, blur, out = f"base{index}", f"crop{index}", f"blur{index}", f"v{index}"
        parts.append(f"[{current}]split=2[{base}][{crop}]")
        parts.append(f"[{crop}]crop=iw*{rect.w:.7f}:ih*{rect.h:.7f}:iw*{rect.x:.7f}:ih*{rect.y:.7f},gblur=sigma=20:steps=3[{blur}]")
        parts.append(f"[{base}][{blur}]overlay=main_w*{rect.x:.7f}:main_h*{rect.y:.7f}[{out}]")
        current = out
    if subtitle_rect is not None:
        index = len(regions)
        base, crop, blur, panel = f"panelbase{index}", f"panelcrop{index}", f"panelblur{index}", f"panel{index}"
        parts.append(f"[{current}]split=2[{base}][{crop}]")
        parts.append(
            f"[{crop}]crop=iw*{subtitle_rect.w:.7f}:ih*{subtitle_rect.h:.7f}:"
            f"iw*{subtitle_rect.x:.7f}:ih*{subtitle_rect.y:.7f},gblur=sigma=10:steps=2[{blur}]"
        )
        parts.append(
            f"[{base}][{blur}]overlay=main_w*{subtitle_rect.x:.7f}:main_h*{subtitle_rect.y:.7f},"
            f"drawbox=x=iw*{subtitle_rect.x:.7f}:y=ih*{subtitle_rect.y:.7f}:"
            f"w=iw*{subtitle_rect.w:.7f}:h=ih*{subtitle_rect.h:.7f}:color=black@0.26:t=fill[{panel}]"
        )
        current = panel
    escaped = str(ass_path).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
    parts.append(f"[{current}]ass=filename='{escaped}'[vout]")
    return ";".join(parts)


def audio_mix_filter(duration: float) -> str:
    return (
        f"[1:a]volume=0.92,atrim=0:{duration:.3f},asetpts=N/SR/TB[bg]"
        f";[2:a]volume=1.15,atrim=0:{duration:.3f},asetpts=N/SR/TB[dub]"
        f";[bg][dub]amix=inputs=2:duration=first:normalize=0,alimiter=limit=.95,"
        f"atrim=0:{duration:.3f}[aout]"
    )


def encoding_options(dimensions: tuple[int, int], duration: float) -> list[str]:
    maxrate = "3000k" if dimensions[0] * dimensions[1] <= 1_000_000 else "5000k"
    bufsize = "6000k" if maxrate == "3000k" else "10000k"
    return [
        "-t", f"{duration:.3f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
        "-maxrate", maxrate, "-bufsize", bufsize, "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
    ]


def render_job(job: dict, request, voices: dict) -> None:
    preview_only = bool(request.previewOnly)
    limit = min(30.0, float(job["duration"])) if preview_only else None
    update(job, 58, "Đang chuẩn bị bản nghe thử 30 giây…" if preview_only else "Đang chuẩn bị lồng tiếng toàn bộ…",
           status="rendering_preview" if preview_only else "rendering")
    dubbing, rendered_cues = create_dubbing(job, request, voices, limit)
    background = separate_background(job)
    update(job, 91, "Đang xuất bản xem trước 30 giây…" if preview_only else "Đang blur, chèn phụ đề và xuất MP4…")
    dimensions = job.get("video_size") or video_size(job["source"])
    ass = job["work_dir"] / ("review-subtitles.ass" if preview_only else "subtitles.ass")
    write_ass(ass, rendered_cues, request.subtitleRect, dimensions)
    if preview_only:
        review_sequence = int(job.get("review_sequence", 0)) + 1
        job["review_sequence"] = review_sequence
        result = job["work_dir"] / f"review-{review_sequence:03d}.mp4"
    else:
        result = job["work_dir"] / "result.mp4"
    duration = limit if preview_only else float(job["duration"])
    filters = video_filter(request.blurRegions, ass, request.subtitleRect) + ";" + audio_mix_filter(duration)
    run([ffmpeg(), "-y", "-i", str(job["source"]), "-i", str(background), "-i", str(dubbing), "-filter_complex", filters,
         "-map", "[vout]", "-map", "[aout]", *encoding_options(dimensions, duration), str(result)], "RENDER_FAILED")
    if preview_only:
        job["review_result"] = result
        job["review_rate"] = float(request.speechRate)
        update(job, 100, "Bản xem trước 30 giây đã sẵn sàng.", status="preview_ready")
    else:
        job["result"] = result
        update(job, 100, "Hoàn tất. Đang chuẩn bị tải xuống…", status="complete")
