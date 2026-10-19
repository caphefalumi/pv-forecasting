import logging
from typing import Any
import httpx
import numpy as np

from config import settings

logger = logging.getLogger(__name__)


async def fetch_open_meteo_day_ahead(
    target_date_str: str,
    latitude: float | None = None,
    longitude: float | None = None,
) -> dict[str, Any]:
    """Fetch hourly weather forecast from Open-Meteo for target_date, with synthetic clear-sky fallback."""
    lat = latitude if latitude is not None else settings.SITE_LATITUDE
    lon = longitude if longitude is not None else settings.SITE_LONGITUDE

    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": [
            "shortwave_radiation",
            "direct_normal_irradiance",
            "diffuse_radiation",
            "temperature_2m",
            "cloud_cover",
            "wind_speed_10m",
        ],
        "timezone": settings.SITE_TZ,
        "start_date": target_date_str,
        "end_date": target_date_str,
    }

    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            resp = await client.get(settings.OPEN_METEO_FORECAST_URL, params=params)
            if resp.status_code == 200:
                data = resp.json().get("hourly", {})
                if "time" in data and len(data["time"]) == 24:
                    logger.info("Successfully fetched live Open-Meteo forecast for %s", target_date_str)
                    return data
            logger.warning("Open-Meteo returned status %s. Falling back to synthetic physics.", resp.status_code)
    except Exception as exc:
        logger.warning("Failed to reach Open-Meteo (%s). Falling back to synthetic physics.", exc)

    return generate_synthetic_weather_day(target_date_str)


def generate_synthetic_weather_day(target_date_str: str) -> dict[str, Any]:
    """Synthetic clear-sky weather for offline resilience."""
    times = [f"{target_date_str}T{h:02d}:00" for h in range(24)]
    ghi, dni, dhi, temp, cloud, wind = [], [], [], [], [], []

    for h in range(24):
        if 6 <= h <= 18:
            factor = max(0.0, np.sin(np.pi * (h - 6) / 12.0))
            ghi.append(float(round(950.0 * factor, 1)))
            dni.append(float(round(850.0 * factor, 1)))
            dhi.append(float(round(100.0 * factor + 15.0, 1)))
            temp.append(float(round(20.0 + 12.0 * factor, 1)))
            cloud.append(5.0)
            wind.append(3.5)
        else:
            ghi.append(0.0)
            dni.append(0.0)
            dhi.append(0.0)
            temp.append(18.0)
            cloud.append(10.0)
            wind.append(2.0)

    return {
        "time": times,
        "shortwave_radiation": ghi,
        "direct_normal_irradiance": dni,
        "diffuse_radiation": dhi,
        "temperature_2m": temp,
        "cloud_cover": cloud,
        "wind_speed_10m": wind,
    }
