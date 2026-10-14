import logging
from typing import Dict, Any
from urllib.parse import urlparse
from config import settings

logger = logging.getLogger(__name__)


async def log_prediction_to_supabase(prediction_data: Dict[str, Any]) -> None:
    """Log prediction summary to Supabase for Sprint 1 & Sprint 2 eval jobs."""
    if not settings.SUPABASE_URL or not settings.SUPABASE_SERVICE_ROLE_KEY:
        return

    try:
        from supabase import create_client
        parsed = urlparse(settings.SUPABASE_URL)
        base_url = f"{parsed.scheme}://{parsed.hostname}"
        client = create_client(base_url, settings.SUPABASE_SERVICE_ROLE_KEY)

        user_input = prediction_data.get("inputs", {})
        row = {
            "predict_date": prediction_data.get("predict_date"),
            "target_date": prediction_data.get("target_date"),
            "kwp": float(prediction_data.get("kwp", user_input.get("kwp", 10.0))),
            "technology": str(user_input.get("technology", "mono-Si")),
            "tracking": str(user_input.get("tracking", "fixed")),
            "tilt_deg": float(user_input.get("tilt_deg", 20.0)),
            "surface_azimuth_pvlib": float(user_input.get("surface_azimuth_pvlib", 0.0)),
            "model_version": str(prediction_data.get("model_version", "unknown")),
            "total_daily_kwh": float(prediction_data.get("total_daily_kwh", 0.0)),
            "peak_hour": str(prediction_data.get("peak_hour", "")),
            "peak_kwh": float(prediction_data.get("peak_kwh", 0.0)),
            "inputs": user_input,
            "weather_forecast": prediction_data.get("weather_forecast", {}),
            "hourly_predictions": prediction_data.get("hourly_breakdown", []),
        }

        client.table("prediction_logs").insert(row).execute()
        logger.info("Successfully logged prediction to Supabase 'prediction_logs' (target_date=%s, total=%s kWh)",
                    row["target_date"], row["total_daily_kwh"])
    except Exception as exc:
        logger.warning("Failed to log prediction to Supabase: %s", exc)
