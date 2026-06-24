import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict

import requests
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse


APP_NAME = "CleanMark AI Online Backend"

RUNPOD_ENDPOINT_ID = os.environ.get("RUNPOD_ENDPOINT_ID", "i54121inahiu6n").strip()
RUNPOD_API_KEY = os.environ.get("RUNPOD_API_KEY", "").strip()

PUBLIC_BASE_URL = os.environ.get(
    "PUBLIC_BASE_URL",
    "https://cleanmarkai-backend.onrender.com"
).strip().rstrip("/")

MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "150"))

BASE_DIR = Path(__file__).parent
UPLOADS_DIR = BASE_DIR / "uploads"
OUTPUTS_DIR = BASE_DIR / "outputs"
JOBS_FILE = BASE_DIR / "jobs.json"

UPLOADS_DIR.mkdir(exist_ok=True)
OUTPUTS_DIR.mkdir(exist_ok=True)

app = FastAPI(title=APP_NAME)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def runpod_headers() -> Dict[str, str]:
    if not RUNPOD_API_KEY:
        raise HTTPException(
            status_code=500,
            detail="RUNPOD_API_KEY is missing. Add it in Render Environment Variables."
        )

    return {
        "Authorization": f"Bearer {RUNPOD_API_KEY}",
        "Content-Type": "application/json"
    }


def runpod_run_url() -> str:
    return f"https://api.runpod.ai/v2/{RUNPOD_ENDPOINT_ID}/run"


def runpod_status_url(runpod_job_id: str) -> str:
    return f"https://api.runpod.ai/v2/{RUNPOD_ENDPOINT_ID}/status/{runpod_job_id}"


