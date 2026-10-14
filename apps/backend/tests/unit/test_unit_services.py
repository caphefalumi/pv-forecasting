"""
Unit Test Suite: Domain Services & Model Logic
Location: apps/backend/tests/unit/test_unit_services.py
Focus: In-memory model manager inference, solar geometry calculations, capacity scaling.
"""

import pytest
from services.model_service import ModelManager

pytestmark = pytest.mark.unit


def get_mock_weather_dict():
    """Generates standard Open-Meteo format dictionary with 24 hours of data."""
    return {
        "time": [f"2026-10-15T{h:02d}:00" for h in range(24)],
        "shortwave_radiation": [850.0 if 6 <= h <= 18 else 0.0 for h in range(24)],
        "direct_normal_irradiance": [700.0 if 6 <= h <= 18 else 0.0 for h in range(24)],
        "diffuse_radiation": [150.0 if 6 <= h <= 18 else 0.0 for h in range(24)],
        "temperature_2m": [25.0 for _ in range(24)],
        "relative_humidity_2m": [30.0 for _ in range(24)],
        "wind_speed_10m": [2.5 for _ in range(24)],
        "cloud_cover": [10.0 for _ in range(24)],
    }


def test_model_manager_initialization():
    mgr = ModelManager()
    assert mgr.model is not None
    assert mgr.model_version != ""
    assert "LSTM" in mgr.model_source or "Champion" in mgr.model_source


def test_model_manager_prediction_structure_and_bounds():
    mgr = ModelManager()
    weather_sample = get_mock_weather_dict()
    user_input = {
        "kwp": 10.0,
        "technology": "mono-Si",
        "tracking": "fixed",
        "tilt_deg": 20.0,
        "surface_azimuth_pvlib": 0.0,
    }

    result = mgr.predict(
        user_input=user_input,
        weather_hourly=weather_sample,
        target_date_str="2026-10-15",
        predict_date_str="2026-10-14",
    )

    # Core response assertions
    assert result["predict_date"] == "2026-10-14"
    assert result["target_date"] == "2026-10-15"
    assert result["kwp"] == 10.0
    assert result["total_daily_kwh"] > 0
    assert len(result["hourly_breakdown"]) == 24
    assert result["daytime_hours_count"] == 13

    # Physical daylight checks: night hours must be strictly 0.0 kWh
    night_indices = [0, 1, 2, 3, 4, 5, 19, 20, 21, 22, 23]
    for idx in night_indices:
        assert result["hourly_breakdown"][idx]["kwh"] == 0.0

    # Solar midday output must be positive
    midday_kwh = result["hourly_breakdown"][12]["kwh"]
    assert midday_kwh > 0
    assert result["peak_kwh"] >= midday_kwh


def test_model_manager_capacity_linear_scaling():
    mgr = ModelManager()
    weather_sample = get_mock_weather_dict()

    base_input = {
        "kwp": 10.0,
        "technology": "mono-Si",
        "tracking": "fixed",
        "tilt_deg": 20.0,
        "surface_azimuth_pvlib": 0.0,
    }
    double_input = {**base_input, "kwp": 20.0}

    res_10 = mgr.predict(base_input, weather_sample, "2026-10-15", "2026-10-14")
    res_20 = mgr.predict(double_input, weather_sample, "2026-10-15", "2026-10-14")

    # Double kWp capacity should produce approximately double daily energy (linear scaling)
    ratio = res_20["total_daily_kwh"] / res_10["total_daily_kwh"]
    assert 1.95 <= ratio <= 2.05


def test_model_manager_missing_optional_weather_columns():
    """Validates that predict handles missing optional weather keys gracefully via defaults."""
    mgr = ModelManager()
    minimal_weather = {
        "time": [f"2026-10-15T{h:02d}:00" for h in range(24)],
        "shortwave_radiation": [600.0 if 6 <= h <= 18 else 0.0 for h in range(24)],
    }
    user_input = {"kwp": 8.0}

    # Should not raise KeyError
    result = mgr.predict(user_input, minimal_weather, "2026-10-15", "2026-10-14")
    assert result["total_daily_kwh"] > 0
    assert len(result["hourly_breakdown"]) == 24
