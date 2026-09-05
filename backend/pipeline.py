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
import time
import unicodedata
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class PipelineError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


_whisper_model = None
_omnivoice_model = None
_ocr_model = None
TTS_CACHE_VERSION = 8
MIN_AUTO_SPEED = .75
MAX_AUTO_SPEED = 1.08
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


def allowed_douyin_media_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        domains = ("douyinvod.com", "douyin.com", "bytecdn.cn", "ibytedtos.com", "bytedance.com")
        return (parsed.scheme == "https" and parsed.port in (None, 443)
                and not parsed.username and not parsed.password
                and not any(char.isspace() for char in value)
                and any(host == domain or host.endswith("." + domain) for domain in domains))
    except (TypeError, ValueError):
        return False


class DouyinMediaRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not allowed_douyin_media_url(newurl):
            raise PipelineError("DOWNLOAD_FAILED", "Link media chuyển hướng đến máy chủ không được hỗ trợ.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download_browser_media(job: dict, media_url: str, user_agent: str) -> Path:
    if not allowed_douyin_media_url(media_url):
        raise PipelineError("DOWNLOAD_FAILED", "Link media không thuộc máy chủ video Douyin được hỗ trợ.")
    directory = job["work_dir"]
    temporary = directory / "browser-media.part"
    output = directory / "source.mp4"
    headers = {"Referer": "https://www.douyin.com/"}
    if user_agent:
        headers["User-Agent"] = user_agent
    opener = build_opener(DouyinMediaRedirectHandler())
    maximum = 1024 * 1024 * 1024
    deadline = time.monotonic() + 600
    try:
        with opener.open(Request(media_url, headers=headers), timeout=30) as response:
            if not allowed_douyin_media_url(response.geturl()):
                raise PipelineError("DOWNLOAD_FAILED", "Địa chỉ media không hợp lệ.")
            if response.status != 200 or "text/html" in response.headers.get("Content-Type", "").lower():
                raise PipelineError("DOWNLOAD_FAILED", "Douyin không trả về file video.")
            expected_size = int(response.headers.get("Content-Length", "0"))
            if expected_size > maximum:
                raise PipelineError("DOWNLOAD_FAILED", "Video vượt giới hạn tải 1 GB.")
            size = 0
            with temporary.open("wb") as stream:
                while chunk := response.read(1024 * 1024):
                    if job.get("cancelled"):
                        raise PipelineError("CANCELLED", "Job đã bị hủy.")
                    size += len(chunk)
                    if size > maximum or time.monotonic() > deadline:
                        raise PipelineError("DOWNLOAD_FAILED", "Video quá lớn hoặc tải quá lâu.")
                    stream.write(chunk)
            if expected_size and size != expected_size:
                raise PipelineError("DOWNLOAD_FAILED", "File video tải chưa đầy đủ.")
        # Validate and remux both tracks; a video-only browser stream needs the extractor fallback.
        run([ffmpeg(), "-y", "-protocol_whitelist", "file,pipe", "-f", "mov", "-i", str(temporary), "-map", "0:v:0", "-map", "0:a:0",
             "-c", "copy", "-movflags", "+faststart", str(output)], "DOWNLOAD_FAILED")
        return output
    except Exception:
        output.unlink(missing_ok=True)
        raise
    finally:
        temporary.unlink(missing_ok=True)


def download_douyin(job: dict) -> Path:

    directory: Path = job["work_dir"]
    cookie_file = directory / "cookies.txt"
    cookie_text = job.pop("cookie_text")
    media_url = job.pop("media_url", None)
    user_agent = job.pop("browser_user_agent", "")
    headers = {"Referer": "https://www.douyin.com/"}
    if user_agent:
        headers["User-Agent"] = user_agent
    options = {
        "format": "bv*+ba/b", "merge_output_format": "mp4", "outtmpl": str(directory / "source.%(ext)s"),
        "cookiefile": str(cookie_file), "noplaylist": True, "quiet": True, "no_warnings": True,
        "http_headers": headers, "socket_timeout": 30, "retries": 2,
    }
    try:
        if media_url and allowed_douyin_media_url(media_url):
            update(job, 5, "Đang tải trực tiếp media của video đang phát trong Chrome…")
            try:
                return download_browser_media(job, media_url, user_agent)
            except PipelineError as error:
                if error.code == "CANCELLED":
                    raise
            except Exception:
                pass
            update(job, 5, "Link media chưa tải được từ Colab. Đang thử bộ tải Douyin…")
        from yt_dlp import YoutubeDL
        cookie_file.write_text(cookie_text, encoding="utf-8")
        with YoutubeDL(options) as downloader:
            info = downloader.extract_info(job["canonical_url"], download=True)
            path = Path(downloader.prepare_filename(info))
        if path.suffix.lower() != ".mp4" or not path.exists():
            candidates = list(directory.glob("source*.mp4"))
            if candidates: path = max(candidates, key=lambda item: item.stat().st_mtime)
        if not path.exists(): raise RuntimeError("Không tìm thấy file đã tải.")
        return path
    except Exception as error:
        if isinstance(error, PipelineError) and error.code == "CANCELLED":
            raise
        text = str(error).lower()
        if any(marker in text for marker in ("fresh cookies", "403", "429", "verify", "captcha", "login", "sign in", "web detail json")):
            raise PipelineError("DOUYIN_ACCESS_BLOCKED", "Douyin không trả dữ liệu video cho Colab; chưa thể kết luận cookie hết hạn. Hãy phát video rồi thử phân tích lại.") from error
        raise PipelineError("DOWNLOAD_FAILED", "Không tải được video Douyin. Hãy mở lại video để cập nhật link media rồi thử lại.") from error
    finally:
        cookie_file.unlink(missing_ok=True)


def extract_assets(source: Path, directory: Path, duration: float) -> tuple[Path, str]:
    audio = directory / "speech.mp3"
    preview = directory / "preview.jpg"
    run([ffmpeg(), "-y", "-i", str(source), "-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k", str(audio)])
    run([ffmpeg(), "-y", "-ss", f"{max(0, duration * .25):.3f}", "-i", str(source), "-frames:v", "1", "-q:v", "2", str(preview)])
    data_url = "data:image/jpeg;base64," + base64.b64encode(preview.read_bytes()).decode("ascii")
    return audio, data_url


def create_browser_preview(source: Path, directory: Path) -> Path:
    output = directory / "browser-preview.mp4"
    run([
        ffmpeg(), "-y", "-i", str(source),
        "-map", "0:v:0", "-map", "0:a:0?", "-sn",
        "-vf", "scale=1280:720:force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "30", "-pix_fmt", "yuv420p",
        "-maxrate", "1500k", "-bufsize", "3000k",
        "-force_key_frames", "expr:gte(t,n_forced*2)",
        "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(output),
    ], "PREVIEW_ENCODING_FAILED")
    return output


def transcribe(audio: Path) -> list[dict]:
    segments, _ = _whisper().transcribe(
        str(audio), language="zh", vad_filter=True, beam_size=5, condition_on_previous_text=True,
    )
    cues = [{"id": index, "start": float(segment.start), "end": max(float(segment.end), float(segment.start) + .12), "original": segment.text.strip()}
            for index, segment in enumerate(segments) if segment.text and segment.text.strip()]
    if not cues:
        raise PipelineError("NO_SPEECH", "Không nhận diện được lời thoại trong video.")
    return cues


def _translation_schema(speaker_count: int = 1) -> dict:
    speaker_count = max(1, min(4, int(speaker_count)))
    properties = {
        "source_ids": {"type": "array", "items": {"type": "integer"}},
        "original_corrected": {"type": "string"},
        "text_vi": {"type": "string"},
        "confidence": {"type": "number"},
    }
    required = ["source_ids", "original_corrected", "text_vi", "confidence"]
    if speaker_count > 1:
        properties.update({"speaker": {"type": "string"}, "gender": {"type": "string"}})
        required.extend(["speaker", "gender"])
    return {
        "type": "array",
        "items": {
            "type": "object",
            "properties": properties,
            "required": required,
        },
    }


def _apply_translation_rows(cues: list[dict], rows: Any, speaker_count: int = 1) -> list[dict]:
    if not isinstance(rows, list):
        raise ValueError("Kết quả không phải JSON array")
    source_by_id = {int(cue["id"]): cue for cue in cues}
    expected_ids = [int(cue["id"]) for cue in cues]
    normalized_rows = []
    flattened_ids = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Mỗi câu dịch phải là JSON object")
        raw_ids = row.get("source_ids", [row["id"]] if "id" in row else None)
        if not isinstance(raw_ids, list) or not raw_ids:
            raise ValueError("Mỗi câu dịch phải có source_ids")
        source_ids = [int(value) for value in raw_ids]
        if any(value not in source_by_id for value in source_ids):
            raise ValueError("Gemini đã tạo source id không tồn tại")
        grouped_sources = [source_by_id[value] for value in source_ids]
        if any(float(right["start"]) - float(left["end"]) > 1.5 for left, right in zip(grouped_sources, grouped_sources[1:])):
            raise ValueError("Gemini đã gộp lời thoại qua khoảng nghỉ quá dài")
        normalized_rows.append((row, source_ids, grouped_sources))
        flattened_ids.extend(source_ids)
    if flattened_ids != expected_ids:
        raise ValueError("Gemini phải dùng mỗi source id đúng một lần và đúng thứ tự")
    speaker_count = max(1, min(4, int(speaker_count)))
    allowed_speakers = {f"S{index}" for index in range(1, speaker_count + 1)}
    translated_cues = []
    for row, source_ids, grouped_sources in normalized_rows:
        corrected = str(row["original_corrected"]).strip()
        translated = str(row["text_vi"]).strip()
        if not corrected or not translated:
            raise ValueError("Transcript hoặc bản dịch trống")
        group_duration = float(grouped_sources[-1]["end"]) - float(grouped_sources[0]["start"])
        # Gemini can merge source cues but cannot split one Whisper source id.
        # Reject an overlong merge while preserving a long atomic source cue.
        if len(source_ids) > 1 and group_duration > 4.8:
            raise ValueError("Một subtitle không được vượt quá 4,8 giây thời lượng lời nói")
        sentence_endings = re.findall(r"(?:[!?。？！]+|(?<!\d)\.)(?=\s|$)", translated)
        if len(sentence_endings) > 1:
            raise ValueError("Mỗi subtitle chỉ được chứa một câu nói hoàn chỉnh")
        normalized_translation = " ".join(translated.split())
        max_characters = max(28, min(56, round(group_duration * 18)))
        if len(normalized_translation) > max_characters:
            raise ValueError(f"Subtitle quá dài ({len(normalized_translation)}/{max_characters} ký tự)")
        cue = {
            "id": source_ids[0], "source_ids": source_ids,
            "start": float(grouped_sources[0]["start"]), "end": float(grouped_sources[-1]["end"]),
            "original": " ".join(str(source["original"]).strip() for source in grouped_sources),
            "original_corrected": corrected, "text_vi": translated,
        }
        if speaker_count == 1:
            cue["speaker"] = "S1"
            cue["gender"] = "unknown"
        else:
            speaker = re.sub(r"[^A-Za-z0-9_-]", "", str(row.get("speaker", "S1"))).upper()[:20] or "S1"
            cue["speaker"] = speaker if speaker in allowed_speakers else "S1"
            gender = str(row.get("gender", "unknown")).lower()
            cue["gender"] = gender if gender in {"male", "female", "unknown"} else "unknown"
        confidence = float(row["confidence"])
        if not 0 <= confidence <= 1:
            raise ValueError("Độ tin cậy nằm ngoài khoảng 0–1")
        cue["confidence"] = confidence
        translated_cues.append(cue)
    if speaker_count > 1 and {cue["speaker"] for cue in translated_cues} != allowed_speakers:
        raise ValueError(f"Gemini không dùng đủ đúng {speaker_count} vai nói")
    return translated_cues


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


def _translation_prompt(cues: list[dict], speaker_count: int = 1) -> str:
    speaker_count = max(1, min(4, int(speaker_count)))
    if speaker_count == 1:
        speaker_instruction = "Chỉ sửa transcript và viết lại phụ đề; không phân vai, không suy đoán nhân vật hay giới tính. "
    else:
        allowed = ", ".join(f"S{index}" for index in range(1, speaker_count + 1))
        speaker_instruction = (
            f"Phải phân thành đúng {speaker_count} vai nói bằng các mã {allowed}; không dùng mã khác. "
            "giữ cùng một speaker cho cùng một nhân vật/góc nhìn xuyên suốt video. "
            "Dùng gender unknown nếu transcript không có bằng chứng rõ ràng về giới tính người nói. "
        )
    return (
        "Đọc toàn bộ các cue như một transcript liên tục. Dựa trên ngữ cảnh trước/sau để sửa lỗi nhận dạng và từ đồng âm "
        "(ví dụ phân biệt 掉 và 钓 khi chủ đề là câu cá), rồi viết lại thành bản dịch tiếng Việt chính xác, tự nhiên. "
        "Chỉ dùng nội dung trong transcript; tuyệt đối không sáng tác, suy diễn hoặc thêm kiến thức ngoài lời nói. "
        "Được gộp các cue liền kề thành một câu nói hoàn chỉnh theo ngữ nghĩa và dấu câu, thay vì coi mỗi ranh giới Whisper "
        "là hết câu. Mỗi output phải có source_ids chứa các id liền kề; toàn bộ id đầu vào phải xuất hiện đúng một lần, "
        "đúng thứ tự. Mỗi output chỉ chứa một câu, tối đa 4,8 giây, tối đa 56 ký tự tiếng Việt và không gộp nhiều lượt "
        "đối thoại. Không gộp qua khoảng im lặng dài hoặc giữa hai người nói. original_corrected phải là transcript "
        "tiếng Trung đã sửa của cả nhóm. text_vi phải đủ câu, đủ nghĩa, viết hoa kiểu câu bình thường và hướng tới tổng "
        "target_vi_characters của các source_ids để đọc vừa toàn bộ thời lượng nhóm; chỉ rút gọn cách diễn đạt, "
        "không được làm mất chủ thể, hành động, con số hay sự kiện chính. " + speaker_instruction + "Dữ liệu cue: " +
        json.dumps([_translation_cue_payload(cue) for cue in cues], ensure_ascii=False)
    )


def gemini_translate(cues: list[dict], api_key: str, speaker_count: int = 1) -> list[dict]:
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
    except Exception as error:
        value = str(error).lower()
        if "api key" in value or "401" in value or "403" in value:
            raise PipelineError("INVALID_GEMINI_KEY", "Gemini API key không hợp lệ hoặc đã bị khóa.") from error
        raise PipelineError("GEMINI_FAILED", f"Gemini không xử lý được transcript: {error}") from error
    validation_error = None
    base_prompt = _translation_prompt(cues, speaker_count)
    prompt = base_prompt
    for _attempt in range(3):
        try:
            response = client.models.generate_content(
                model=os.environ.get("GEMINI_MODEL", DEFAULT_GEMINI_MODEL),
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_json_schema=_translation_schema(speaker_count), temperature=.1,
                ),
            )
        except Exception as error:
            value = str(error).lower()
            if "api key" in value or "401" in value or "403" in value:
                raise PipelineError("INVALID_GEMINI_KEY", "Gemini API key không hợp lệ hoặc đã bị khóa.") from error
            raise PipelineError("GEMINI_FAILED", f"Gemini không xử lý được transcript: {error}") from error
        try:
            text = response.text or ""
            rows = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I))
            return _apply_translation_rows(cues, rows, speaker_count)
        except Exception as error:
            validation_error = error
            prompt = (
                base_prompt
                + "\n\nKết quả trước không vượt qua kiểm tra bắt buộc. "
                + f"Lỗi cần sửa: {error}. "
                + "Hãy tạo lại TOÀN BỘ JSON array từ dữ liệu cue gốc, sửa chính xác lỗi trên; "
                + "không giải thích và không lặp lại JSON sai."
            )
    raise PipelineError(
        "GEMINI_RESPONSE_INVALID",
        f"Gemini trả kết quả không đúng cấu trúc sau 3 lần tự sửa: {validation_error}",
    ) from validation_error


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
    update(job, 16, "Đang tạo video xem trước tương thích Chrome…", status="analyzing")
    duration = media_duration(source); dimensions = video_size(source); audio, preview = extract_assets(source, job["work_dir"], duration)
    job["browser_preview"] = create_browser_preview(source, job["work_dir"])
    update(job, 24, "Whisper đang nhận diện lời thoại…")
    cues = transcribe(audio)
    update(job, 36, "Gemini đang sửa lời thoại và viết lại tiếng Việt theo thời gian gốc…")
    cues = gemini_translate(cues, job.pop("gemini_key"), job.get("voice_count", 1))
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
    if not math.isfinite(speed) or speed <= 0:
        raise ValueError("Tốc độ âm thanh phải là số dương hữu hạn")
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


