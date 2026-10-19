import pytest
from services.weather_service import fetch_open_meteo_day_ahead

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_live_open_meteo_weather_fetching():
    weather = await fetch_open_meteo_day_ahead("2026-10-18")
    assert "time" in weather
    assert len(weather["time"]) == 24

    assert "shortwave_radiation" in weather
    assert len(weather["shortwave_radiation"]) == 24

    assert "temperature_2m" in weather
    assert len(weather["temperature_2m"]) == 24

    assert all(val >= 0.0 for val in weather["shortwave_radiation"])
