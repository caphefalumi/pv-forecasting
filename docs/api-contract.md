# Prediction Serving & API Contract

| | |
|---|---|
| **Service** | `solar-api` (`apps/backend/`) |
| **Runtime** | Python 3.12+, FastAPI, Pydantic v2, PyTorch CPU, Uvicorn |
| **Base URL (Local)** | `http://127.0.0.1:8000` |
| **Interactive OpenAPI** | `GET /docs` (Swagger UI) and `GET /redoc` |

---

## 1. Request Lifecycle & Headers

Every HTTP request processed by `apps/backend/main.py` passes through the observability and correlation middleware:

1. **Correlation ID (`X-Request-ID`):** If the caller supplies an `X-Request-ID` header (e.g., from the frontend axios client in `apps/frontend/src/lib/api-client.ts`), it is preserved; otherwise, a UUIDv4 identifier is generated.
2. **Latency Tracking (`X-Process-Time-Ms`):** Request execution duration is measured in high-resolution monotonic time, recorded in the Prometheus histogram `http_request_duration_seconds_bucket`, and returned in the `X-Process-Time-Ms` response header.
3. **Structured Error Handling:** Unhandled exceptions are caught by the global exception handler, logged with `request_id`, counted in `http_requests_total{status="500"}`, and returned as a sanitized JSON payload without exposing internal stack traces.

---

## 2. Endpoints

### 2.1 `POST /api/v1/forecast` — Generate Day-Ahead (D+1) Forecast

Computes the 24-hour solar energy generation profile (kWh) for the day following `predict_date` (`target_date = predict_date + 1 day`).

#### Request Body (`ForecastRequest`)

| Field | Type | Constraints / Enum | Default | Description |
|---|---|---|---|---|
| `kwp` | `float` | `> 0` | `10.0` | Installed DC peak capacity in kWp |
| `technology` | `string` | `mono-Si`, `poly-Si`, `CdTe`, `CIGS`, `HIT`, `a-Si`, `CPV` | `"mono-Si"` | PV cell technology |
| `tracking` | `string` | `fixed`, `single_axis`, `dual_axis` | `"fixed"` | Mounting / tracking system type |
| `tilt_deg` | `float` | `0 <= tilt_deg <= 90` | `20.0` | Panel tilt angle in degrees |
| `surface_azimuth_pvlib` | `float` | `-180 <= azimuth <= 360` | `0.0` | Surface azimuth angle ($0^\circ$ = North) |
| `predict_date` | `string \| null` | `YYYY-MM-DD` | Today | Request date; forecast targets `predict_date + 1 day` |
| `latitude` | `float \| null` | Optional | `-23.7624` | Site latitude (defaults to DKASC Alice Springs) |
| `longitude` | `float \| null` | Optional | `133.8754` | Site longitude (defaults to DKASC Alice Springs) |

#### Example Request

```bash
curl -X POST http://127.0.0.1:8000/api/v1/forecast \
  -H "Content-Type: application/json" \
  -H "X-Request-ID: demo-req-001" \
  -d '{
    "kwp": 15.0,
    "technology": "mono-Si",
    "tracking": "fixed",
    "tilt_deg": 20.0,
    "surface_azimuth_pvlib": 0.0,
    "predict_date": "2026-10-18"
  }'
```

#### Response Body (`ForecastResponse` — `200 OK`)

```json
{
  "predict_date": "2026-10-18",
  "target_date": "2026-10-19",
  "kwp": 15.0,
  "total_daily_kwh": 68.42,
  "peak_hour": "12:00",
  "peak_kwh": 9.75,
  "daytime_hours_count": 13,
  "model_version": "v1.0-mock",
  "is_ood": false,
  "hourly_breakdown": [
    {
      "hour": "00:00",
      "ghi_fc": 0.0,
      "target_norm": 0.0,
      "kwh": 0.0
    },
    {
      "hour": "12:00",
      "ghi_fc": 950.0,
      "target_norm": 0.65,
      "kwh": 9.75
    }
  ]
}
```

