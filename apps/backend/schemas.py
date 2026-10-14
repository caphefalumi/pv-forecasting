from enum import Enum
from typing import List, Optional
from pydantic import BaseModel, ConfigDict, Field


class TechnologyType(str, Enum):
    POLY_SI = "poly-Si"
    MONO_SI = "mono-Si"
    CDTE = "CdTe"
    CIGS = "CIGS"
    HIT = "HIT"
    A_SI = "a-Si"
    CPV = "CPV"


class TrackingType(str, Enum):
    FIXED = "fixed"
    SINGLE_AXIS = "single_axis"
    DUAL_AXIS = "dual_axis"


class ForecastRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "kwp": 10.0,
                "technology": "mono-Si",
                "tracking": "fixed",
                "tilt_deg": 20.0,
                "surface_azimuth_pvlib": 0.0,
                "predict_date": "2026-10-18",
            }
        }
    )

    kwp: float = Field(default=10.0, gt=0, description="Installed DC capacity in kWp")
    technology: TechnologyType = Field(default=TechnologyType.MONO_SI, description="PV cell technology")
    tracking: TrackingType = Field(default=TrackingType.FIXED, description="Tracking system type")
    tilt_deg: float = Field(default=20.0, ge=0, le=90, description="Tilt angle in degrees")
    surface_azimuth_pvlib: float = Field(default=0.0, ge=-180, le=360, description="Surface azimuth angle (0 = North in S. Hemisphere)")
    predict_date: Optional[str] = Field(default=None, description="Request date YYYY-MM-DD. Target forecast will be predict_date + 1 day")
    latitude: Optional[float] = Field(default=None, description="Optional override latitude (defaults to site lat)")
    longitude: Optional[float] = Field(default=None, description="Optional override longitude (defaults to site lon)")


class HourlyPoint(BaseModel):
    hour: str
    ghi_fc: float
    target_norm: float
    kwh: float


class ForecastResponse(BaseModel):
    predict_date: str
    target_date: str
    kwp: float
    total_daily_kwh: float
    peak_hour: str
    peak_kwh: float
    daytime_hours_count: int
    model_version: str
    is_ood: bool = Field(default=False, description="True if array parameters fall outside the DKASC training distribution")
    hourly_breakdown: List[HourlyPoint]


class HealthResponse(BaseModel):
    status: str
    model_loaded: bool
    model_source: str
