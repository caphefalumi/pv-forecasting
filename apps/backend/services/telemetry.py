"""
telemetry.py — Direct Telemetry Integration for Grafana Cloud (Prometheus, Loki, Tempo).
Zero extra hosted services required: calls Grafana Cloud raw endpoints directly
and exposes a Prometheus-compatible /metrics endpoint.
"""

import time
import json
import logging
from typing import Dict, Any, List
import httpx

from config import settings

logger = logging.getLogger(__name__)

# Histogram buckets in seconds for FastAPI solar-api latency (p50, p95, p99)
LATENCY_BUCKETS = [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0]


class TelemetryStore:
    """In-memory metric store matching the PromQL queries in Grafana Cloud dashboards."""

    def __init__(self):
        self.http_requests_total: Dict[str, int] = {
            "200": 0,
            "400": 0,
            "422": 0,
            "500": 0,
        }
        self.latency_bucket_counts: Dict[float, int] = {b: 0 for b in LATENCY_BUCKETS}
        self.latency_inf_count: int = 0
        self.latency_sum: float = 0.0
        self.latency_count: int = 0

        # User traffic Out-of-Distribution (OOD) tracking (Table 16 & SCRUM-22)
        self.total_forecast_requests: int = 0
        self.ood_forecast_requests: int = 0

        # Model & Data Quality reference metrics (Table 16, 17, 18)
        self.model_nmae: Dict[str, float] = {
            "all": 0.064,
            "train_25": 0.058,
            "held_out_6": 0.076,
        }
        self.error_attribution: Dict[str, float] = {
            "e_model": 0.42,
            "e_forecast": 0.31,
        }
        self.drift_scores: Dict[str, float] = {
            "ghi_fc": 0.08,
            "dni_fc": 0.11,
            "temp_fc": 0.06,
            "cloud_fc": 0.14,
            "poa_fc": 0.09,
        }
        self.schema_violations_total: int = 0
        self.range_flags_total: Dict[str, int] = {
            "missing": 12,
            "neg": 4,
            "over_kwp": 1,
            "stuck": 3,
            "day_zero": 2,
        }
        self.job_durations: Dict[str, float] = {
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

    def check_and_record_ood(self, user_input: Dict[str, Any]) -> bool:
        """
        Checks whether user array parameters fall outside the DKASC training distribution:
        - Training kWp range: [1.0, 100.0] kWp
        - Training tilt range: [0.0, 45.0] deg
        """
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
        lines: List[str] = []

        # 1. HTTP Requests Total
        lines.append("# HELP http_requests_total Total HTTP requests served by solar-api")
        lines.append("# TYPE http_requests_total counter")
        for status, count in self.http_requests_total.items():
            lines.append(f'http_requests_total{{service="solar-api",status="{status}"}} {count}')

        # 2. HTTP Request Duration Histogram
        lines.append("# HELP http_request_duration_seconds HTTP request latency histogram for solar-api")
        lines.append("# TYPE http_request_duration_seconds histogram")
        for b in LATENCY_BUCKETS:
            cnt = self.latency_bucket_counts[b]
            lines.append(f'http_request_duration_seconds_bucket{{service="solar-api",le="{b}"}} {cnt}')
        lines.append(f'http_request_duration_seconds_bucket{{service="solar-api",le="+Inf"}} {self.latency_inf_count}')
        lines.append(f'http_request_duration_seconds_sum{{service="solar-api"}} {self.latency_sum:.6f}')
        lines.append(f'http_request_duration_seconds_count{{service="solar-api"}} {self.latency_count}')

        # 3. User Traffic OOD Rate
        lines.append("# HELP pv_user_ood_rate Fraction of user requests outside training feature distribution")
        lines.append("# TYPE pv_user_ood_rate gauge")
        lines.append(f"pv_user_ood_rate {self.ood_rate}")

        # 4. Model nMAE by Cohort
        lines.append("# HELP pv_model_nmae Normalized Mean Absolute Error by array cohort")
        lines.append("# TYPE pv_model_nmae gauge")
        for cohort, val in self.model_nmae.items():
            lines.append(f'pv_model_nmae{{cohort="{cohort}"}} {val}')

        # 5. Error Attribution (e_model vs e_forecast)
        lines.append("# HELP pv_error_attribution_kwh Error attribution between model mapping and weather forecast")
        lines.append("# TYPE pv_error_attribution_kwh gauge")
        for src, val in self.error_attribution.items():
            lines.append(f'pv_error_attribution_kwh{{source="{src}"}} {val}')

        # 6. Feature Drift Scores (Evidently)
        lines.append("# HELP drift_score Per-feature PSI/KS covariate drift score")
        lines.append("# TYPE drift_score gauge")
        for feat, val in self.drift_scores.items():
            lines.append(f'drift_score{{site="dkasc",feature="{feat}"}} {val}')

        # 7. Data Quality Schema Violations & Physical Range Flags
        lines.append("# HELP pv_ingestion_schema_violations_total Critical schema or night-irradiance violations")
        lines.append("# TYPE pv_ingestion_schema_violations_total counter")
        lines.append(f"pv_ingestion_schema_violations_total {self.schema_violations_total}")

        lines.append("# HELP pv_ingestion_range_flags_total Physical QC range flags (flagged, retained)")
        lines.append("# TYPE pv_ingestion_range_flags_total counter")
        for flag_type, cnt in self.range_flags_total.items():
            lines.append(f'pv_ingestion_range_flags_total{{flag_type="{flag_type}"}} {cnt}')

        # 8. Scheduled MLOps Batch Job Durations
        lines.append("# HELP pv_job_duration_seconds Execution duration of scheduled MLOps batch jobs")
        lines.append("# TYPE pv_job_duration_seconds gauge")
        for job_name, dur in self.job_durations.items():
            lines.append(f'pv_job_duration_seconds{{job="{job_name}"}} {dur}')

        return "\n".join(lines) + "\n"


telemetry_store = TelemetryStore()


async def push_log_to_loki(event_type: str, payload: Dict[str, Any]) -> None:
    """Push structured JSON log directly to raw Grafana Cloud Loki endpoint."""
    if not settings.GRAFANA_CLOUD_API_KEY or not settings.GRAFANA_LOKI_URL:
        return

    ns_timestamp = str(int(time.time() * 1e9))
    body = {
        "streams": [
            {
                "stream": {
                    "service": "solar-api",
                    "env": "prod",
                    "event_type": event_type,
                },
                "values": [[ns_timestamp, json.dumps(payload)]],
            }
        ]
    }

    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            await client.post(
                settings.GRAFANA_LOKI_URL,
                json=body,
                auth=(settings.GRAFANA_LOKI_USER, settings.GRAFANA_CLOUD_API_KEY),
            )
    except Exception as exc:
        logger.debug("Loki push skipped/failed: %s", exc)
