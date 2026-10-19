import json
import logging

import pytest
from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.instrumentation.logging.handler import LoggingHandler
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import HistogramDataPoint, InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider as SdkTracerProvider

from config import settings
from services import otel, telemetry
from services.telemetry import LATENCY_BUCKETS, TelemetryStore

EXPECTED_INSTRUMENTS = {
    # name: unit  (Grafana: counters + "_total", unit "s" + "_seconds", gauges unchanged)
    "http_requests": "",
    "http_request_duration": "s",
    "pv_user_ood_rate": "",
    "pv_model_nmae": "",
    "pv_error_attribution_kwh": "",
    "drift_score": "",
    "pv_ingestion_schema_violations": "",
    "pv_ingestion_range_flags": "",
}


@pytest.fixture
def no_otlp_auth(monkeypatch):
    monkeypatch.setattr(settings, "OTEL_EXPORTER_OTLP_HEADERS", "")
    monkeypatch.setattr(settings, "GRAFANA_CLOUD_API_TOKEN", "")
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)


@pytest.fixture
def sdk_enabled(monkeypatch):
    # In-memory providers only (nothing is exported). The SDK itself honours
    # OTEL_SDK_DISABLED, which local runs may set to keep real export off.
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)


def _collect(store: TelemetryStore):
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    otel.register_metrics(provider.get_meter("test"), store, "solar-api")
    return reader, provider


def _metrics_by_name(reader):
    data = reader.get_metrics_data()
    return {m.name: m for rm in data.resource_metrics for sm in rm.scope_metrics for m in sm.metrics}


def test_otel_disabled_without_headers(no_otlp_auth):
    assert otel.otel_enabled() is False
    assert otel.setup_otel(FastAPI()) is False
    assert otel._state == {}
    assert not isinstance(trace.get_tracer_provider(), SdkTracerProvider)
    assert telemetry.telemetry_store.duration_observer is None
    assert not any(isinstance(h, LoggingHandler) for h in telemetry.event_logger.handlers)
    otel.shutdown_otel()  # no-op, must not raise


def test_otel_sdk_disabled_env_wins(monkeypatch):
    monkeypatch.setattr(settings, "OTEL_EXPORTER_OTLP_HEADERS", "Authorization=Basic abc")
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    assert otel.otel_enabled() is False


def test_export_headers_accept_literal_space(monkeypatch):
    monkeypatch.setattr(settings, "OTEL_EXPORTER_OTLP_HEADERS", "Authorization=Basic abc=")
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    assert otel.otel_enabled() is True
    assert otel.otlp_export_headers() == {"Authorization": "Basic abc="}


def test_resource_keeps_service_name_and_environment():
    attrs = otel.build_resource().attributes
    assert attrs["service.name"] == "solar-api"
    assert attrs["deployment.environment"] == "prod"
    assert attrs["service.instance.id"]


def test_registered_instrument_names_and_units(sdk_enabled):
    store = TelemetryStore()
    reader, _ = _collect(store)
    store.record_http_request(200, 0.03)
    by_name = _metrics_by_name(reader)

    assert set(by_name) == set(EXPECTED_INSTRUMENTS)
    for name, unit in EXPECTED_INSTRUMENTS.items():
        assert (by_name[name].unit or "") == unit, name
        assert not name.endswith("_total"), name
    assert "pv_job_duration_seconds" not in by_name


def test_metric_values_and_labels_match_prometheus_endpoint(sdk_enabled):
    store = TelemetryStore()
    reader, _ = _collect(store)
    store.record_http_request(200, 0.03)
    store.record_http_request(500, 2.0)
    by_name = _metrics_by_name(reader)

    reqs = {dict(p.attributes)["status"]: p.value for p in by_name["http_requests"].data.data_points}
    assert reqs["200"] == 1 and reqs["500"] == 1
    for p in by_name["http_requests"].data.data_points:
        assert p.attributes["service"] == "solar-api"

    (hist,) = by_name["http_request_duration"].data.data_points
    assert isinstance(hist, HistogramDataPoint)
    assert list(hist.explicit_bounds) == LATENCY_BUCKETS
    assert hist.count == 2
    assert dict(hist.attributes) == {"service": "solar-api"}

    nmae = {p.attributes["cohort"]: p.value for p in by_name["pv_model_nmae"].data.data_points}
    assert nmae == store.model_nmae
    drift = {p.attributes["feature"] for p in by_name["drift_score"].data.data_points}
    assert drift == set(store.drift_scores)
    assert all(p.attributes["site"] == "dkasc" for p in by_name["drift_score"].data.data_points)


async def test_forecast_served_emits_exactly_one_log_record(monkeypatch, sdk_enabled):
    def fail(*args, **kwargs):  # the old hand-built HTTP push must be gone
        raise AssertionError("push_log_to_otlp must not make its own HTTP call")

    monkeypatch.setattr("httpx.AsyncClient", fail)
    exporter = InMemoryLogRecordExporter()
    provider = LoggerProvider()
    provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))
    handler = LoggingHandler(level=logging.INFO, logger_provider=provider)
    telemetry.event_logger.addHandler(handler)
    payload = {
        "target_date": "2026-10-20",
        "kwp": 10.0,
        "total_daily_kwh": 34.14,
        "model_version": "v4",
        "is_ood": False,
    }
    try:
        await telemetry.push_log_to_otlp("forecast_served", payload)
    finally:
        telemetry.event_logger.removeHandler(handler)

    logs = exporter.get_finished_logs()
    assert len(logs) == 1
    record = logs[0].log_record
    assert json.loads(record.body) == {"event_type": "forecast_served", **payload}
    assert record.attributes["event_type"] == "forecast_served"
    assert record.severity_number.value == 9
    assert logs[0].instrumentation_scope.name == "pv-forecasting"