def _max_consecutive_tokens(tokens: list[str], token: str) -> int:
    best = run = 0
    for item in tokens:
        run = run + 1 if item == token else 0
        best = max(best, run)
    return best


def _repetition_penalty(expected: list[str], recognized: list[str]) -> bool:
    for token in set(recognized):
        recognized_run = _max_consecutive_tokens(recognized, token)
        if recognized_run >= 3 and recognized_run > _max_consecutive_tokens(expected, token):
            return True
    return False


def tts_text_score(expected_text: str, recognized_text: str) -> float:
    expected = _spoken_tokens(expected_text)
    recognized = _spoken_tokens(recognized_text)
    if not expected or not recognized:
        return 0.0
    matcher = difflib.SequenceMatcher(None, expected, recognized)
    blocks = [block for block in matcher.get_matching_blocks() if block.size]
    matching = sum(block.size for block in blocks)
    score = matching / max(len(expected), len(recognized))

    if _repetition_penalty(expected, recognized):
        return min(score, .35)

    last_expected_match_end = max((block.a + block.size for block in blocks), default=0)
    if len(expected) - last_expected_match_end >= 2:
        return min(score, .4)
    last_match_end = max((block.b + block.size for block in blocks), default=0)
    if len(recognized) - last_match_end >= 2:
        return min(score, .4)
    return score


