"""OpenTelemetry SDK setup: traces, metrics and logs over OTLP/HTTP (protobuf) to Grafana Cloud.

Export is enabled only when OTLP auth is configured (OTEL_EXPORTER_OTLP_HEADERS, or
GRAFANA_CLOUD_API_TOKEN + GRAFANA_OTLP_INSTANCE_ID) and OTEL_SDK_DISABLED is not "true".
Otherwise nothing is installed and the app behaves exactly as before.

Metric names are chosen so Grafana Cloud's OTLP -> Prometheus translation produces the
names the existing dashboards query: counters get ``_total`` appended, the ``s`` unit adds
``_seconds``, and gauges carry no unit (a unit would add a suffix).
"""

import logging
import os
import uuid
from collections.abc import Iterable

from fastapi import FastAPI
from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.logging.handler import LoggingHandler
from opentelemetry.metrics import CallbackOptions, Meter, NoOpMeterProvider, Observation
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from config import settings
from services.telemetry import (
    LATENCY_BUCKETS,
    TelemetryStore,
    _otlp_auth_headers,
    event_logger,
    telemetry_store,
)

logger = logging.getLogger(__name__)

METER_NAME = "pv-forecasting"
# Health checks and scrapes would only add noise to traces.
EXCLUDED_URLS = "/health,/metrics"

_state: dict = {}


def otlp_export_headers() -> dict[str, str]:
    """Auth headers for the OTLP exporters (the SDK sets its own Content-Type)."""
    return {k: v for k, v in _otlp_auth_headers().items() if k.lower() != "content-type"}


def otel_enabled() -> bool:
    if os.environ.get("OTEL_SDK_DISABLED", "").strip().lower() == "true":
        return False
    if not (settings.OTEL_EXPORTER_OTLP_ENDPOINT or "").strip():
        return False
    return "Authorization" in otlp_export_headers()


def build_resource() -> Resource:
    return Resource.create(
        {
            "service.name": settings.OTEL_SERVICE_NAME,
            # Unique per process so several ECS tasks never write the same series.
            "service.instance.id": str(uuid.uuid4()),
            # Kept as `deployment.environment` so the existing Loki label is unchanged.
            "deployment.environment": "prod",
        }
    )


def register_metrics(meter: Meter, store: TelemetryStore, service: str) -> None:
    """Mirror the /metrics series as OTel instruments (single source: ``store``)."""

    def http_requests(_: CallbackOptions) -> Iterable[Observation]:
        for status, count in list(store.http_requests_total.items()):
            yield Observation(count, {"service": service, "status": status})

    def ood_rate(_: CallbackOptions) -> Iterable[Observation]:
        yield Observation(store.ood_rate)

    def model_nmae(_: CallbackOptions) -> Iterable[Observation]:
        for cohort, val in store.model_nmae.items():
            yield Observation(val, {"cohort": cohort})

    def error_attribution(_: CallbackOptions) -> Iterable[Observation]:
        for src, val in store.error_attribution.items():
            yield Observation(val, {"source": src})

    def drift(_: CallbackOptions) -> Iterable[Observation]:
        for feat, val in store.drift_scores.items():
            yield Observation(val, {"site": "dkasc", "feature": feat})

    def schema_violations(_: CallbackOptions) -> Iterable[Observation]:
        yield Observation(store.schema_violations_total)

    def range_flags(_: CallbackOptions) -> Iterable[Observation]:
        for flag_type, cnt in store.range_flags_total.items():
            yield Observation(cnt, {"flag_type": flag_type})

    # -> http_requests_total{service,status}
    meter.create_observable_counter(
        "http_requests", callbacks=[http_requests], description="Total HTTP requests served by solar-api"
    )
    # -> http_request_duration_seconds_{bucket,sum,count}{service}
    histogram = meter.create_histogram(
        "http_request_duration",
        unit="s",
        description="HTTP request latency histogram for solar-api",
        explicit_bucket_boundaries_advisory=LATENCY_BUCKETS,
    )
    store.duration_observer = lambda d: histogram.record(d, {"service": service})

    meter.create_observable_gauge(
        "pv_user_ood_rate",
        callbacks=[ood_rate],
        description="Fraction of user requests outside training feature distribution",
    )
    meter.create_observable_gauge(
        "pv_model_nmae", callbacks=[model_nmae], description="Normalized Mean Absolute Error by array cohort"
    )
    meter.create_observable_gauge(
        "pv_error_attribution_kwh",
        callbacks=[error_attribution],
        description="Error attribution between model mapping and weather forecast",
    )
    meter.create_observable_gauge(
        "drift_score", callbacks=[drift], description="Per-feature PSI/KS covariate drift score"
    )
    # -> pv_ingestion_schema_violations_total / pv_ingestion_range_flags_total{flag_type}
    meter.create_observable_counter(
        "pv_ingestion_schema_violations",
        callbacks=[schema_violations],
        description="Critical schema or night-irradiance violations",
    )
    meter.create_observable_counter(
        "pv_ingestion_range_flags",
        callbacks=[range_flags],
        description="Physical QC range flags (flagged, retained)",
    )
    # TODO: pv_job_duration_seconds is intentionally NOT exported over OTLP (it stays on
    # /metrics). Its `job` attribute clashes with the `job` label Grafana Cloud derives
    # from service.name, so the per-job series would collide. Rename the attribute (e.g.
    # `job_name`) and update the dashboard query before adding it here.


