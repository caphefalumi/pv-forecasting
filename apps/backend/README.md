# PV Forecasting API (`apps/backend`)

FastAPI microservice serving Day-Ahead (D+1) 24-hour photovoltaic solar energy forecasts on AWS ECS Fargate.

---

## 1. Architecture & Responsibilities

- **Framework:** FastAPI + Pydantic v2 + Uvicorn (`Python 3.12+`)
- **Model Inference:** Dynamically resolves the production `@champion` alias from the MLflow Model Registry (`pv-lstm-fleet`), downloads the PyTorch LSTM checkpoint + `scikit-learn` feature preprocessor from Supabase/S3 storage, and caches it in memory with automatic 60-second zero-downtime refresh.
- **Weather Enrichment:** Queries the Open-Meteo Forecast API for the target day (`predict_date + 1 day`), computing 23 physics-informed features (solar geometry, clear-sky GHI, plane-of-array irradiance, cell temperature, cyclical time, and array metadata encodings). Includes a deterministic clear-sky fallback for offline resilience.
- **Observability & Audit Logging:**
  - Propagates `X-Request-ID` correlation headers and `X-Process-Time-Ms` response timing.
  - Exposes Prometheus metrics at `GET /metrics` (HTTP request counters, latency histogram, user traffic Out-of-Distribution `pv_user_ood_rate`, cohort nMAE, error attribution, and feature drift scores).
  - Asynchronously logs every prediction payload, weather forecast, model version, and OOD flag to the Supabase `prediction_logs` table and Grafana Cloud Loki.

---

## 2. Directory Structure

```text
apps/backend/
├── config.py                  # Typed Pydantic Settings (env & defaults)
├── Dockerfile                 # Multi-stage uv build + non-root runtime (appuser)
├── main.py                    # FastAPI app, lifespan, CORS, correlation middleware
├── pyproject.toml             # Project dependencies and pytest configuration
├── schemas.py                 # Request/Response Pydantic v2 contracts & enums
├── routes/
│   ├── forecast.py            # POST /api/v1/forecast, GET/POST /api/v1/model/*
│   └── health.py              # GET /health, GET /metrics, GET /api/v1/telemetry/endpoints
├── services/
│   ├── model_service.py       # Champion checkpoint loader & 24h batch LSTM inference
│   ├── supabase_service.py    # Async background logging to Supabase prediction_logs
│   ├── telemetry.py           # Prometheus exposition store & Grafana Cloud Loki push
│   └── weather_service.py     # Open-Meteo day-ahead client & synthetic fallback
└── tests/
    ├── unit/                  # Schema validation & in-memory model service unit tests
    └── integration/           # Full route lifecycle, Open-Meteo, & Supabase tests
```

---

## 3. API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | AWS ALB target group health check (`status`, `model_loaded`, `model_source`) |
| `POST` | `/api/v1/forecast` | Compute D+1 24-hour PV generation profile (`kwh`, `target_norm`, `total_daily_kwh`, `is_ood`) |
| `GET` | `/api/v1/model/info` | Inspect active Champion model version and source |
| `POST` | `/api/v1/model/reload` | Force zero-downtime reload of `@champion` from MLflow Registry |
| `GET` | `/metrics` | Prometheus text exposition endpoint (`text/plain; version=0.0.4`) |
| `GET` | `/api/v1/telemetry/endpoints` | Inspect configured Grafana Cloud Prometheus/Loki/Tempo endpoints |
| `GET` | `/docs` | Interactive OpenAPI / Swagger UI |

---

## 4. Local Development & Testing

```bash
cd apps/backend

# Install dependencies via uv
uv sync --dev

# Start local development server on http://127.0.0.1:8000
uv run uvicorn main:app --reload --port 8000

# Run unit and integration test suites
uv run pytest -v
```

---

## 5. Container Build

```bash
docker build -t pv-forecasting-backend:local ./apps/backend
docker run -p 8000:8000 pv-forecasting-backend:local
```