def tts_transcript_score(path: Path, text: str) -> float:
    try:
        segments, _ = _whisper().transcribe(
            str(path), language="vi", vad_filter=True, beam_size=1,
            word_timestamps=False, condition_on_previous_text=False,
        )
        recognized = " ".join(segment.text for segment in segments)
        return tts_text_score(text, recognized)
    except Exception:
        return 0.0


def synthesize_verified_clone(
    text: str, voice: str, raw: Path, voices: dict, target_duration: float,
    attempts: int = 3, timing_window: float | None = None,
) -> tuple[float, bool]:
    candidates = []
    best_path = None
    best_score = -1.0
    best_distance = math.inf
    try:
        for attempt in range(attempts):
            candidate = raw.with_name(f"{raw.stem}-attempt-{attempt + 1}.wav")
            candidates.append(candidate)
            # Retry duration-controlled generation only after hearing a natural take.
            synthesize_cue(text, voice, candidate, voices,
                           target_duration=target_duration if timing_window is not None and attempt else None)
            trim_repeated_tts_tail(candidate, text)
            score = tts_transcript_score(candidate, text)
            if score >= .86 and timing_window is not None:
                duration = media_duration(candidate)
                distance = abs(duration - target_duration)
                if best_score < .86 or distance < best_distance:
                    best_path, best_score, best_distance = candidate, score, distance
                if duration > timing_window * MAX_AUTO_SPEED:
                    continue
                if duration < target_duration * MIN_AUTO_SPEED and attempt < attempts - 1:
                    continue
                best_path, best_score = candidate, score
                break
            if score > best_score:
                best_path, best_score = candidate, score
            if score >= .86:
                break
        if best_path is not None and best_score >= .86:
            shutil.copyfile(best_path, raw)
            return best_score, False
        raise PipelineError(
            "VOICE_FAILED",
            f"Giọng clone đã chọn không đọc đúng cue sau {attempts} lần thử (score {best_score:.2f}); không tự đổi sang giọng khác.",
        )
    finally:
        for candidate in candidates:
            candidate.unlink(missing_ok=True)


