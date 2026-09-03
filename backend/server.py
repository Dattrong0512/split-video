from __future__ import annotations

import os
import re
import secrets
import shutil
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from .models import AnalyzeRequest, RenderRequest
from .pipeline import PipelineError, analyze_job, probe_reference, render_job


SESSION_TOKEN = os.environ.get("DUBBING_SESSION_TOKEN") or secrets.token_urlsafe(32)
WORK_ROOT = Path(os.environ.get("DUBBING_WORK_ROOT", tempfile.mkdtemp(prefix="douyin-dubbing-")))
PUBLIC_URL = os.environ.get("DUBBING_PUBLIC_URL", "").rstrip("/")
EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dubbing-job")
JOBS: dict[str, dict] = {}
VOICES: dict[str, dict] = {}
LOCK = threading.RLock()

app = FastAPI(title="Douyin Vietnamese Dubbing", version="1.5.0", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^chrome-extension://[a-p]{32}$",
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)


def authorize(authorization: str | None = Header(default=None)) -> None:
    if authorization != f"Bearer {SESSION_TOKEN}":
        raise HTTPException(status_code=401, detail={"code": "UNAUTHORIZED", "message": "Token phiên không hợp lệ."})


def public_job(job: dict) -> dict:
    private = {
        "gemini_key", "cookie_text", "download_token", "download_expires",
        "preview_token", "preview_expires", "review_token", "review_expires",
        "work_dir", "source", "browser_preview", "cues", "result", "review_result", "tts_cache", "background",
    }
    return {key: value for key, value in job.items() if key not in private}


def fail_job(job_id: str, error: Exception) -> None:
    code = error.code if isinstance(error, PipelineError) else "PROCESSING_FAILED"
    with LOCK:
        job = JOBS[job_id]
        job.pop("gemini_key", None)
        job.pop("cookie_text", None)
        job.update(status="failed", message=str(error), error={"code": code, "message": str(error)})
    shutil.rmtree(job["work_dir"], ignore_errors=True)


def run_analysis(job_id: str) -> None:
    try:
        analyze_job(JOBS[job_id], VOICES)
    except Exception as error:
        fail_job(job_id, error)


def run_render(job_id: str, request: RenderRequest) -> None:
    try:
        render_job(JOBS[job_id], request, VOICES)
    except Exception as error:
        fail_job(job_id, error)


@app.get("/api/health", dependencies=[Depends(authorize)])
def health() -> dict:
    return {
        "ok": True, "apiVersion": "1.5.0",
        "immutableReviews": True,
        "voices": [
            {"id": "edge:vi-VN-HoaiMyNeural", "name": "Hoài My · Nữ"},
            {"id": "edge:vi-VN-NamMinhNeural", "name": "Nam Minh · Nam"},
        ],
    }


@app.post("/api/jobs/analyze", dependencies=[Depends(authorize)])
def create_analysis(request: AnalyzeRequest) -> dict:
    if not re.fullmatch(r"https://www\.douyin\.com/video/\d{15,25}", request.canonicalUrl):
        raise HTTPException(status_code=400, detail={"code": "INVALID_URL", "message": "URL Douyin không hợp lệ."})
    job_id = uuid.uuid4().hex
    work_dir = WORK_ROOT / job_id
    work_dir.mkdir(parents=True)
    with LOCK:
        JOBS[job_id] = {
            "id": job_id, "status": "queued", "progress": 1, "message": "Đang bắt đầu phân tích trên Colab…",
            "canonical_url": request.canonicalUrl, "cookie_text": request.cookieText,
            "gemini_key": request.geminiApiKey, "blur_mode": request.blurMode,
            "voice_count": request.voiceCount,
            "work_dir": work_dir, "cancelled": False,
        }
    EXECUTOR.submit(run_analysis, job_id)
    return {"jobId": job_id}


@app.get("/api/jobs/{job_id}", dependencies=[Depends(authorize)])
def get_job(job_id: str) -> dict:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail={"code": "JOB_NOT_FOUND", "message": "Không tìm thấy job."})
    return public_job(job)


@app.post("/api/voices", dependencies=[Depends(authorize)])
async def create_voice(name: str = Form(...), file: UploadFile = File(...)) -> dict:
    suffix = Path(file.filename or ".wav").suffix.lower()
    if suffix not in {".wav", ".mp3", ".m4a", ".mp4", ".ogg", ".webm"}:
        raise HTTPException(status_code=400, detail={"code": "INVALID_VOICE", "message": "Định dạng clip giọng không hỗ trợ."})
    voice_id = uuid.uuid4().hex
    path = WORK_ROOT / f"voice-{voice_id}{suffix}"
    size = 0
    try:
        with path.open("wb") as output:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > 25 * 1024 * 1024:
                    raise HTTPException(status_code=413, detail={"code": "VOICE_TOO_LARGE", "message": "Clip giọng vượt quá 25 MB."})
                output.write(chunk)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    try:
        transcript, duration = probe_reference(path)
    except Exception as error:
        path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail={"code": "INVALID_VOICE", "message": str(error)}) from error
    VOICES[voice_id] = {"id": voice_id, "name": name[:40], "path": path, "transcript": transcript, "duration": duration}
    return {"voiceId": voice_id, "duration": duration}


