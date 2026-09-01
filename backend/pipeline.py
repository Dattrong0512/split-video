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
from pathlib import Path
from typing import Any


class PipelineError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


_whisper_model = None
_omnivoice_model = None


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
    segments, _ = _whisper().transcribe(str(audio), vad_filter=True, beam_size=5, condition_on_previous_text=True)
    cues = [{"id": index, "start": float(segment.start), "end": max(float(segment.end), float(segment.start) + .12), "original": segment.text.strip()}
            for index, segment in enumerate(segments) if segment.text and segment.text.strip()]
    if not cues:
        raise PipelineError("NO_SPEECH", "Không nhận diện được lời thoại trong video.")
    return cues


def gemini_translate(cues: list[dict], audio: Path, api_key: str) -> list[dict]:
    client = None
    uploaded = None
    try:
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=api_key)
        uploaded = client.files.upload(file=str(audio))
        prompt = (
            "Nghe audio và đối chiếu transcript. Sửa lỗi nhận dạng, dịch từng câu sang tiếng Việt tự nhiên, "
            "gán người nói nhất quán S1, S2... và gender male/female/unknown. Không đổi, thiếu, thêm, gộp hoặc tách id. "
            "Chỉ trả JSON array với các field id, text_vi, speaker, gender. Transcript: " +
            json.dumps([{"id": cue["id"], "text": cue["original"]} for cue in cues], ensure_ascii=False)
        )
        response = client.models.generate_content(
            model=os.environ.get("GEMINI_MODEL", "gemini-3.7-flash"),
            contents=[prompt, uploaded],
            config=types.GenerateContentConfig(response_mime_type="application/json"),
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
        by_id = {int(row["id"]): row for row in rows}
        if set(by_id) != set(range(len(cues))): raise ValueError("Gemini đã thay đổi danh sách id")
        for cue in cues:
            row = by_id[cue["id"]]
            cue["text_vi"] = str(row["text_vi"]).strip()
            cue["speaker"] = re.sub(r"[^A-Za-z0-9_-]", "", str(row.get("speaker", "S1")))[:20] or "S1"
            gender = str(row.get("gender", "unknown")).lower()
            cue["gender"] = gender if gender in {"male", "female", "unknown"} else "unknown"
            if not cue["text_vi"]: raise ValueError("Bản dịch trống")
        return cues
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
            count = target["count"]
            for key in ("x", "y", "w", "h"): target["rect"][key] = (target["rect"][key] * count + rect[key]) / (count + 1)
            target["count"] += 1; target["frames"].add(frame_index)
    output = []
    for cluster in clusters:
        rect = cluster["rect"]
        persistent = len(cluster["frames"]) >= max(2, math.ceil(sample_count * .18))
        subtitle_band = rect["y"] > .52 and len(cluster["frames"]) >= 2
        if not (persistent or subtitle_band): continue
        padding_x, padding_y = .012, .01
        x, y = max(0, rect["x"] - padding_x), max(0, rect["y"] - padding_y)
        output.append({"x": x, "y": y, "w": min(1 - x, rect["w"] + padding_x * 2), "h": min(1 - y, rect["h"] + padding_y * 2)})
    return output[:8]


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
    try:
        import cv2
        from paddleocr import PaddleOCR
    except Exception:
        return []
    try:
        ocr = PaddleOCR(
            lang="ch", ocr_version="PP-OCRv5",
            text_detection_model_name="PP-OCRv5_mobile_det",
            text_recognition_model_name="PP-OCRv5_mobile_rec",
            use_doc_orientation_classify=False, use_doc_unwarping=False,
            use_textline_orientation=False, device="cpu",
        )
    except Exception:
        ocr = None
    capture = cv2.VideoCapture(str(source)); sample_count = 16; detections = []
    try:
        for index in range(sample_count):
            capture.set(cv2.CAP_PROP_POS_MSEC, (duration * (index + .5) / sample_count) * 1000)
            ok, frame = capture.read()
            if not ok: continue
            height, width = frame.shape[:2]
            boxes = []
            if ocr is not None:
                try:
                    boxes = _v3_ocr_boxes(ocr.predict(frame), width, height)
                except Exception:
                    try: boxes = _old_ocr_boxes(ocr.ocr(frame), width, height)
                    except Exception: boxes = []
            hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
            mask = cv2.inRange(hsv, (85, 55, 45), (140, 255, 255))
            contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            for x, y, w, h in (cv2.boundingRect(contour) for contour in contours):
                if w * h > width * height * .00012 and w > 8 and h > 5:
                    boxes.append({"x": x/width, "y": y/height, "w": w/width, "h": h/height})
            detections.extend((index, rect) for rect in boxes if rect["w"] > .015 and rect["h"] > .008)
    finally: capture.release()
    return cluster_rectangles(detections, sample_count)


def analyze_job(job: dict, _voices: dict) -> None:
    update(job, 4, "Đang tải video Douyin…", status="downloading")
    source = download_douyin(job); job["source"] = source
    update(job, 16, "Đang tách audio và ảnh xem trước…", status="analyzing")
    duration = media_duration(source); audio, preview = extract_assets(source, job["work_dir"], duration)
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
    job["cues"] = cues; job["duration"] = duration
    analysis = {"previewDataUrl": preview, "blurRegions": regions, "subtitleRect": {"x": .08, "y": .75, "w": .84, "h": .17}, "speakers": speakers, "cueCount": len(cues)}
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
    final = AudioSegment.silent(duration=int(job["duration"] * 1000) + 500, frame_rate=24000).set_channels(1)
    total = len(job["cues"])
    for index, cue in enumerate(job["cues"], 1):
        update(job, 62 + index / total * 16, f"Đang tạo giọng Việt {index}/{total} ({cue['speaker']})…")
        voice = request.voiceMap.get(cue["speaker"]) or request.voiceMap.get("*") or "edge:vi-VN-HoaiMyNeural"
        raw, fitted = directory / f"{index:04d}.wav", directory / f"{index:04d}-fit.wav"
        synthesize_cue(cue["text_vi"], voice, raw, voices)
        generated = media_duration(raw); target = max(.25, cue["end"] - cue["start"])
        run([ffmpeg(), "-y", "-i", str(raw), "-af", _atempo(generated / target), "-ar", "24000", "-ac", "1", str(fitted)], "VOICE_FAILED")
        final = final.overlay(AudioSegment.from_file(fitted), position=max(0, int(cue["start"] * 1000)))
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


def write_ass(path: Path, cues: list[dict], rect) -> None:
    size = max(30, min(72, int(rect.h * 260))); x = int((rect.x + rect.w/2) * 1920); y = int((rect.y + rect.h/2) * 1080)
    header = "[Script Info]\nScriptType: v4.00+\nPlayResX: 1920\nPlayResY: 1080\nScaledBorderAndShadow: yes\n\n[V4+ Styles]\nFormat: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding\n"
    header += f"Style: Vietnamese,DejaVu Sans,{size},&H00FFFFFF,&H000000FF,&H00101010,&H90000000,-1,0,0,0,100,100,0,0,3,2,1,2,30,30,30,1\n\n[Events]\nFormat: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n"
    events = []
    for cue in cues:
        text = cue["text_vi"].replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}").replace("\n", "\\N")
        events.append(f"Dialogue: 0,{ass_time(cue['start'])},{ass_time(cue['end'])},Vietnamese,,0,0,0,,{{\\an5\\pos({x},{y})}}{text}")
    path.write_text(header + "\n".join(events) + "\n", encoding="utf-8-sig")


