# ── Stage 1: Build & Dependency Installation ──────────────────────────────────
FROM python:3.11-slim-bookworm AS builder

WORKDIR /build

RUN apt-get update && \
    apt-get install -y --no-install-recommends build-essential && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt && \
    pip install --no-cache-dir --user awscli

# ── Stage 2: Minimal Hardened Production Runtime ──────────────────────────────
FROM python:3.11-slim-bookworm AS runtime

WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PATH="/home/appuser/.local/bin:$PATH"

# Create non-root unprivileged service account
RUN groupadd -g 10001 appuser && \
    useradd -u 10001 -g appuser -s /bin/bash -m appuser

# Copy installed Python packages and CLI from builder
COPY --from=builder --chown=appuser:appuser /root/.local /home/appuser/.local

# Copy application code and MLOps job modules with proper unprivileged ownership
COPY --chown=appuser:appuser pvdata/ ./pvdata/
COPY --chown=appuser:appuser jobs/ ./jobs/
COPY --chown=appuser:appuser config/ ./config/
COPY --chown=appuser:appuser scripts/ ./scripts/
COPY --chown=appuser:appuser ingest.py evaluate.py drift.py retrain.py promote.py ./

RUN chmod +x scripts/run_weekly.sh && \
    chown appuser:appuser /app

USER appuser

# ECS task definitions override command per job:
#   python ingest.py | evaluate.py | drift.py | retrain.py
ENTRYPOINT []
CMD ["python", "ingest.py"]
