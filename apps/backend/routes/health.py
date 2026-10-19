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
    """Verify configured Grafana Cloud OTLP exporter settings."""
    return {
        "otlp": {
            "protocol": settings.OTEL_EXPORTER_OTLP_PROTOCOL,
            "endpoint": settings.OTEL_EXPORTER_OTLP_ENDPOINT,
            "instance_id": settings.GRAFANA_OTLP_INSTANCE_ID,
            "headers_configured": bool(
                settings.OTEL_EXPORTER_OTLP_HEADERS or settings.GRAFANA_CLOUD_API_TOKEN
            ),
        }
    }


@router.get("/")
async def root():
    return {
        "service": "pv-forecasting-api",
        "description": "PV Energy Prediction FastAPI Service",
        "model_version": model_manager.model_version,
        "docs": "/docs",
    }
