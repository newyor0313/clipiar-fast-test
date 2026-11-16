"""Clipiar Fast — SnapDeploy TEST app (worker-only, no Firebase).

Purpose: answer ONE question on a non-Google IP (SnapDeploy runs on AWS
Fargate): can yt-dlp download the Rochy live segment, or does YouTube
block it with "Sign in to confirm you're not a bot" like it did on
Google Cloud Run?

This app deliberately has NO Firebase, NO auth, NO RTDB, NO Storage.
That is what makes it deployable on SnapDeploy's 512 MB free container
without a service-account key (the full app.py returns 503 for /cut
without Firebase credentials).

Endpoints:
  GET  /healthz          -> {"ok": true}
  POST /test-cut         {"liveUrl", "startSec", "endSec"} (defaults:
                         Rochy live, 0, 60)
                         -> runs download_segment() then convert_vertical()
                            in a background thread; returns 202 {jobId}
  GET  /test-cut/<id>    -> {status, download_ok, download_bytes,
                            download_error, convert_error, bytes,
                            durationSec, videoUrl}
  GET  /download/<id>    -> the finished MP4 (only when status == "ready")

The download step is reported separately from the convert step, because
on a 512 MB container ffmpeg may run out of memory (OOM) even if the
download worked. A download success + convert OOM still answers the
key question (YouTube did NOT block this IP); a download failure with
the bot-check message answers it the other way.

Run locally:  python3 test_app.py   (PORT env, default 8080)
Deploy on SnapDeploy: push this folder to a GitHub repo, sign up at
snapdeploy.dev/register (email or GitHub, NO card), Deploy -> pick the
repo -> Small (512 MB, free) -> Deploy. URL:
<name>.containers.snapdeploy.app
"""
from __future__ import annotations

import os
import secrets
import shutil
import threading
import time

from flask import Flask, jsonify, request, send_file

from worker import CutError, convert_vertical, download_segment, probe_duration

app = Flask(__name__)

LIVE_URL = "https://www.youtube.com/live/WUoeVvKaugw"
JOB_DIR_ROOT = os.environ.get("JOB_DIR_ROOT", "/tmp/snap-test-jobs")

jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()


def _run(job_id: str, live_url: str, start: int, end: int) -> None:
    job_dir = os.path.join(JOB_DIR_ROOT, job_id)
    os.makedirs(job_dir, exist_ok=True)
    src = os.path.join(job_dir, "src.mp4")
    out = os.path.join(job_dir, "cut.mp4")
    try:
        with jobs_lock:
            jobs[job_id]["status"] = "downloading"
        download_segment(live_url, start, end, src,
                         deadline=time.time() + 12 * 60)
        with jobs_lock:
            jobs[job_id].update(
                status="converting", download_ok=True,
                download_bytes=os.path.getsize(src))
        convert_vertical(src, out, "Clips Dominicano", "br")
        duration = probe_duration(out)
        with jobs_lock:
            jobs[job_id].update(
                status="ready", bytes=os.path.getsize(out),
                durationSec=round(duration, 2), output_path=out)
    except CutError as exc:
        msg = str(exc)
        with jobs_lock:
            # download_segment raises CutError too; if download_bytes was
            # never set, the failure was in the download step.
            if jobs[job_id].get("download_ok") is None:
                jobs[job_id].update(status="error", download_ok=False,
                                    download_error=msg)
            else:
                jobs[job_id].update(status="error", convert_error=msg)
    except Exception as exc:  # noqa: BLE001
        with jobs_lock:
            jobs[job_id].update(status="error", error=str(exc))


@app.route("/healthz", methods=["GET"])
def healthz():
    return jsonify({"ok": True, "service": "clipiar-fast-snapdeploy-test"})


@app.route("/test-cut", methods=["POST"])
def test_cut():
    body = request.get_json(silent=True) or {}
    live_url = str(body.get("liveUrl") or LIVE_URL)
    start = int(body.get("startSec", 0))
    end = int(body.get("endSec", 60))
    job_id = f"test_{secrets.token_hex(6)}"
    with jobs_lock:
        jobs[job_id] = {"jobId": job_id, "status": "queued",
                        "liveUrl": live_url, "startSec": start,
                        "endSec": end, "download_ok": None}
    threading.Thread(target=_run, args=(job_id, live_url, start, end),
                     daemon=True).start()
    return jsonify({"jobId": job_id, "status": "queued"}), 202


@app.route("/test-cut/<job_id>", methods=["GET"])
def get_test(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
    if job is None:
        return jsonify({"error": "not found"}), 404
    return jsonify({k: v for k, v in job.items() if k != "output_path"})


@app.route("/download/<job_id>", methods=["GET"])
def download(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
    if job is None:
        return jsonify({"error": "not found"}), 404
    if job.get("status") != "ready" or not job.get("output_path"):
        return jsonify({"error": "not ready"}), 409
    return send_file(job["output_path"], mimetype="video/mp4",
                     as_attachment=True, download_name=f"{job_id}.mp4")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, threaded=True)
