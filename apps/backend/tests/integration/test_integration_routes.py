"""
Integration Test Suite: FastAPI Routes & HTTP Lifecycle
Location: apps/backend/tests/integration/test_integration_routes.py
Focus: End-to-end request parsing, validation responses, and route contracts.
"""

import pytest
from starlette.testclient import TestClient
from main import app

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_health_endpoint(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["model_loaded"] is True
    assert "model_source" in data


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
    assert data["total_daily_kwh"] > 0
    assert len(data["hourly_breakdown"]) == 24
    assert data["daytime_hours_count"] == 13
    assert data["peak_kwh"] > 0


def test_forecast_endpoint_validation_errors_produce_422(client):
    # Invalid negative kwp
    resp = client.post("/api/v1/forecast", json={"kwp": -10.0})
    assert resp.status_code == 422

    # Invalid technology enum
    resp = client.post("/api/v1/forecast", json={"technology": "invalid-cell-tech"})
    assert resp.status_code == 422

    # Invalid tilt (> 90 deg)
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
