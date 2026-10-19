import pytest
from pydantic import ValidationError
from schemas import (
    ForecastRequest,
    ForecastResponse,
    HealthResponse,
    TechnologyType,
    TrackingType,
    HourlyPoint,
)

pytestmark = pytest.mark.unit


def test_forecast_request_valid_defaults():
    req = ForecastRequest()
    assert req.kwp == 10.0
    assert req.technology == TechnologyType.MONO_SI
    assert req.tracking == TrackingType.FIXED
    assert req.tilt_deg == 20.0
    assert req.surface_azimuth_pvlib == 0.0
    assert req.predict_date is None


def test_forecast_request_all_valid_technologies():
    valid_techs = ["poly-Si", "mono-Si", "CdTe", "CIGS", "HIT", "a-Si", "CPV"]
    for tech in valid_techs:
        req = ForecastRequest(technology=tech)
        assert req.technology == tech


def test_forecast_request_all_valid_tracking():
    valid_tracking = ["fixed", "single_axis", "dual_axis"]
    for track in valid_tracking:
        req = ForecastRequest(tracking=track)
        assert req.tracking == track


def test_forecast_request_invalid_kwp():
    with pytest.raises(ValidationError):
        ForecastRequest(kwp=0.0)

    with pytest.raises(ValidationError):
        ForecastRequest(kwp=-5.0)


def test_forecast_request_invalid_tilt_boundaries():
    with pytest.raises(ValidationError):
        ForecastRequest(tilt_deg=-0.1)

    with pytest.raises(ValidationError):
        ForecastRequest(tilt_deg=90.1)

    assert ForecastRequest(tilt_deg=0.0).tilt_deg == 0.0
    assert ForecastRequest(tilt_deg=90.0).tilt_deg == 90.0


def test_forecast_request_invalid_azimuth_boundaries():
    with pytest.raises(ValidationError):
        ForecastRequest(surface_azimuth_pvlib=-180.1)

    with pytest.raises(ValidationError):
        ForecastRequest(surface_azimuth_pvlib=360.1)

    assert ForecastRequest(surface_azimuth_pvlib=-180.0).surface_azimuth_pvlib == -180.0
    assert ForecastRequest(surface_azimuth_pvlib=360.0).surface_azimuth_pvlib == 360.0


def test_forecast_request_invalid_enums():
    with pytest.raises(ValidationError):
        ForecastRequest(technology="perovskite-unsupported")

    with pytest.raises(ValidationError):
        ForecastRequest(tracking="hex-axis")


def test_hourly_point_and_forecast_response():
    points = [
        HourlyPoint(hour=f"{h:02d}:00", ghi_fc=150.0, target_norm=0.15, kwh=1.5)
        for h in range(24)
    ]
    resp = ForecastResponse(
        predict_date="2026-10-14",
        target_date="2026-10-15",
        kwp=10.0,
        total_daily_kwh=36.0,
        peak_hour="12:00",
        peak_kwh=1.5,
        daytime_hours_count=13,
        model_version="v1.0-mock",
        hourly_breakdown=points,
    )
    assert resp.total_daily_kwh == 36.0
    assert len(resp.hourly_breakdown) == 24
    assert resp.peak_hour == "12:00"


def test_health_response_contract():
    health = HealthResponse(
        status="ok",
        model_loaded=True,
        model_source="Local Standalone LSTM",
    )
    assert health.status == "ok"
    assert health.model_loaded is True
    assert "Standalone" in health.model_source
