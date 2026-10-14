import logging
import time
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from config import settings
from routes.health import router as health_router
from routes.forecast import router as forecast_router
from services.model_service import model_manager
from services.telemetry import telemetry_store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("pv-backend")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Preload model on startup to ensure sub-5ms request latency
    logger.info("Initializing PV Forecasting Model...")
    model_manager.load_model()
    yield
    logger.info("Shutting down PV Forecasting API.")


app = FastAPI(
    title="PV Forecasting API",
    description="Day-Ahead Solar Energy Prediction Service (Sprint 2 ECS Fargate)",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS configuration for Vercel / local frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def prometheus_telemetry_middleware(request: Request, call_next):
    start_time = time.perf_counter()
    response = await call_next(request)
    duration = time.perf_counter() - start_time
    if request.url.path != "/metrics":
        telemetry_store.record_http_request(response.status_code, duration)
    return response

# Mount routes
app.include_router(health_router)
app.include_router(forecast_router)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=settings.HOST, port=settings.PORT, reload=True)