def plan_dubbing_timeline(
    cues: list[dict], generated_durations: list[float], video_duration: float,
    gap: float = .04, speech_rate: float = 1.0, next_cue_start: float | None = None,
) -> list[dict]:
    if len(cues) != len(generated_durations):
        raise ValueError("Mỗi cue phải có đúng một audio TTS")
    if not math.isfinite(speech_rate) or not .8 <= speech_rate <= 1.4:
        raise ValueError("Tốc độ giọng phải nằm trong khoảng 0,80–1,40")
    windows = dubbing_windows(cues, video_duration, gap, next_cue_start)
    timeline = []
    for index, (cue, generated, window) in enumerate(zip(cues, generated_durations, windows)):
        start = float(cue["start"])
        generated = float(generated)
        if not math.isfinite(generated) or generated <= 0:
            raise ValueError("Audio TTS phải có thời lượng dương hữu hạn")
        target = min(float(cue["end"]) - start, window)
        automatic_speed = min(MAX_AUTO_SPEED, max(MIN_AUTO_SPEED, generated / target))
        speed = automatic_speed * speech_rate
        end = start + generated / speed
        if end > start + window + .001:
            raise PipelineError(
                "TTS_TIMING_OVERFLOW",
                f"Câu {index + 1} tại {start:.2f}s cần {generated / speed:.2f}s nhưng chỉ có {window:.2f}s. "
                "Hãy chọn giọng khác hoặc tăng nhẹ tốc độ rồi tạo lại preview.",
            )
        timeline.append({"start": start, "end": end, "speed": speed})
    return timeline