@app.post("/api/jobs/{job_id}/render", dependencies=[Depends(authorize)])
def start_render(job_id: str, request: RenderRequest) -> dict:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail={"code": "JOB_NOT_FOUND", "message": "Không tìm thấy job."})
    if job["status"] not in {"analysis_ready", "preview_ready"}:
        raise HTTPException(status_code=409, detail={"code": "JOB_NOT_READY", "message": "Job chưa phân tích xong."})
    if request.previewOnly:
        job.update(status="queued_preview", message="Đang chuẩn bị bản xem trước 30 giây…", progress=56)
    else:
        job.update(status="queued_render", message="Đang chuẩn bị render toàn bộ…", progress=56)
    EXECUTOR.submit(run_render, job_id, request)
    return {"ok": True}


@app.post("/api/jobs/{job_id}/preview-token", dependencies=[Depends(authorize)])
def create_preview_token(job_id: str) -> dict:
    job = JOBS.get(job_id)
    preview_value = job.get("browser_preview") if job else None
    preview = Path(preview_value) if preview_value else None
    if not job or job.get("status") not in {"analysis_ready", "queued_render", "rendering", "complete"} or not preview or not preview.exists():
        raise HTTPException(status_code=409, detail={"code": "PREVIEW_NOT_READY", "message": "Video xem trước chưa sẵn sàng."})
    token = secrets.token_urlsafe(24)
    job["preview_token"] = token
    job["preview_expires"] = time.time() + 7200
    return {"url": f"{PUBLIC_URL}/api/previews/{job_id}?token={token}"}


@app.get("/api/previews/{job_id}")
def preview_video(job_id: str, token: str):
    job = JOBS.get(job_id)
    if not job or token != job.get("preview_token") or time.time() > job.get("preview_expires", 0):
        raise HTTPException(status_code=403, detail="Liên kết xem trước không hợp lệ hoặc đã hết hạn.")
    return FileResponse(
        job["browser_preview"], media_type="video/mp4", filename="browser-preview.mp4",
        content_disposition_type="inline", headers={"Cache-Control": "private, max-age=3600"},
    )


@app.post("/api/jobs/{job_id}/review-token", dependencies=[Depends(authorize)])
def create_review_token(job_id: str) -> dict:
    job = JOBS.get(job_id)
    result_value = job.get("review_result") if job else None
    result = Path(result_value) if result_value else None
    if not job or job.get("status") != "preview_ready" or not result or not result.exists():
        raise HTTPException(status_code=409, detail={"code": "REVIEW_NOT_READY", "message": "Bản xem trước chưa sẵn sàng."})
    token = secrets.token_urlsafe(24)
    job["review_token"] = token
    job["review_expires"] = time.time() + 7200
    return {"url": f"{PUBLIC_URL}/api/reviews/{job_id}?token={token}", "seconds": min(30, float(job["duration"])),
            "speechRate": job.get("review_rate", 1.0)}


@app.get("/api/reviews/{job_id}")
def review_video(job_id: str, token: str):
    job = JOBS.get(job_id)
    if not job or token != job.get("review_token") or time.time() > job.get("review_expires", 0):
        raise HTTPException(status_code=403, detail="Liên kết bản xem trước không hợp lệ hoặc đã hết hạn.")
    return FileResponse(
        job["review_result"], media_type="video/mp4", filename="review-30s.mp4",
        content_disposition_type="inline", headers={"Cache-Control": "private, max-age=3600"},
    )


@app.post("/api/jobs/{job_id}/cancel", dependencies=[Depends(authorize)])
def cancel_job(job_id: str) -> dict:
    job = JOBS.get(job_id)
    if job:
        job["cancelled"] = True
        job.update(status="cancelled", message="Đã hủy.")
    return {"ok": True}


@app.post("/api/jobs/{job_id}/download-token", dependencies=[Depends(authorize)])
def create_download_token(job_id: str) -> dict:
    job = JOBS.get(job_id)
    result_value = job.get("result") if job else None
    result = Path(result_value) if result_value else None
    if not job or job.get("status") != "complete" or not result or not result.exists():
        raise HTTPException(status_code=409, detail={"code": "RESULT_NOT_READY", "message": "Video chưa sẵn sàng."})
    token = secrets.token_urlsafe(24)
    job["download_token"] = token
    job["download_expires"] = time.time() + 600
    video_id = job["canonical_url"].rsplit("/", 1)[-1]
    return {
        "url": f"{PUBLIC_URL}/api/results/{job_id}?token={token}",
        "filename": f"douyin_{video_id}_vi_dub.mp4", "size": result.stat().st_size,
    }


def cleanup_job(job_id: str) -> None:
    job = JOBS.pop(job_id, None)
    if job:
        shutil.rmtree(job["work_dir"], ignore_errors=True)


def schedule_cleanup_job(job_id: str) -> None:
    timer = threading.Timer(300, cleanup_job, args=(job_id,))
    timer.daemon = True
    timer.start()


@app.get("/api/results/{job_id}")
def download_result(job_id: str, token: str, background_tasks: BackgroundTasks):
    job = JOBS.get(job_id)
    if not job or token != job.get("download_token") or time.time() > job.get("download_expires", 0):
        raise HTTPException(status_code=403, detail="Liên kết tải không hợp lệ hoặc đã hết hạn.")
    background_tasks.add_task(schedule_cleanup_job, job_id)
    video_id = job["canonical_url"].rsplit("/", 1)[-1]
    return FileResponse(job["result"], media_type="video/mp4", filename=f"douyin_{video_id}_vi_dub.mp4", background=background_tasks)
