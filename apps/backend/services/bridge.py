"""
bridge.py — Python-to-Rust PyO3 execution bridge.
Provides high-performance synchronous entrypoints callable by Axum via PyO3.
"""

import asyncio
import logging
from typing import Dict, Any
import pandas as pd

from services.model_service import model_manager
from services.weather_service import fetch_open_meteo_day_ahead
from services.supabase_service import log_prediction_to_supabase

logger = logging.getLogger("axum_pyo3_bridge")


def run_forecast_bridge(user_input: Dict[str, Any]) -> Dict[str, Any]:
    """Called by Axum Rust server via PyO3."""
    # 1. Resolve Day-Ahead dates
    predict_date_raw = user_input.get("predict_date")
    if predict_date_raw:
        predict_dt = pd.to_datetime(predict_date_raw)
    else:
        predict_dt = pd.Timestamp.now().normalize()

    target_dt = predict_dt + pd.Timedelta(days=1)
    predict_date_str = str(predict_dt.date())
    target_date_str = str(target_dt.date())

    # 2. Fetch weather forecast from Open-Meteo
    weather_hourly = asyncio.run(fetch_open_meteo_day_ahead(
        target_date_str=target_date_str,
        latitude=user_input.get("latitude"),
        longitude=user_input.get("longitude"),
    ))

    # 3. Model inference
    result = model_manager.predict(
        user_input=user_input,
        weather_hourly=weather_hourly,
        target_date_str=target_date_str,
        predict_date_str=predict_date_str,
    )

    # 4. Trigger Supabase logging asynchronously
    result["inputs"] = user_input
    result["weather_forecast"] = weather_hourly
    try:
        asyncio.run(log_prediction_to_supabase(result))
    except Exception as exc:
        logger.debug("Bridge Supabase log error: %s", exc)

    return result


def get_model_info_bridge() -> Dict[str, Any]:
    """Retrieve model info for Axum /api/v1/model/info endpoint."""
    return {
        "model_version": model_manager.model_version,
        "model_source": model_manager.model_source,
        "is_ready": model_manager.model is not None,
    }


def reload_model_bridge() -> Dict[str, Any]:
    """Reload champion model for Axum /api/v1/model/reload endpoint."""
    reloaded = model_manager.load_model(force=True)
    return {
        "reloaded": reloaded,
        "model_version": model_manager.model_version,
        "model_source": model_manager.model_source,
    }