def dubbing_windows(cues: list[dict], video_duration: float, gap: float = .04,
                    next_cue_start: float | None = None) -> list[float]:
    if not math.isfinite(video_duration) or video_duration <= 0 or not math.isfinite(gap) or gap < 0:
        raise ValueError("Thời lượng video hoặc khoảng nghỉ không hợp lệ")
    windows = []
    for index, cue in enumerate(cues):
        start, end = float(cue["start"]), float(cue["end"])
        if not all(math.isfinite(value) for value in (start, end)) or start < 0 or end <= start or start >= video_duration:
            raise ValueError("Timestamp lời thoại không hợp lệ")
        boundary = float(cues[index + 1]["start"]) if index + 1 < len(cues) else (
            video_duration if next_cue_start is None else float(next_cue_start))
        if not math.isfinite(boundary) or boundary <= start:
            raise ValueError("Các câu phải được sắp theo thời gian tăng dần")
        # Preserve scene timing; borrow at most half a second of a real pause.
        # Leave a small guard for FFmpeg's sample-level duration rounding.
        available_end = min(video_duration, end + .5, boundary - gap)
        window = available_end - start
        if window <= 0:
            raise PipelineError("TTS_TIMING_OVERFLOW", f"Các câu quá sát nhau tại {start:.2f}s.")
        windows.append(window)
    return windows


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
    all_cues = job["cues"]
    all_windows = dubbing_windows(all_cues, float(job["duration"]))
    cues = [dict(cue) for cue in all_cues if duration_limit is None or float(cue["start"]) < duration_limit]
    windows = all_windows[:len(cues)]
    total = len(cues)
    cache = job.setdefault("tts_cache", {})
    generated_files = []
    generated_durations = []
    for index, cue in enumerate(cues, 1):
        voice = request.voiceMap.get(cue["speaker"]) or request.voiceMap.get("*")
        if not voice:
            raise PipelineError("INVALID_VOICE_MAP", f"Không có giọng được chọn cho {cue['speaker']}.")
        cache_key = str(cue["id"])
        cached = cache.get(cache_key, {})
        raw = directory / f"raw-{int(cue['id']):04d}.wav"
        if (cached.get("version") != TTS_CACHE_VERSION or cached.get("voice") != voice
                or cached.get("text") != cue["text_vi"] or cached.get("window") != windows[index - 1]
                or cached.get("source_duration") != float(cue["end"]) - float(cue["start"]) or not raw.exists()):
            update(job, 62 + index / max(1, total) * 12, f"Đang tạo giọng Việt {index}/{total} ({cue['speaker']})…")
            cue_duration = max(.35, float(cue["end"]) - float(cue["start"]))
            if voice.startswith("clone:"):
                synthesize_verified_clone(
                    cue["text_vi"], voice, raw, voices, cue_duration,
                    timing_window=windows[index - 1],
                )
            else:
                synthesize_cue(cue["text_vi"], voice, raw, voices, target_duration=cue_duration)
            cache[cache_key] = {
                "version": TTS_CACHE_VERSION, "voice": voice,
                "text": cue["text_vi"], "duration": media_duration(raw),
                "window": windows[index - 1], "source_duration": float(cue["end"]) - float(cue["start"]),
            }
        generated_files.append(raw)
        generated_durations.append(float(cache[cache_key]["duration"]))
    output_duration = min(float(job["duration"]), duration_limit) if duration_limit is not None else float(job["duration"])
    # A preview uses the same source boundaries as the full export, even at 30s.
    next_start = float(all_cues[len(cues)]["start"]) if len(cues) < len(all_cues) else None
    timeline = plan_dubbing_timeline(cues, generated_durations, float(job["duration"]),
                                    speech_rate=float(request.speechRate), next_cue_start=next_start)
    rendered_segments = []
    for index, (cue, raw, timing) in enumerate(zip(cues, generated_files, timeline), 1):
        update(job, 74 + index / total * 4, f"Đang căn thời gian giọng Việt {index}/{total}…")
        fitted = directory / f"fit-{int(cue['id']):04d}.wav"
        run([ffmpeg(), "-y", "-i", str(raw), "-af", _atempo(timing["speed"]), "-ar", "24000", "-ac", "1", str(fitted)], "VOICE_FAILED")
        with fitted.open("rb") as fitted_stream:
            segment = AudioSegment.from_file(fitted_stream, format="wav").set_frame_rate(24000).set_channels(1)
        if len(segment) / 1000 > windows[index - 1] + .02:
            raise PipelineError("TTS_TIMING_OVERFLOW", f"Audio câu {index} vượt khoảng thời gian cho phép sau khi căn giọng.")
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


