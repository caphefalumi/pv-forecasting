import pytest
from services.supabase_service import log_prediction_to_supabase

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_supabase_logging_handles_offline_gracefully():
    payload = {
        "target_date": "2026-10-19",
        "predict_date": "2026-10-18",
        "kwp": 15.0,
        "total_daily_kwh": 65.4,
        "model_version": "v1.0-mock",
        "inputs": {"kwp": 15.0, "technology": "mono-Si"},
        "weather_forecast": {},
        "hourly_breakdown": [],
    }
    try:
        await log_prediction_to_supabase(payload)
    except Exception as exc:
        pytest.fail(f"log_prediction_to_supabase raised unhandled exception: {exc}")