#### Error Responses

| Status Code | Condition | Example Response |
|---|---|---|
| `400 Bad Request` | Malformed `predict_date` string | `{"detail": "Invalid predict_date format. Use YYYY-MM-DD."}` |
| `422 Unprocessable Entity` | Physical boundary or enum violation (`kwp <= 0`, `tilt_deg > 90`, invalid `technology`) | Standard FastAPI / Pydantic validation error array |
| `500 Internal Server Error` | Unhandled runtime error | `{"detail": "Internal server error", "request_id": "..."}` |

---

### 2.2 `GET /health` — ALB & Container Health Check

Used by the AWS Application Load Balancer target group and Docker `HEALTHCHECK` directive.

```json
{
  "status": "ok",
  "model_loaded": true,
  "model_source": "MLflow Champion (version 3, run 8f3a...)"
}
```

---

### 2.3 `GET /api/v1/model/info` & `POST /api/v1/model/reload`

- `GET /api/v1/model/info`: Returns the currently active `model_version`, `model_source`, and readiness flag (`is_ready`).
- `POST /api/v1/model/reload`: Forces an immediate check and download of the `@champion` alias from the MLflow Registry without restarting the FastAPI container or dropping in-flight requests. In normal operation, `ModelManager` also checks for newly promoted Champions automatically every 60 seconds.

---

### 2.4 `GET /metrics` — Prometheus Telemetry Exposition

Returns all service, data-quality, and model metrics in standard Prometheus text format (`text/plain; version=0.0.4`):

- `http_requests_total{service="solar-api",status="200|400|422|500"}`
- `http_request_duration_seconds_bucket{service="solar-api",le="..."}`
- `pv_user_ood_rate` — fraction of user requests outside the DKASC training distribution (`kwp` outside `[1.0, 100.0]` or `tilt_deg` outside `[0.0, 45.0]`)
- `pv_model_nmae{cohort="all|train_25|held_out_6"}`
- `pv_error_attribution_kwh{source="e_model|e_forecast"}`
- `drift_score{site="dkasc",feature="ghi_fc|dni_fc|temp_fc|cloud_fc|poa_fc"}`
- `pv_ingestion_schema_violations_total` and `pv_ingestion_range_flags_total{flag_type="..."}`
- `pv_job_duration_seconds{job="solar-ingest|solar-evaluate|solar-drift|solar-retrain"}`

---

## 3. Supabase Prediction Logging (`prediction_logs`)

Every completed `POST /api/v1/forecast` call dispatches an asynchronous FastAPI `BackgroundTask` (`log_prediction_to_supabase` in `apps/backend/services/supabase_service.py`) that inserts a structured record into the Supabase `prediction_logs` table:

| Column | Type | Description |
|---|---|---|
| `predict_date` | `date` | Date the prediction was requested |
| `target_date` | `date` | Target forecast date (`D+1`) |
| `kwp` | `float8` | Array capacity in kWp |
| `technology` | `text` | Cell technology enum value |
| `tracking` | `text` | Tracking configuration |
| `tilt_deg` | `float8` | Surface tilt angle |
| `surface_azimuth_pvlib` | `float8` | Surface azimuth angle |
| `model_version` | `text` | Registered MLflow model version that served the request |
| `total_daily_kwh` | `float8` | Predicted 24-hour energy total (kWh) |
| `peak_hour` | `text` | Hour of maximum generation (`HH:00`) |
| `peak_kwh` | `float8` | Peak hourly generation (kWh) |
| `inputs` | `jsonb` | Full user input payload including `is_ood` boolean flag |
| `weather_forecast` | `jsonb` | 24-hour Open-Meteo weather forecast arrays used at inference time |
| `hourly_predictions` | `jsonb` | Array of 24 `{hour, ghi_fc, target_norm, kwh}` objects |
