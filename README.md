# Clipiar Fast — SnapDeploy test (worker-only)

## What this is
A Firebase-free copy of the cutter, built only to answer one question on a
NON-Google IP: does YouTube block yt-dlp on SnapDeploy (AWS Fargate),
or was the block specific to Google Cloud Run?

Files:
- `test_app.py` — Flask app, NO Firebase/auth/Storage. Endpoints:
  `GET /healthz`, `POST /test-cut` {liveUrl, startSec, endSec},
  `GET /test-cut/<jobId>`, `GET /download/<jobId>`.
  The job reports `download_ok` / `download_bytes` / `download_error`
  SEPARATELY from the convert step, so a 512 MB ffmpeg OOM does not
  hide a successful download.
- `worker.py` — unchanged cutter pipeline (yt-dlp 134+140, sections,
  EJS/Node, ffmpeg vertical 720x1280 + "Clips Dominicano" watermark).
- `Dockerfile`, `requirements.txt` — Flask + yt-dlp only (lighter than
  the full app; no firebase-admin).

## Local validation (this VM, 2026-11-16 19:42–19:47 UTC)
- `POST /test-cut` Rochy live https://www.youtube.com/live/WUoeVvKaugw,
  0–60 s, job `test_0ce7c8eaa77d`
- Download: OK, 4,689,936 bytes (`src.mp4`)
- Convert: OK, `cut.mp4` 12,373,784 bytes
- ffprobe: duration 60.000000 s, 720x1280, h264 + aac
- NOTE: this only proves the code works on THIS VM's IP. It says
  NOTHING about AWS Fargate's IP — that still needs the SnapDeploy run.

## How to deploy on SnapDeploy (needs a browser — a generic subagent
## CANNOT do this; the parent agent / Aneudi must do it)
1. Push this folder to a GitHub repo (no repo exists yet; `gh` is not
   logged in on this VM). Alternative: build the image locally with
   Docker (Docker is NOT installed on this VM) and use SnapDeploy's
   `docker save` tarball upload.
2. Go to https://snapdeploy.dev/register — sign up with email or
   GitHub. **NO credit card** (verified in SnapDeploy's own free-tier
   docs, 2026-11-16: "No. Sign up with an email address or a GitHub
   account and deploy. A payment method is needed only when you buy
   Always-On, a $1 Sprint Pack, a database add-on or a Dedicated GPU").
3. Dashboard -> Deploy -> pick the repo/branch -> name it
   e.g. `clipiar-fast-test` -> size Small (512 MB, free) -> Deploy.
   URL will be `https://clipiar-fast-test.containers.snapdeploy.app`.
4. Wait for RUNNING (~2–6 min build). Free containers sleep after
   15 min idle; open the URL once to wake it (~60 s).
5. Run the test:
   `curl -X POST https://clipiar-fast-test.containers.snapdeploy.app/test-cut -H 'Content-Type: application/json' -d '{"liveUrl":"https://www.youtube.com/live/WUoeVvKaugw","startSec":0,"endSec":60}'`
   then poll `GET /test-cut/<jobId>` every ~30 s.
6. Read the result:
   - `download_ok: true` -> YouTube did NOT block the AWS IP. The
     Google-IP theory is confirmed; a paid VPS (~$4/mo) is worth it.
   - `download_ok: false` + error containing "Sign in to confirm
     you're not a bot" -> YouTube blocks AWS Fargate too; the problem
     is not Google-specific.
   - `download_ok: true` + `convert_error` / OOM -> download worked,
     ffmpeg ran out of 512 MB. Still proves the IP is fine; the full
     cutter needs more RAM than the free tier gives.
   - `GET /download/<jobId>` returns the finished 60 s vertical MP4
     when status is "ready".

## Limits (from SnapDeploy's own docs, verified 2026-11-16)
- Free: 4 containers, 512 MB RAM / 0.25 vCPU each, 100 container-hours
  /month, 10 deploys/day (5 per rolling 12 h), sleeps after 15 min idle.
- Medium (2 GB) / Large (4 GB) need Always-On ($12/mo) — DO NOT buy
  without Aneudi's authorization.
- No card was entered, nothing was paid, no account was created by
  this test. Firebase app and the Cloud Run service were not touched.