def setup_otel(app: FastAPI) -> bool:
    """Install OTel providers, exporters and instrumentation. Returns True if enabled."""
    if _state:
        return True
    if not otel_enabled():
        logger.info("OpenTelemetry export disabled (no OTLP auth configured).")
        return False

    protocol = (settings.OTEL_EXPORTER_OTLP_PROTOCOL or "").strip()
    if protocol and protocol != "http/protobuf":
        logger.warning("OTEL_EXPORTER_OTLP_PROTOCOL=%s is not supported; using http/protobuf.", protocol)

    base = settings.OTEL_EXPORTER_OTLP_ENDPOINT.strip().rstrip("/")
    headers = otlp_export_headers()
    resource = build_resource()

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{base}/v1/traces", headers=headers))
    )
    trace.set_tracer_provider(tracer_provider)

    reader = PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=f"{base}/v1/metrics", headers=headers))
    meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
    metrics.set_meter_provider(meter_provider)
    register_metrics(meter_provider.get_meter(METER_NAME), telemetry_store, settings.OTEL_SERVICE_NAME)

    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(
        BatchLogRecordProcessor(OTLPLogExporter(endpoint=f"{base}/v1/logs", headers=headers))
    )
    log_handler = LoggingHandler(level=logging.INFO, logger_provider=logger_provider)
    event_logger.addHandler(log_handler)

    # Traces only from auto-instrumentation: its HTTP metrics would add series that no
    # dashboard queries, so it gets a no-op meter provider.
    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=tracer_provider,
        meter_provider=NoOpMeterProvider(),
        excluded_urls=EXCLUDED_URLS,
    )
    HTTPXClientInstrumentor().instrument(tracer_provider=tracer_provider, meter_provider=NoOpMeterProvider())

    _state.update(
        tracer_provider=tracer_provider,
        meter_provider=meter_provider,
        logger_provider=logger_provider,
        log_handler=log_handler,
    )
    logger.info("OpenTelemetry export enabled for service %s.", settings.OTEL_SERVICE_NAME)
    return True


def shutdown_otel() -> None:
    """Flush and shut down providers (no-op when OTel was never enabled)."""
    if not _state:
        return
    event_logger.removeHandler(_state["log_handler"])
    telemetry_store.duration_observer = None
    for key in ("tracer_provider", "meter_provider", "logger_provider"):
        try:
            _state[key].shutdown()
        except Exception as exc:  # noqa: BLE001 - never block app shutdown on telemetry
            logger.debug("OTel %s shutdown failed: %s", key, exc)
    _state.clear()
