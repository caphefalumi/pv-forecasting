import os
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List

# Look for .env in current dir, apps/backend, or repo root
BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(BASE_DIR / ".env", REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Server config
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    CORS_ORIGINS: List[str] = ["http://localhost:3000", "http://127.0.0.1:3000", "*"]

    # Supabase config
    SUPABASE_URL: str = ""
    SUPABASE_SERVICE_ROLE_KEY: str = ""
    SUPABASE_RAW_DATA_BUCKET: str = "processed_data"

    # MLflow config
    MLFLOW_TRACKING_URI: str = ""
    REGISTERED_MODEL_NAME: str = "pv-lstm-fleet"
    MODEL_ALIAS: str = "champion"

    # Weather API (Open-Meteo)
    OPEN_METEO_FORECAST_URL: str = "https://api.open-meteo.com/v1/forecast"
    SITE_LATITUDE: float = -23.7624   # Alice Springs DKASC
    SITE_LONGITUDE: float = 133.8754
    SITE_TZ: str = "Australia/Darwin"

    # Grafana Cloud Raw Telemetry Endpoints (slimgodwit2228 — prod-ap-southeast-1)
    GRAFANA_PROMETHEUS_URL: str = "https://prometheus-prod-37-prod-ap-southeast-1.grafana.net/api/prom"
    GRAFANA_PROMETHEUS_USER: str = "3616746"
    GRAFANA_LOKI_URL: str = "https://logs-prod-020.grafana.net/loki/api/v1/push"
    GRAFANA_LOKI_USER: str = "1804041"
    GRAFANA_TEMPO_URL: str = "https://tempo-prod-14-prod-ap-southeast-1.grafana.net/tempo"
    GRAFANA_TEMPO_USER: str = "1798340"
    GRAFANA_OTLP_ENDPOINT: str = "https://otlp-gateway-prod-ap-southeast-1.grafana.net/otlp"
    GRAFANA_CLOUD_API_KEY: str = ""


settings = Settings()
