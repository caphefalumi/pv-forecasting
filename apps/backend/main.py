import logging
import time
import uuid
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

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

OPENAPI_TAGS = [
    {
        "name": "health",
        "description": "Service readiness, Prometheus metrics, and telemetry endpoint inspection.",
    },
    {
        "name": "forecast",
        "description": "Day-ahead (D+1) 24-hour PV energy forecasting and zero-downtime model management.",
    },
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Preload model on startup to ensure sub-5ms request latency
    logger.info("Initializing PV Forecasting Model...")
    model_manager.load_model()
    yield
    logger.info("Shutting down PV Forecasting API.")


app = FastAPI(
    title="PV Forecasting API",
    description="Day-Ahead Solar Energy Prediction Service",
    version="1.0.0",
    openapi_tags=OPENAPI_TAGS,
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
async def observability_and_correlation_middleware(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    request.state.request_id = request_id
    start_time = time.perf_counter()

    response = await call_next(request)

    duration = time.perf_counter() - start_time
    if request.url.path != "/metrics":
        telemetry_store.record_http_request(response.status_code, duration)

    response.headers["X-Request-ID"] = request_id
    response.headers["X-Process-Time-Ms"] = f"{duration * 1000.0:.2f}"
    return response


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    request_id = getattr(request.state, "request_id", str(uuid.uuid4()))
    logger.error("Unhandled exception [request_id=%s]: %s", request_id, exc, exc_info=True)
    telemetry_store.record_http_request(500, 0.0)
    return JSONResponse(
        status_code=500,
        content={
            "detail": "Internal server error",
            "request_id": request_id,
        },
        headers={"X-Request-ID": request_id},
    )

# Mount routes
app.include_router(health_router)
app.include_router(forecast_router)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=settings.HOST, port=settings.PORT, reload=True)