def load_jobs() -> Dict[str, Any]:
    if not JOBS_FILE.exists():
        return {}

    try:
        return json.loads(JOBS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_jobs(jobs: Dict[str, Any]) -> None:
    JOBS_FILE.write_text(
        json.dumps(jobs, indent=2),
        encoding="utf-8"
    )


def safe_file_name(name: str) -> str:
    cleaned = name.replace(" ", "_")
    cleaned = "".join(
        c for c in cleaned
        if c.isalnum() or c in ["_", "-", "."]
    )
    return cleaned or "video.mp4"


@app.get("/")
def home():
    return {
        "app": APP_NAME,
        "status": "running",
        "runpod_endpoint_id": RUNPOD_ENDPOINT_ID,
        "public_base_url": PUBLIC_BASE_URL,
        "endpoints": {
            "submit_video": "/submit-video",
            "input_video": "/input-video/{local_job_id}",
            "upload_result": "/upload-result/{local_job_id}",
            "job_status": "/job-status/{local_job_id}",
            "download": "/download/{local_job_id}"
        }
    }


@app.post("/submit-video")
async def submit_video(
    source_name: str = Form(...),
    video_index: int = Form(1),
    file: UploadFile = File(...)
):
    local_job_id = str(uuid.uuid4())

    original_name = safe_file_name(file.filename or "video.mp4")
    input_path = UPLOADS_DIR / f"{local_job_id}_{original_name}"
    output_zip_path = OUTPUTS_DIR / f"{local_job_id}_result.zip"

    video_bytes = await file.read()
    size_mb = len(video_bytes) / (1024 * 1024)

    if size_mb <= 0:
        raise HTTPException(
            status_code=400,
            detail="Uploaded video is empty."
        )

    if size_mb > MAX_UPLOAD_MB:
        raise HTTPException(
            status_code=400,
            detail=f"Video is too large. Max is {MAX_UPLOAD_MB} MB."
        )

    input_path.write_bytes(video_bytes)

    video_url = f"{PUBLIC_BASE_URL}/input-video/{local_job_id}"
    result_upload_url = f"{PUBLIC_BASE_URL}/upload-result/{local_job_id}"

    payload = {
        "input": {
            "source_name": source_name,
            "video_index": video_index,
            "video_url": video_url,
            "result_upload_url": result_upload_url,
            "return_base64_zip": False
        }
    }

    response = requests.post(
        runpod_run_url(),
        headers=runpod_headers(),
        json=payload,
        timeout=120
    )

    if response.status_code >= 400:
        raise HTTPException(
            status_code=500,
            detail=f"RunPod submit failed: {response.text}"
        )

    data = response.json()
    runpod_job_id = data.get("id")

    if not runpod_job_id:
        raise HTTPException(
            status_code=500,
            detail=f"RunPod did not return job id: {data}"
        )

    jobs = load_jobs()

    jobs[local_job_id] = {
        "local_job_id": local_job_id,
        "runpod_job_id": runpod_job_id,
        "source_name": source_name,
        "video_index": video_index,
        "status": "SUBMITTED",
        "created_at": time.time(),
        "input_path": str(input_path),
        "output_zip": str(output_zip_path),
        "error": ""
    }

    save_jobs(jobs)

    return {
        "success": True,
        "message": "Video submitted to AI.",
        "local_job_id": local_job_id,
        "runpod_job_id": runpod_job_id,
        "status_url": f"/job-status/{local_job_id}",
        "download_url": f"/download/{local_job_id}"
    }


@app.get("/input-video/{local_job_id}")
def input_video(local_job_id: str):
    jobs = load_jobs()
    job = jobs.get(local_job_id)

    if not job:
        raise HTTPException(
            status_code=404,
            detail="Job not found."
        )

    input_path = Path(job["input_path"])

    if not input_path.exists():
        raise HTTPException(
            status_code=404,
            detail="Input video not found."
        )

    return FileResponse(
        path=str(input_path),
        media_type="video/mp4",
        filename=input_path.name
    )


@app.put("/upload-result/{local_job_id}")
async def upload_result(local_job_id: str, request: Request):
    jobs = load_jobs()
    job = jobs.get(local_job_id)

    if not job:
        raise HTTPException(
            status_code=404,
            detail="Job not found."
        )

    output_zip_path = Path(job["output_zip"])
    body = await request.body()

    if not body:
        raise HTTPException(
            status_code=400,
            detail="Uploaded result is empty."
        )

    output_zip_path.write_bytes(body)

    job["result_uploaded"] = True
    job["result_uploaded_at"] = time.time()
    jobs[local_job_id] = job
    save_jobs(jobs)

    return {
        "success": True,
        "message": "Result uploaded.",
        "local_job_id": local_job_id,
        "bytes": output_zip_path.stat().st_size
    }


@app.get("/job-status/{local_job_id}")
def job_status(local_job_id: str):
    jobs = load_jobs()
    job = jobs.get(local_job_id)

    if not job:
        raise HTTPException(
            status_code=404,
            detail="Job not found."
        )

    output_zip_path = Path(job["output_zip"])

    if job.get("status") == "COMPLETED" and output_zip_path.exists():
        return {
            "success": True,
            "status": "COMPLETED",
            "local_job_id": local_job_id,
            "download_url": f"/download/{local_job_id}",
            "zip_size_mb": round(output_zip_path.stat().st_size / (1024 * 1024), 2)
        }

    response = requests.get(
        runpod_status_url(job["runpod_job_id"]),
        headers=runpod_headers(),
        timeout=60
    )

    if response.status_code >= 400:
        raise HTTPException(
            status_code=500,
            detail=f"RunPod status check failed: {response.text}"
        )

    data = response.json()
    runpod_status = data.get("status", "")

    if runpod_status == "COMPLETED":
        output = data.get("output")

        if not isinstance(output, dict):
            job["status"] = "FAILED"
            job["error"] = f"RunPod output invalid: {data}"
            jobs[local_job_id] = job
            save_jobs(jobs)

            return {
                "success": False,
                "status": "FAILED",
                "error": job["error"]
            }

        if not output.get("success"):
            job["status"] = "FAILED"
            job["error"] = str(output)
            jobs[local_job_id] = job
            save_jobs(jobs)

            return {
                "success": False,
                "status": "FAILED",
                "error": job["error"]
            }

        if not output_zip_path.exists():
            job["status"] = "FAILED"
            job["error"] = "RunPod completed but result ZIP was not uploaded to backend."
            jobs[local_job_id] = job
            save_jobs(jobs)

            return {
                "success": False,
                "status": "FAILED",
                "error": job["error"]
            }

        job["status"] = "COMPLETED"
        job["completed_at"] = time.time()
        job["clean_video_file"] = output.get("clean_video_file", "")
        job["image_files"] = output.get("image_files", [])
        job["runpod_seconds"] = output.get("seconds")
        jobs[local_job_id] = job
        save_jobs(jobs)

        try:
            input_path = Path(job["input_path"])
            if input_path.exists():
                input_path.unlink()
        except Exception:
            pass

        return {
            "success": True,
            "status": "COMPLETED",
            "local_job_id": local_job_id,
            "clean_video_file": job["clean_video_file"],
            "image_files": job["image_files"],
            "download_url": f"/download/{local_job_id}",
            "zip_size_mb": round(output_zip_path.stat().st_size / (1024 * 1024), 2),
            "runpod_seconds": job["runpod_seconds"]
        }

    if runpod_status in ["FAILED", "CANCELLED", "TIMED_OUT"]:
        job["status"] = "FAILED"
        job["error"] = str(data)
        jobs[local_job_id] = job
        save_jobs(jobs)

        return {
            "success": False,
            "status": "FAILED",
            "error": job["error"]
        }

    job["status"] = runpod_status or "RUNNING"
    jobs[local_job_id] = job
    save_jobs(jobs)

    return {
        "success": True,
        "status": job["status"],
        "local_job_id": local_job_id,
        "message": "Still processing."
    }


@app.get("/download/{local_job_id}")
def download_result(local_job_id: str):
    jobs = load_jobs()
    job = jobs.get(local_job_id)

    if not job:
        raise HTTPException(
            status_code=404,
            detail="Job not found."
        )

    output_zip_path = Path(job["output_zip"])

    if not output_zip_path.exists():
        raise HTTPException(
            status_code=404,
            detail="Result ZIP not ready yet."
        )

    return FileResponse(
        path=str(output_zip_path),
        media_type="application/zip",
        filename=f"cleanmark_result_{local_job_id}.zip"
    )
