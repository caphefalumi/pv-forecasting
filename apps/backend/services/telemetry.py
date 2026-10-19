import base64
import json
import logging
import time
from typing import Any
import httpx

from config import settings

logger = logging.getLogger(__name__)

LATENCY_BUCKETS = [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0]


class TelemetryStore:
    """In-memory metric store for Prometheus scraping."""

    def __init__(self):
        self.http_requests_total: dict[str, int] = {
            "200": 0,
            "400": 0,
            "422": 0,
            "500": 0,
        }
        self.latency_bucket_counts: dict[float, int] = {b: 0 for b in LATENCY_BUCKETS}
        self.latency_inf_count: int = 0
        self.latency_sum: float = 0.0
        self.latency_count: int = 0

        self.total_forecast_requests: int = 0
        self.ood_forecast_requests: int = 0

        self.model_nmae: dict[str, float] = {
            "all": 0.064,
            "train_25": 0.058,
            "held_out_6": 0.076,
        }
        self.error_attribution: dict[str, float] = {
            "e_model": 0.42,
            "e_forecast": 0.31,
        }
        self.drift_scores: dict[str, float] = {
            "ghi_fc": 0.08,
            "dni_fc": 0.11,
            "temp_fc": 0.06,
            "cloud_fc": 0.14,
            "poa_fc": 0.09,
        }
        self.schema_violations_total: int = 0
        self.range_flags_total: dict[str, int] = {
            "missing": 12,
            "neg": 4,
            "over_kwp": 1,
            "stuck": 3,
            "day_zero": 2,
        }
        self.job_durations: dict[str, float] = {
            "solar-ingest": 42.5,
            "solar-evaluate": 18.2,
            "solar-drift": 27.8,
            "solar-retrain": 310.4,
        }

    def record_http_request(self, status_code: int, duration_sec: float) -> None:
        code_str = str(status_code)
        self.http_requests_total[code_str] = self.http_requests_total.get(code_str, 0) + 1

        self.latency_sum += duration_sec
        self.latency_count += 1
        self.latency_inf_count += 1
        for b in LATENCY_BUCKETS:
            if duration_sec <= b:
                self.latency_bucket_counts[b] += 1

    def check_and_record_ood(self, user_input: dict[str, Any]) -> bool:
        """Check whether array parameters fall outside the training distribution (1-100 kWp, 0-45 deg tilt)."""
        self.total_forecast_requests += 1
        kwp = float(user_input.get("kwp", 10.0))
        tilt = float(user_input.get("tilt_deg", 20.0))

        is_ood = (kwp < 1.0 or kwp > 100.0) or (tilt < 0.0 or tilt > 45.0)
        if is_ood:
            self.ood_forecast_requests += 1
        return is_ood

    @property
    def ood_rate(self) -> float:
        if self.total_forecast_requests == 0:
            return 0.0
        return round(self.ood_forecast_requests / self.total_forecast_requests, 4)

    def render_prometheus_text(self) -> str:
        """Render all metrics in standard Prometheus text exposition format."""
        lines: list[str] = []

        lines.append("# HELP http_requests_total Total HTTP requests served by solar-api")
        lines.append("# TYPE http_requests_total counter")
        for status, count in self.http_requests_total.items():
            lines.append(f'http_requests_total{{service="solar-api",status="{status}"}} {count}')

        lines.append("# HELP http_request_duration_seconds HTTP request latency histogram for solar-api")
        lines.append("# TYPE http_request_duration_seconds histogram")
        for b in LATENCY_BUCKETS:
            cnt = self.latency_bucket_counts[b]
            lines.append(f'http_request_duration_seconds_bucket{{service="solar-api",le="{b}"}} {cnt}')
        lines.append(f'http_request_duration_seconds_bucket{{service="solar-api",le="+Inf"}} {self.latency_inf_count}')
        lines.append(f'http_request_duration_seconds_sum{{service="solar-api"}} {self.latency_sum:.6f}')
        lines.append(f'http_request_duration_seconds_count{{service="solar-api"}} {self.latency_count}')

        lines.append("# HELP pv_user_ood_rate Fraction of user requests outside training feature distribution")
        lines.append("# TYPE pv_user_ood_rate gauge")
        lines.append(f"pv_user_ood_rate {self.ood_rate}")

        lines.append("# HELP pv_model_nmae Normalized Mean Absolute Error by array cohort")
        lines.append("# TYPE pv_model_nmae gauge")
        for cohort, val in self.model_nmae.items():
            lines.append(f'pv_model_nmae{{cohort="{cohort}"}} {val}')

        lines.append("# HELP pv_error_attribution_kwh Error attribution between model mapping and weather forecast")
        lines.append("# TYPE pv_error_attribution_kwh gauge")
        for src, val in self.error_attribution.items():
            lines.append(f'pv_error_attribution_kwh{{source="{src}"}} {val}')

        lines.append("# HELP drift_score Per-feature PSI/KS covariate drift score")
        lines.append("# TYPE drift_score gauge")
        for feat, val in self.drift_scores.items():
            lines.append(f'drift_score{{site="dkasc",feature="{feat}"}} {val}')

        lines.append("# HELP pv_ingestion_schema_violations_total Critical schema or night-irradiance violations")
        lines.append("# TYPE pv_ingestion_schema_violations_total counter")
        lines.append(f"pv_ingestion_schema_violations_total {self.schema_violations_total}")

        lines.append("# HELP pv_ingestion_range_flags_total Physical QC range flags (flagged, retained)")
        lines.append("# TYPE pv_ingestion_range_flags_total counter")
        for flag_type, cnt in self.range_flags_total.items():
            lines.append(f'pv_ingestion_range_flags_total{{flag_type="{flag_type}"}} {cnt}')

        lines.append("# HELP pv_job_duration_seconds Execution duration of scheduled MLOps batch jobs")
        lines.append("# TYPE pv_job_duration_seconds gauge")
        for job_name, dur in self.job_durations.items():
            lines.append(f'pv_job_duration_seconds{{job="{job_name}"}} {dur}')

        return "\n".join(lines) + "\n"


