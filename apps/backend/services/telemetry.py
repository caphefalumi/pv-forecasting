import base64
import json
import logging
from collections.abc import Callable
from typing import Any
from urllib.parse import unquote

from config import settings

logger = logging.getLogger(__name__)

# Structured application events (e.g. forecast_served). services.otel attaches the
# OpenTelemetry LoggingHandler here when OTLP export is configured; otherwise the
# events are dropped, exactly as before. propagate=False keeps them off stdout.
EVENT_LOGGER_NAME = "pv-forecasting"
event_logger = logging.getLogger(EVENT_LOGGER_NAME)
event_logger.setLevel(logging.INFO)
event_logger.propagate = False

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

        # Model and data-quality series. The batch jobs (ingest / evaluate / drift /
        # retrain) run as separate ECS tasks and push their real results to Grafana
        # themselves over OTLP (jobs/common/otel_metrics.py), so nothing in production
        # calls the setters below. They are kept for in-process use and tests; while
        # unset they are not exported on /metrics or OTLP: no placeholder values.
        self.model_nmae: dict[str, float] = {}
        self.error_attribution: dict[str, float] = {}
        self.drift_scores: dict[str, float] = {}
        self.schema_violations_total: int | None = None
        self.range_flags_total: dict[str, int] = {}
        self.job_durations: dict[str, float] = {}
        # Set by services.otel to mirror request latency into the OTel histogram.
        self.duration_observer: Callable[[float], None] | None = None

    def record_http_request(self, status_code: int, duration_sec: float) -> None:
        code_str = str(status_code)
        self.http_requests_total[code_str] = self.http_requests_total.get(code_str, 0) + 1

        self.latency_sum += duration_sec
        self.latency_count += 1
        self.latency_inf_count += 1
        for b in LATENCY_BUCKETS:
            if duration_sec <= b:
                self.latency_bucket_counts[b] += 1

        if self.duration_observer is not None:
            self.duration_observer(duration_sec)

    def set_model_nmae(self, cohort: str, value: float) -> None:
        self.model_nmae[cohort] = float(value)

    def set_error_attribution(self, source: str, value_kwh: float) -> None:
        self.error_attribution[source] = float(value_kwh)

    def set_drift_score(self, feature: str, value: float) -> None:
        self.drift_scores[feature] = float(value)

    def add_schema_violations(self, count: int = 1) -> None:
        if count < 0:
            raise ValueError("counter increments must be non-negative")
        self.schema_violations_total = (self.schema_violations_total or 0) + count

    def add_range_flags(self, flag_type: str, count: int = 1) -> None:
        if count < 0:
            raise ValueError("counter increments must be non-negative")
        self.range_flags_total[flag_type] = self.range_flags_total.get(flag_type, 0) + count

    def set_job_duration(self, job: str, seconds: float) -> None:
        self.job_durations[job] = float(seconds)

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

        # Model / data-quality series are omitted entirely while unset.
        if self.model_nmae:
            lines.append("# HELP pv_model_nmae Normalized Mean Absolute Error by array cohort")
            lines.append("# TYPE pv_model_nmae gauge")
            for cohort, val in self.model_nmae.items():
                lines.append(f'pv_model_nmae{{cohort="{cohort}"}} {val}')

        if self.error_attribution:
            lines.append("# HELP pv_error_attribution_kwh Error attribution between model mapping and weather forecast")
            lines.append("# TYPE pv_error_attribution_kwh gauge")
            for src, val in self.error_attribution.items():
                lines.append(f'pv_error_attribution_kwh{{source="{src}"}} {val}')

        if self.drift_scores:
            lines.append("# HELP drift_score Per-feature PSI/KS covariate drift score")
            lines.append("# TYPE drift_score gauge")
            for feat, val in self.drift_scores.items():
                lines.append(f'drift_score{{site="dkasc",feature="{feat}"}} {val}')

        if self.schema_violations_total is not None:
            lines.append("# HELP pv_ingestion_schema_violations_total Critical schema or night-irradiance violations")
            lines.append("# TYPE pv_ingestion_schema_violations_total counter")
            lines.append(f"pv_ingestion_schema_violations_total {self.schema_violations_total}")

        if self.range_flags_total:
            lines.append("# HELP pv_ingestion_range_flags_total Physical QC range flags (flagged, retained)")
            lines.append("# TYPE pv_ingestion_range_flags_total counter")
            for flag_type, cnt in self.range_flags_total.items():
                lines.append(f'pv_ingestion_range_flags_total{{flag_type="{flag_type}"}} {cnt}')

        if self.job_durations:
            lines.append("# HELP pv_job_duration_seconds Execution duration of scheduled MLOps batch jobs")
            lines.append("# TYPE pv_job_duration_seconds gauge")
            for job_name, dur in self.job_durations.items():
                lines.append(f'pv_job_duration_seconds{{job="{job_name}"}} {dur}')

        return "\n".join(lines) + "\n"


telemetry_store = TelemetryStore()


def _otlp_auth_headers() -> dict[str, str]:
    """Auth headers for the Grafana Cloud OTLP gateway (used by services.otel).

    Values are URL-decoded, so both ``Authorization=Basic <tok>`` and the
    W3C-encoded ``Authorization=Basic%20<tok>`` work.
    """
    headers: dict[str, str] = {}
    raw = (settings.OTEL_EXPORTER_OTLP_HEADERS or "").strip()
    if raw:
        for part in raw.split(","):
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            headers[key.strip()] = unquote(value.strip())
        return headers

    token = (settings.GRAFANA_CLOUD_API_TOKEN or "").strip()
    instance_id = (settings.GRAFANA_OTLP_INSTANCE_ID or "").strip()
    if token and instance_id:
        basic = base64.b64encode(f"{instance_id}:{token}".encode()).decode()
        headers["Authorization"] = f"Basic {basic}"
    return headers


async def push_log_to_otlp(event_type: str, payload: dict[str, Any]) -> None:
    """Emit a structured event log; exported to Grafana Cloud Loki via the OTel SDK.

    Body, ``event_type`` attribute, scope name (``pv-forecasting``) and resource
    (service.name / deployment.environment) match the previous hand-built OTLP JSON.
    """
    event_logger.info(
        json.dumps({"event_type": event_type, **payload}),
        extra={"event_type": event_type},
    )


# Backwards-compatible alias for older imports
push_log_to_loki = push_log_to_otlp