def separate_background(job: dict) -> Path:
    cached = job.get("background")
    if cached and Path(cached).exists():
        return Path(cached)
    output = job["work_dir"] / "separated"
    update(job, 79, "Đang tách nhạc nền và loại bỏ giọng gốc…")
    result = subprocess.run([sys.executable, "-m", "demucs", "--two-stems", "vocals", "-n", "htdemucs", "-o", str(output), str(job["source"])], capture_output=True, text=True)
    music_candidates = list(output.glob("**/no_vocals.wav"))
    music = music_candidates[0] if music_candidates else None
    if result.returncode or music is None:
        fallback = job["work_dir"] / "background.wav"
        duration = max(.1, float(job.get("duration", 1)))
        run([
            ffmpeg(), "-y", "-f", "lavfi", "-i", "anullsrc",
            "-t", f"{duration:.3f}", "-ar", "44100", "-ac", "2", str(fallback),
        ], "AUDIO_SEPARATION_FAILED")
        job["warning"] = "Demucs không tách được nhạc nền; bản xuất dùng nền im lặng để không giữ giọng gốc."
        job["background"] = fallback
        return fallback
    job["background"] = music
    return music


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
            f"w=iw*{subtitle_rect.w:.7f}:h=ih*{subtitle_rect.h:.7f}:color=white@0.16:t=fill[{panel}]"
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