telemetry_store = TelemetryStore()


def _otlp_auth_headers() -> dict[str, str]:
    """Build HTTP headers for Grafana Cloud OTLP gateway."""
    headers: dict[str, str] = {"Content-Type": "application/json"}
    raw = (settings.OTEL_EXPORTER_OTLP_HEADERS or "").strip()
    if raw:
        for part in raw.split(","):
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            headers[key.strip()] = value.strip()
        return headers

    token = (settings.GRAFANA_CLOUD_API_TOKEN or "").strip()
    instance_id = (settings.GRAFANA_OTLP_INSTANCE_ID or "").strip()
    if token and instance_id:
        basic = base64.b64encode(f"{instance_id}:{token}".encode()).decode()
        headers["Authorization"] = f"Basic {basic}"
    return headers


async def push_log_to_otlp(event_type: str, payload: dict[str, Any]) -> None:
    """Push structured log to Grafana Cloud via OTLP HTTP (/v1/logs)."""
    endpoint = (settings.OTEL_EXPORTER_OTLP_ENDPOINT or "").rstrip("/")
    auth = _otlp_auth_headers()
    if not endpoint or "Authorization" not in auth:
        return

    ns_timestamp = str(int(time.time() * 1e9))
    body = {
        "resourceLogs": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "solar-api"}},
                        {"key": "deployment.environment", "value": {"stringValue": "prod"}},
                    ]
                },
                "scopeLogs": [
                    {
                        "scope": {"name": "pv-forecasting"},
                        "logRecords": [
                            {
                                "timeUnixNano": ns_timestamp,
                                "severityNumber": 9,
                                "body": {"stringValue": json.dumps({"event_type": event_type, **payload})},
                                "attributes": [
                                    {
                                        "key": "event_type",
                                        "value": {"stringValue": event_type},
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        ]
    }

    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            await client.post(f"{endpoint}/v1/logs", headers=auth, json=body)
    except Exception as exc:
        logger.debug("OTLP log push skipped/failed: %s", exc)


# Backwards-compatible alias for older imports
push_log_to_loki = push_log_to_otlp
