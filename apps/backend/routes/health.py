from fastapi import APIRouter
from schemas import HealthResponse
from services.model_service import model_manager

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health_check():
    """AWS ALB health check endpoint."""
    return HealthResponse(
        status="ok",
        model_loaded=model_manager.model is not None,
        model_source=model_manager.model_source,
    )


@router.get("/")
async def root():
    return {
        "service": "pv-forecasting-api",
        "description": "PV Energy Prediction FastAPI Service (Sprint 2)",
        "model_version": model_manager.model_version,
        "docs": "/docs",
    }