def video_filter(regions: list, ass_path: Path) -> str:
    parts = []; current = "0:v"
    for index, rect in enumerate(regions):
        base, crop, blur, out = f"base{index}", f"crop{index}", f"blur{index}", f"v{index}"
        parts.append(f"[{current}]split=2[{base}][{crop}]")
        parts.append(f"[{crop}]crop=iw*{rect.w:.7f}:ih*{rect.h:.7f}:iw*{rect.x:.7f}:ih*{rect.y:.7f},gblur=sigma=14[{blur}]")
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
    ass = job["work_dir"] / "subtitles.ass"; write_ass(ass, job["cues"], request.subtitleRect)
    result = job["work_dir"] / "result.mp4"
    filters = video_filter(request.blurRegions, ass) + ";[1:a]volume=0.92[bg];[2:a]volume=1.15[dub];[bg][dub]amix=inputs=2:duration=longest:normalize=0,alimiter=limit=.95[aout]"
    run([ffmpeg(), "-y", "-i", str(job["source"]), "-i", str(background), "-i", str(dubbing), "-filter_complex", filters,
         "-map", "[vout]", "-map", "[aout]", "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-shortest", str(result)], "RENDER_FAILED")
    job["result"] = result
    update(job, 100, "Hoàn tất. Đang chuẩn bị tải xuống…", status="complete")
