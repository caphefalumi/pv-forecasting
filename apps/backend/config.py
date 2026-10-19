from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(BASE_DIR / ".env", REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    HOST: str = "0.0.0.0"
    PORT: int = 8000
    CORS_ORIGINS: list[str] = ["http://localhost:3000", "http://127.0.0.1:3000"]

    SUPABASE_URL: str = ""
    SUPABASE_SERVICE_ROLE_KEY: str = ""
    SUPABASE_RAW_DATA_BUCKET: str = "processed_data"

    MLFLOW_TRACKING_URI: str = ""
    SQL_ALCHEMY_DATABASE_URL: str = ""
    REGISTERED_MODEL_NAME: str = "pv-lstm-fleet"
    MODEL_ALIAS: str = "champion"

    OPEN_METEO_FORECAST_URL: str = "https://api.open-meteo.com/v1/forecast"
    SITE_LATITUDE: float = -23.7624
    SITE_LONGITUDE: float = 133.8754
    SITE_TZ: str = "Australia/Darwin"

    # Grafana Cloud OTLP (https://otlp-gateway-prod-ap-southeast-1.grafana.net/otlp)
    OTEL_EXPORTER_OTLP_PROTOCOL: str = "http/protobuf"
    OTEL_EXPORTER_OTLP_ENDPOINT: str = (
        "https://otlp-gateway-prod-ap-southeast-1.grafana.net/otlp"
    )
    # e.g. Authorization=Basic <base64(instanceId:apiToken)>
    OTEL_EXPORTER_OTLP_HEADERS: str = ""
    GRAFANA_OTLP_INSTANCE_ID: str = "1846261"
    GRAFANA_CLOUD_API_TOKEN: str = ""


settings = Settings()
