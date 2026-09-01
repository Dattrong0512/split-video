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

app = FastAPI(title="Douyin Vietnamese Dubbing", version="1.0.0", docs_url=None, redoc_url=None, openapi_url=None)
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
    return {key: value for key, value in job.items() if key not in {"gemini_key", "cookie_text", "download_token", "download_expires", "work_dir", "source", "cues", "result"}}


def fail_job(job_id: str, error: Exception) -> None:
    code = error.code if isinstance(error, PipelineError) else "PROCESSING_FAILED"
    with LOCK:
        JOBS[job_id].update(status="failed", message=str(error), error={"code": code, "message": str(error)})


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
        "ok": True,
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
            "id": job_id, "status": "queued", "progress": 1, "message": "Đang chờ GPU…",
            "canonical_url": request.canonicalUrl, "cookie_text": request.cookieText,
            "gemini_key": request.geminiApiKey, "blur_mode": request.blurMode,
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
    with path.open("wb") as output:
        while chunk := await file.read(1024 * 1024):
            output.write(chunk)
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
    if job["status"] != "analysis_ready":
        raise HTTPException(status_code=409, detail={"code": "JOB_NOT_READY", "message": "Job chưa phân tích xong."})
    job.update(status="queued_render", message="Đang chờ render…", progress=56)
    EXECUTOR.submit(run_render, job_id, request)
    return {"ok": True}


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
    if not job or job.get("status") != "complete" or not Path(job.get("result", "")).exists():
        raise HTTPException(status_code=409, detail={"code": "RESULT_NOT_READY", "message": "Video chưa sẵn sàng."})
    token = secrets.token_urlsafe(24)
    job["download_token"] = token
    job["download_expires"] = time.time() + 600
    video_id = job["canonical_url"].rsplit("/", 1)[-1]
    return {"url": f"{PUBLIC_URL}/api/results/{job_id}?token={token}", "filename": f"douyin_{video_id}_vi_dub.mp4"}


def cleanup_job(job_id: str) -> None:
    job = JOBS.pop(job_id, None)
    if job:
        shutil.rmtree(job["work_dir"], ignore_errors=True)


@app.get("/api/results/{job_id}")
def download_result(job_id: str, token: str, background_tasks: BackgroundTasks):
    job = JOBS.get(job_id)
    if not job or token != job.get("download_token") or time.time() > job.get("download_expires", 0):
        raise HTTPException(status_code=403, detail="Liên kết tải không hợp lệ hoặc đã hết hạn.")
    job["download_token"] = None
    background_tasks.add_task(cleanup_job, job_id)
    video_id = job["canonical_url"].rsplit("/", 1)[-1]
    return FileResponse(job["result"], media_type="video/mp4", filename=f"douyin_{video_id}_vi_dub.mp4", background=background_tasks)
