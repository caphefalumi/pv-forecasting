import sys
from pathlib import Path
import pytest
from starlette.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parents[1] / "apps" / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_health_check(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["model_loaded"] is True
    assert "model_source" in data


def test_forecast_day_ahead_scrum16(client):
    payload = {
        "kwp": 10.0,
        "technology": "mono-Si",
        "tracking": "fixed",
        "tilt_deg": 20.0,
        "surface_azimuth_pvlib": 0.0,
        "predict_date": "2026-10-12",
    }
    resp = client.post("/api/v1/forecast", json=payload)
    assert resp.status_code == 200
    data = resp.json()

    assert data["target_date"] == "2026-10-13"
    assert data["predict_date"] == "2026-10-12"
    assert data["total_daily_kwh"] > 0
    assert len(data["hourly_breakdown"]) == 24
    assert data["daytime_hours_count"] == 13

    night_hours = [data["hourly_breakdown"][h]["kwh"] for h in [0, 1, 2, 3, 4, 5, 20, 21, 22, 23]]
    assert all(val == 0.0 for val in night_hours)

    midday_kwh = data["hourly_breakdown"][12]["kwh"]
    assert midday_kwh > 0


def test_model_reload_endpoint(client):
    resp = client.post("/api/v1/model/reload")
    assert resp.status_code == 200
    data = resp.json()
    assert "model_version" in data
    assert "model_source" in data
