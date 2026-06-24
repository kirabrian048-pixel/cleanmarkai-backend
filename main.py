import base64
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict

import requests
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse


APP_NAME = "CleanMark AI Online Backend"

RUNPOD_ENDPOINT_ID = os.environ.get("RUNPOD_ENDPOINT_ID", "i54121inahiu6n").strip()
RUNPOD_API_KEY = os.environ.get("RUNPOD_API_KEY", "").strip()

MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "80"))
RUNPOD_MAX_INLINE_MB = int(os.environ.get("RUNPOD_MAX_INLINE_MB", "80"))

BASE_DIR = Path(__file__).parent
OUTPUTS_DIR = BASE_DIR / "outputs"
JOBS_FILE = BASE_DIR / "jobs.json"

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
            detail="RUNPOD_API_KEY is missing. Add it as an environment variable."
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


def encode_bytes_to_base64(data: bytes) -> str:
    return base64.b64encode(data).decode("utf-8")


def decode_zip_base64(zip_base64: str, output_path: Path) -> None:
    output_path.write_bytes(base64.b64decode(zip_base64))


@app.get("/")
def home():
    return {
        "app": APP_NAME,
        "status": "running",
        "runpod_endpoint_id": RUNPOD_ENDPOINT_ID,
        "endpoints": {
            "submit_video": "/submit-video",
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
            detail=f"Video is too large. Max is {MAX_UPLOAD_MB} MB for this version."
        )

    local_job_id = str(uuid.uuid4())

    payload = {
        "input": {
            "source_name": source_name,
            "video_index": video_index,
            "video_base64": encode_bytes_to_base64(video_bytes),
            "return_base64_zip": True,
            "max_inline_mb": RUNPOD_MAX_INLINE_MB
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
        "output_zip": str(OUTPUTS_DIR / f"{local_job_id}_result.zip"),
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

    runpod_job_id = job["runpod_job_id"]

    response = requests.get(
        runpod_status_url(runpod_job_id),
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

        zip_base64 = output.get("zip_base64")

        if not zip_base64:
            job["status"] = "FAILED"
            job["error"] = f"RunPod did not return zip_base64: {output}"
            jobs[local_job_id] = job
            save_jobs(jobs)

            return {
                "success": False,
                "status": "FAILED",
                "error": job["error"]
            }

        decode_zip_base64(zip_base64, output_zip_path)

        job["status"] = "COMPLETED"
        job["completed_at"] = time.time()
        job["clean_video_file"] = output.get("clean_video_file", "")
        job["image_files"] = output.get("image_files", [])
        job["runpod_seconds"] = output.get("seconds")
        jobs[local_job_id] = job
        save_jobs(jobs)

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
