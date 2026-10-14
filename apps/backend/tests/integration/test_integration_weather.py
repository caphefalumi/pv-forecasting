"""
Integration Test Suite: Open-Meteo Weather API Integration
Location: apps/backend/tests/integration/test_integration_weather.py
Focus: Network interaction with live Open-Meteo endpoint and fallback behavior.
"""

import pytest
from services.weather_service import fetch_open_meteo_day_ahead

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_live_open_meteo_weather_fetching():
    """Verify live API returns valid 24-hour weather forecast arrays."""
    weather = await fetch_open_meteo_day_ahead("2026-10-18")
    assert "time" in weather
    assert len(weather["time"]) == 24

    assert "shortwave_radiation" in weather
    assert len(weather["shortwave_radiation"]) == 24

    assert "temperature_2m" in weather
    assert len(weather["temperature_2m"]) == 24

    # Radiation values must be non-negative
    assert all(val >= 0.0 for val in weather["shortwave_radiation"])
