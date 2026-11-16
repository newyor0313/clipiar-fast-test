"""Clipiar Fast cutter worker — pure yt-dlp + ffmpeg pipeline.

No Firebase imports here on purpose: worker.py is runnable standalone
for local testing:
    python3 worker.py "https://www.youtube.com/live/WUoeVvKaugw" 0 60 out.mp4

Pipeline (ported verbatim from the proven Muse cutter / manual proofs in
~/workspace/clipiar-test/, e.g. rochy-clip-vertical.mp4 — 60.0 s, 720x1280):
  1. yt-dlp -f 134+140 --download-sections "*START-END"
     --force-keyframes-at-cuts, up to 3 attempts, 5 s wait after a
     YouTube 403, 180 s per-attempt timeout, abort if 0 bytes written
     for 60 s (the freeze that stranded cuts in the Muse app).
  2. ffmpeg: blurred-background vertical 720x1280, source centred,
     "Clips Dominicano" watermark bottom-right (or chosen corner),
     H.264 CRF 20, AAC 128k, +faststart, 30 fps.
  3. Caller (app.py) uploads the MP4 to Cloud Storage and marks the
     RTDB job record ready; worker only returns the local file.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

MAX_JOB_SECONDS = 12 * 60          # 12-min whole-job cap (plan §2)
DOWNLOAD_ATTEMPT_TIMEOUT = 180     # per-attempt timeout, seconds
DOWNLOAD_STALL_SECONDS = 60        # 0 bytes written for this long -> kill
DOWNLOAD_RETRIES = 3
RETRY_WAIT_SECONDS = 5
CONVERT_TIMEOUT = 300

WATERMARK_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
# Fallback font locations (macOS / other Linux) — first existing wins.
FONT_CANDIDATES = [
    WATERMARK_FONT,
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
]

# x:y expressions for drawtext, by corner code (plan §2: br|bl|tr|tl).
WATERMARK_POS = {
    "br": ("w-tw-24", "h-th-24"),
    "bl": ("24", "h-th-24"),
    "tr": ("w-tw-24", "24"),
    "tl": ("24", "24"),
}


class CutError(Exception):
    """A cut failure with the real underlying error attached (never generic)."""


def fmt_time(seconds: int) -> str:
    """0 -> '0:00:00', 60 -> '0:01:00', 3661 -> '1:01:01' (yt-dlp sections)."""
    seconds = int(seconds)
    return f"{seconds // 3600}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"


def _font_path() -> str:
    for p in FONT_CANDIDATES:
        if os.path.exists(p):
            return p
    return WATERMARK_FONT  # let ffmpeg report the real missing-font error


def _escape_drawtext(text: str) -> str:
    # drawtext text=... needs : ' \ escaped; keep it simple and safe.
    return text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


@dataclass
class CutResult:
    output_path: str
    bytes: int
    duration_sec: float


def download_segment(live_url: str, start_sec: int, end_sec: int,
                     dest_path: str, heartbeat=None,
                     deadline: float | None = None) -> None:
    """Download only [start_sec, end_sec) of a YouTube live/video.

    Retries up to 3x on a YouTube 403 (fresh URL resolution each attempt).
    Any other yt-dlp failure aborts immediately with yt-dlp's real stderr.
    """
    section = f"*{fmt_time(start_sec)}-{fmt_time(end_sec)}"
    last_err = ""
    for attempt in range(1, DOWNLOAD_RETRIES + 1):
        if deadline and time.time() > deadline:
            raise CutError("Tiempo agotado (12 min) durante la descarga")
        if heartbeat:
            heartbeat("downloading",
                      f"Descargando solo este tramo… (intento {attempt}/{DOWNLOAD_RETRIES})",
                      progress=10 + attempt * 5)
        # Fresh download each attempt; remove a partial file first.
        if os.path.exists(dest_path):
            os.remove(dest_path)
        # yt-dlp binary if installed on PATH (Docker image), else
        # `python -m yt_dlp` (this VM / local installs without the binary).
        yt_dlp = shutil.which("yt-dlp")
        ytdlp_cmd = ["yt-dlp"] if yt_dlp else [sys.executable, "-m", "yt_dlp"]
        cmd = ytdlp_cmd + [
            "-f", "134+140",
            "--download-sections", section,
            "--force-keyframes-at-cuts",
            "--no-playlist",
            # PO token / JS challenge solver: YouTube's "Sign in to confirm
            # you're not a bot" check on Cloud Run IPs needs the EJS
            # challenge solver (downloaded from GitHub) plus the Node.js
            # runtime installed in the image.
            "--remote-components", "ejs:github",
            "--js-runtimes", "node",
            "-o", dest_path,
            live_url,
        ]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
        # Stall watchdog: kill if the output file grows by 0 bytes for 60 s.
        stall = {"size": 0, "since": time.time(), "killed": False}

        def _watch():
            while proc.poll() is None:
                try:
                    size = os.path.getsize(dest_path) if os.path.exists(dest_path) else 0
                except OSError:
                    size = 0
                if size > stall["size"]:
                    stall["size"], stall["since"] = size, time.time()
                elif time.time() - stall["since"] > DOWNLOAD_STALL_SECONDS:
                    stall["killed"] = True
                    proc.kill()
                    return
                time.sleep(5)

        watcher = threading.Thread(target=_watch, daemon=True)
        watcher.start()
        try:
            out, _ = proc.communicate(timeout=DOWNLOAD_ATTEMPT_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
            last_err = "yt-dlp tardó más de 180 s y se canceló"
            if attempt < DOWNLOAD_RETRIES:
                time.sleep(RETRY_WAIT_SECONDS)
                continue
            raise CutError(last_err)
        if stall["killed"]:
            last_err = "La descarga se quedó sin avanzar 60 s y se canceló"
            if attempt < DOWNLOAD_RETRIES:
                time.sleep(RETRY_WAIT_SECONDS)
                continue
            raise CutError(last_err)
        if proc.returncode == 0 and os.path.exists(dest_path) \
                and os.path.getsize(dest_path) > 0:
            return
        last_err = (out or "").strip()[-1500:]
        is_403 = "403" in last_err or "Forbidden" in last_err
        if is_403 and attempt < DOWNLOAD_RETRIES:
            time.sleep(RETRY_WAIT_SECONDS)
            continue
        # Non-403 failure, or last attempt: surface yt-dlp's real error.
        raise CutError(f"yt-dlp falló: {last_err or 'sin mensaje de error'}")
    raise CutError(f"yt-dlp falló tras {DOWNLOAD_RETRIES} intentos: {last_err}")


def convert_vertical(src_path: str, out_path: str, watermark_text: str = "Clips Dominicano",
                     watermark_position: str = "br", heartbeat=None) -> None:
    """ffmpeg: blurred-background vertical 720x1280 + watermark, faststart."""
    x, y = WATERMARK_POS.get(watermark_position, WATERMARK_POS["br"])
    text = _escape_drawtext(watermark_text)
    font = _font_path()
    vf = (
        "[0:v]split[a][b];"
        "[a]scale=720:1280:force_original_aspect_ratio=increase,"
        "crop=720:1280,gblur=sigma=40[bg];"
        "[b]scale=720:1280:force_original_aspect_ratio=decrease[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2,"
        f"drawtext=fontfile={font}:text='{text}':fontsize=28:fontcolor=white:"
        f"borderw=2:bordercolor=black@0.6:x={x}:y={y},"
        "format=yuv420p,fps=30"
    )
    cmd = [
        "ffmpeg", "-y", "-i", src_path,
        "-filter_complex", vf,
        "-map", "0:a?", "-c:v", "libx264", "-crf", "20",
        "-preset", "medium", "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart", out_path,
    ]
    if heartbeat:
        heartbeat("converting", "Convirtiendo a vertical 9:16…", progress=65)
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, timeout=CONVERT_TIMEOUT)
    if proc.returncode != 0 or not os.path.exists(out_path) \
            or os.path.getsize(out_path) == 0:
        err = (proc.stderr or "").strip()[-1500:]
        raise CutError(f"ffmpeg falló: {err or 'sin mensaje de error'}")


def probe_duration(path: str) -> float:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60)
    try:
        return float(proc.stdout.strip())
    except (ValueError, TypeError):
        return 0.0


def run_cut(job_dir: str, live_url: str, start_sec: int, end_sec: int,
            watermark_text: str = "Clips Dominicano",
            watermark_position: str = "br", heartbeat=None) -> CutResult:
    """Full pipeline for one job. Raises CutError with the real error."""
    os.makedirs(job_dir, exist_ok=True)
    deadline = time.time() + MAX_JOB_SECONDS
    src = os.path.join(job_dir, "src.mp4")
    out = os.path.join(job_dir, "cut.mp4")
    download_segment(live_url, start_sec, end_sec, src,
                     heartbeat=heartbeat, deadline=deadline)
    if time.time() > deadline:
        raise CutError("Tiempo agotado (12 min) antes de convertir")
    convert_vertical(src, out, watermark_text, watermark_position,
                     heartbeat=heartbeat)
    duration = probe_duration(out)
    return CutResult(output_path=out, bytes=os.path.getsize(out),
                     duration_sec=duration)


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 5:
        print("Uso: python3 worker.py <liveUrl> <startSec> <endSec> <out.mp4> [watermark]")
        raise SystemExit(2)
    url, start, end, out_path = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
    wm = sys.argv[5] if len(sys.argv) > 5 else "Clips Dominicano"
    tmpdir = os.path.join("/tmp", f"local-cut-{int(time.time())}")
    result = run_cut(tmpdir, url, start, end, watermark_text=wm,
                     heartbeat=lambda s, p, progress=0: print(f"[{s}] {p}"))
    shutil.copyfile(result.output_path, out_path)
    print(f"OK {out_path}: {result.bytes} bytes, {result.duration_sec:.1f} s")
