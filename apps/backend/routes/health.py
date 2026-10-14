from fastapi import APIRouter
from fastapi.responses import PlainTextResponse
from config import settings
from schemas import HealthResponse
from services.model_service import model_manager
from services.telemetry import telemetry_store

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health_check():
    """AWS ALB health check endpoint."""
    return HealthResponse(
        status="ok",
        model_loaded=model_manager.model is not None,
        model_source=model_manager.model_source,
    )


@router.get("/metrics", response_class=PlainTextResponse)
async def prometheus_metrics():
    """Prometheus scrape endpoint exposing all MLOps & service metrics."""
    return PlainTextResponse(
        content=telemetry_store.render_prometheus_text(),
        media_type="text/plain; version=0.0.4",
    )


@router.get("/api/v1/telemetry/endpoints")
async def telemetry_endpoints():
    """Verify configured Grafana Cloud raw endpoints (Prometheus, Loki, Tempo, OTLP)."""
    return {
        "prometheus": {
            "url": settings.GRAFANA_PROMETHEUS_URL,
            "user": settings.GRAFANA_PROMETHEUS_USER,
        },
        "loki": {
            "url": settings.GRAFANA_LOKI_URL,
            "user": settings.GRAFANA_LOKI_USER,
        },
        "tempo": {
            "url": settings.GRAFANA_TEMPO_URL,
            "user": settings.GRAFANA_TEMPO_USER,
        },
        "otlp_gateway": settings.GRAFANA_OTLP_ENDPOINT,
    }


@router.get("/")
async def root():
    return {
        "service": "pv-forecasting-api",
        "description": "PV Energy Prediction FastAPI Service (Sprint 2)",
        "model_version": model_manager.model_version,
        "docs": "/docs",
    }
