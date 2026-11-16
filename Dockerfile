# Clipiar Fast — SnapDeploy test app (worker-only, no Firebase)
# Same base as the Cloud Run cutter, but runs test_app.py (no Firebase
# credentials needed) so it can run on SnapDeploy's 512 MB free tier.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core ca-certificates nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY test_app.py worker.py ./
RUN chmod 644 /app/test_app.py /app/worker.py
RUN useradd -m cutter && mkdir -p /tmp/snap-test-jobs && chown cutter:cutter /tmp/snap-test-jobs
USER cutter

EXPOSE 8080
CMD ["python", "test_app.py"]
