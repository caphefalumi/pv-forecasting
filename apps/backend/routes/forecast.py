from datetime import datetime, timedelta
import pandas as pd
from fastapi import APIRouter, BackgroundTasks, HTTPException

from schemas import ForecastRequest, ForecastResponse
from services.model_service import model_manager
from services.weather_service import fetch_open_meteo_day_ahead
from services.supabase_service import log_prediction_to_supabase
from services.telemetry import telemetry_store, push_log_to_loki

router = APIRouter(prefix="/api/v1", tags=["forecast"])


@router.post("/forecast", response_model=ForecastResponse)
async def create_forecast(request: ForecastRequest, background_tasks: BackgroundTasks):
    """Generate Day-Ahead 24-hour PV forecast (Energy Prediction kWh)."""
    # 1. Resolve Day-Ahead dates (predict_date -> target_date = predict_date + 1)
    if request.predict_date:
        try:
            predict_dt = pd.to_datetime(request.predict_date)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid predict_date format. Use YYYY-MM-DD.")
    else:
        predict_dt = pd.Timestamp.now().normalize()

    target_dt = predict_dt + pd.Timedelta(days=1)
    predict_date_str = str(predict_dt.date())
    target_date_str = str(target_dt.date())

    # 2. Fetch Day-Ahead weather forecast from Open-Meteo
    weather_hourly = await fetch_open_meteo_day_ahead(
        target_date_str=target_date_str,
        latitude=request.latitude,
        longitude=request.longitude,
    )

    # 3. Model inference: PyTorch LSTM + sklearn preprocessor
    result = model_manager.predict(
        user_input=request.model_dump(),
        weather_hourly=weather_hourly,
        target_date_str=target_date_str,
        predict_date_str=predict_date_str,
    )

    # 4. Track Out-of-Distribution (OOD) user traffic & log to Supabase + Loki
    user_dict = request.model_dump()
    is_ood = telemetry_store.check_and_record_ood(user_dict)
    result["is_ood"] = is_ood
    result["inputs"] = {**user_dict, "is_ood": is_ood}
    result["weather_forecast"] = weather_hourly
    background_tasks.add_task(log_prediction_to_supabase, result)
    background_tasks.add_task(
        push_log_to_loki,
        "forecast_served",
        {
            "target_date": target_date_str,
            "kwp": request.kwp,
            "total_daily_kwh": result["total_daily_kwh"],
            "model_version": result["model_version"],
            "is_ood": is_ood,
        },
    )

    return ForecastResponse(**result)


@router.get("/model/info")
async def get_model_info():
    """Retrieve active model metadata."""
    return {
        "model_version": model_manager.model_version,
        "model_source": model_manager.model_source,
        "is_ready": model_manager.model is not None,
    }


@router.post("/model/reload")
async def reload_model():
    """Force reload the Champion model from MLflow Registry without restarting the server."""
    reloaded = model_manager.load_model(force=True)
    return {
        "reloaded": reloaded,
        "model_version": model_manager.model_version,
        "model_source": model_manager.model_source,
    }

