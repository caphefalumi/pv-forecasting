FROM python:3.11-slim

WORKDIR /app

# Install dependencies and AWS CLI
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir awscli

# Copy source code, config, and scripts
COPY pvdata/ ./pvdata/
COPY config/ ./config/
COPY scripts/ ./scripts/
RUN chmod +x scripts/run_weekly.sh

ENTRYPOINT ["python", "-m", "pvdata"]
