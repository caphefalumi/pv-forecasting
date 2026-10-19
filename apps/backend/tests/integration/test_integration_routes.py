import copy

import pytest
from starlette.testclient import TestClient

from main import app
from services.telemetry import telemetry_store

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_health_endpoint(client):
    resp = client.get("/health", headers={"X-Request-ID": "req-health-123"})
    assert resp.status_code == 200
    assert resp.headers.get("X-Request-ID") == "req-health-123"
    assert "X-Process-Time-Ms" in resp.headers
    assert "etag" in resp.headers
    data = resp.json()
    assert data["status"] == "ok"
    assert data["model_loaded"] is True
    assert "model_source" in data


def test_etag_conditional_get_returns_304(client):
    first = client.get("/api/v1/model/info")
    assert first.status_code == 200
    etag = first.headers.get("etag")
    assert etag is not None and etag.startswith('"') and etag.endswith('"')

    cached = client.get("/api/v1/model/info", headers={"If-None-Match": etag})
    assert cached.status_code == 304
    assert cached.content == b""
    assert cached.headers.get("etag") == etag

    mismatch = client.get("/api/v1/model/info", headers={"If-None-Match": '"stale-etag"'})
    assert mismatch.status_code == 200
    assert mismatch.headers.get("etag") == etag


def test_model_info_endpoint(client):
    resp = client.get("/api/v1/model/info")
    assert resp.status_code == 200
    data = resp.json()
    assert "model_version" in data
    assert "model_source" in data
    assert data["is_ready"] is True


def test_model_reload_endpoint(client):
    resp = client.post("/api/v1/model/reload")
    assert resp.status_code == 200
    data = resp.json()
    assert data["reloaded"] is True
    assert "model_version" in data


def test_forecast_endpoint_full_day_ahead_flow(client):
    payload = {
        "kwp": 15.0,
        "technology": "mono-Si",
        "tracking": "fixed",
        "tilt_deg": 25.0,
        "surface_azimuth_pvlib": 0.0,
        "predict_date": "2026-10-18",
    }
    resp = client.post("/api/v1/forecast", json=payload)
    assert resp.status_code == 200
    data = resp.json()

    assert data["predict_date"] == "2026-10-18"
    assert data["target_date"] == "2026-10-19"
    assert data["kwp"] == 15.0
    assert data["is_ood"] is False
    assert data["total_daily_kwh"] > 0
    assert len(data["hourly_breakdown"]) == 24
    assert data["daytime_hours_count"] == 13
    assert data["peak_kwh"] > 0


def test_forecast_endpoint_flags_ood_inputs(client):
    payload = {
        "kwp": 250.0,
        "technology": "mono-Si",
        "tracking": "fixed",
        "tilt_deg": 60.0,
        "surface_azimuth_pvlib": 0.0,
        "predict_date": "2026-10-18",
    }
    resp = client.post("/api/v1/forecast", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["is_ood"] is True


def test_forecast_endpoint_validation_errors_produce_422(client):
    resp = client.post("/api/v1/forecast", json={"kwp": -10.0})
    assert resp.status_code == 422

    resp = client.post("/api/v1/forecast", json={"technology": "invalid-cell-tech"})
    assert resp.status_code == 422

    resp = client.post("/api/v1/forecast", json={"tilt_deg": 120.0})
    assert resp.status_code == 422


def test_forecast_endpoint_invalid_date_produces_400(client):
    payload = {
        "kwp": 10.0,
        "predict_date": "not-a-valid-date-format",
    }
    resp = client.post("/api/v1/forecast", json=payload)
    assert resp.status_code == 400
    assert "Invalid predict_date format" in resp.json()["detail"]


MLOPS_SERIES = (
    "pv_model_nmae",
    "pv_error_attribution_kwh",
    "drift_score",
    "pv_ingestion_schema_violations_total",
    "pv_ingestion_range_flags_total",
    "pv_job_duration_seconds",
)


def test_prometheus_metrics_endpoint_matches_grafana_dashboards(client):
    resp = client.get("/metrics")
    assert resp.status_code == 200
    text = resp.text
    assert 'http_requests_total{service="solar-api"' in text
    assert 'http_request_duration_seconds_bucket{service="solar-api"' in text
    assert "pv_user_ood_rate" in text
    # Model / data-quality series are not published until a job sets them.
    for name in MLOPS_SERIES:
        assert name not in text, name


def test_prometheus_metrics_endpoint_exposes_values_once_set(client):
    snapshot = {
        attr: copy.copy(getattr(telemetry_store, attr))
        for attr in (
            "model_nmae",
            "error_attribution",
            "drift_scores",
            "schema_violations_total",
            "range_flags_total",
            "job_durations",
        )
    }
    try:
        telemetry_store.set_model_nmae("all", 0.064)
        telemetry_store.set_model_nmae("held_out_6", 0.076)
        telemetry_store.set_error_attribution("e_model", 0.42)
        telemetry_store.set_drift_score("ghi_fc", 0.08)
        telemetry_store.add_schema_violations(0)
        telemetry_store.add_range_flags("neg", 4)
        telemetry_store.set_job_duration("solar-ingest", 42.5)

        text = client.get("/metrics").text
        assert 'pv_model_nmae{cohort="all"} 0.064' in text
        assert 'pv_model_nmae{cohort="held_out_6"} 0.076' in text
        assert 'pv_error_attribution_kwh{source="e_model"} 0.42' in text
        assert 'drift_score{site="dkasc",feature="ghi_fc"} 0.08' in text
        assert "pv_ingestion_schema_violations_total 0" in text
        assert 'pv_ingestion_range_flags_total{flag_type="neg"} 4' in text
        assert 'pv_job_duration_seconds{job="solar-ingest"} 42.5' in text
    finally:
        for attr, value in snapshot.items():
            setattr(telemetry_store, attr, value)


def test_telemetry_raw_endpoints_config(client):
    resp = client.get("/api/v1/telemetry/endpoints")
    assert resp.status_code == 200
    data = resp.json()
    assert data["otlp"]["protocol"] == "http/protobuf"
    assert data["otlp"]["endpoint"].endswith("/otlp")
    assert "otlp-gateway-prod-ap-southeast-1.grafana.net" in data["otlp"]["endpoint"]
    assert data["otlp"]["instance_id"] == "1846261"
    assert "headers_configured" in data["otlp"]
