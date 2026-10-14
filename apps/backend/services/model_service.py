import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import numpy as np
import pandas as pd
import torch
import torch.nn as nn

try:
    from sklearn.impute import SimpleImputer
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler
except Exception:
    SimpleImputer = None
    Pipeline = None
    StandardScaler = None

from config import settings

logger = logging.getLogger(__name__)


class StandalonePreprocessor:
    """High-performance vectorized preprocessor compatible with PyO3 embedded runtimes."""
    def __init__(self, mean: np.ndarray, std: np.ndarray):
        self.mean = np.asarray(mean, dtype=np.float32)
        std_arr = np.asarray(std, dtype=np.float32)
        self.std = np.where(std_arr <= 1e-6, 1.0, std_arr).astype(np.float32)

    def transform(self, x: np.ndarray) -> np.ndarray:
        x_arr = np.nan_to_num(np.asarray(x, dtype=np.float32), nan=0.0)
        return ((x_arr - self.mean) / self.std).astype(np.float32)

# Base feature group — mirrors feature_spec.json and jobs/retrain_tools/data_loader.py
BASE_FEATURES: List[str] = [
    "ghi_fc", "dni_fc", "dhi_fc", "temp_fc", "cloud_fc", "wind_fc", "wx_source",
    "solar_zenith", "solar_azimuth", "ghi_clear", "kt_fc",
    "poa_fc", "temp_cell_fc", "p_phys_norm",
    "kwp", "technology_code", "tracking_code", "tilt_deg", "surface_azimuth_pvlib",
    "hour_sin", "hour_cos", "doy_sin", "doy_cos",
]

TECHNOLOGY_CODES = {
    "poly-Si": 0,
    "mono-Si": 1,
    "CdTe": 2,
    "CIGS": 3,
    "HIT": 4,
    "a-Si": 5,
    "CPV": 6,
}

TRACKING_CODES = {
    "fixed": 0,
    "single_axis": 1,
    "dual_axis": 2,
}


def _make_module(input_size: int = 23, hidden_size: int = 128, num_layers: int = 2, dropout: float = 0.2):
    """Reconstruct nn.Module matching jobs/retrain_tools/lstm_model.py."""
    class _Inner(nn.Module):
        def __init__(self):
            super().__init__()
            self.lstm = nn.LSTM(
                input_size=input_size,
                hidden_size=hidden_size,
                num_layers=num_layers,
                batch_first=True,
                dropout=dropout if num_layers > 1 else 0.0,
            )
            self.head = nn.Linear(hidden_size, 1)

        def forward(self, x):
            out, _ = self.lstm(x)
            last = out[:, -1, :]
            pred = self.head(last).squeeze(-1)
            if not self.training:
                return torch.clamp(pred, min=0.0)
            return pred

    return _Inner()


class ModelManager:
    """Manages dynamic model loading and zero-downtime champion updates."""

    def __init__(self):
        self.model: Optional[nn.Module] = None
        self.preprocessor: Optional[Pipeline] = None
        self.model_version: str = "initialising"
        self.model_source: str = "none"
        self.current_run_id: Optional[str] = None
        self.last_checked_time: float = 0.0
        self.check_ttl_seconds: float = 60.0  # Check for promoted Champion every 60s
        self.load_model()

    def load_model(self, force: bool = False) -> bool:
        """Attempt to load or reload champion from MLflow/Supabase, fallback to mock."""
        self.last_checked_time = time.time()

        # 1. Try MLflow + Supabase champion
        if settings.SUPABASE_URL and settings.SUPABASE_SERVICE_ROLE_KEY:
            try:
                import mlflow
                from mlflow.tracking import MlflowClient
                from supabase import create_client

                if settings.MLFLOW_TRACKING_URI:
                    mlflow.set_tracking_uri(settings.MLFLOW_TRACKING_URI)

                client = MlflowClient()
                mv = client.get_model_version_by_alias(settings.REGISTERED_MODEL_NAME, settings.MODEL_ALIAS)
                run_id = mv.run_id
                version = str(mv.version)

                # Skip download if champion hasn't changed
                if not force and self.current_run_id == run_id and self.model is not None:
                    return False

                logger.info("Found Champion model in MLflow Registry: version=%s run_id=%s. Downloading...", version, run_id)

                # Download checkpoint from Supabase
                parsed = urlparse(settings.SUPABASE_URL)
                base_url = f"{parsed.scheme}://{parsed.hostname}"
                sb_client = create_client(base_url, settings.SUPABASE_SERVICE_ROLE_KEY)
                bucket_path = f"artifacts/{run_id}/checkpoint.pt"
                raw_bytes = sb_client.storage.from_(settings.SUPABASE_RAW_DATA_BUCKET).download(bucket_path)

                with tempfile.NamedTemporaryFile(suffix=".pt", delete=False) as tmp:
                    tmp.write(raw_bytes)
                    tmp_path = tmp.name

                try:
                    checkpoint = torch.load(tmp_path, map_location="cpu", weights_only=False)
                finally:
                    Path(tmp_path).unlink(missing_ok=True)

                cfg = checkpoint["model_config"]
                state = checkpoint["model_state_dict"]
                new_model = _make_module(**cfg)
                new_model.load_state_dict(state)
                new_model.eval()

                # Atomically update references
                self.model = new_model
                self.preprocessor = checkpoint["preprocessor"]
                self.current_run_id = run_id
                self.model_version = f"v{version}"
                self.model_source = f"MLflow Champion (version {version}, run {run_id})"
                logger.info("Successfully activated Champion model: %s", self.model_source)
                return True
            except Exception as exc:
                if self.model is None:
                    logger.warning("Could not load live champion checkpoint (%s). Using fallback mock model.", exc)
                else:
                    logger.debug("Champion check: keeping current model (%s)", exc)

        # 2. Standalone fallback mock model if no model loaded yet
        if self.model is None or self.preprocessor is None:
            input_size = len(BASE_FEATURES)
            self.model = _make_module(input_size=input_size, hidden_size=128, num_layers=2, dropout=0.2)
            with torch.no_grad():
                if hasattr(self.model, "head") and self.model.head.bias is not None:
                    self.model.head.bias.fill_(0.65)
            self.model.eval()

            synthetic_mean = np.zeros(input_size, dtype=np.float32)
            synthetic_std = np.ones(input_size, dtype=np.float32)
            self.preprocessor = StandalonePreprocessor(mean=synthetic_mean, std=synthetic_std)
            self.model_version = "v1.0-mock"
            self.model_source = "Local Standalone LSTM (Architecture identical to Champion)"
            logger.info("Initialized fallback standalone model %s", self.model_source)
            return True

        if force:
            return True

        return False

    def predict(
        self,
        user_input: Dict[str, Any],
        weather_hourly: Dict[str, Any],
        target_date_str: str,
        predict_date_str: str,
    ) -> Dict[str, Any]:
        """Perform Day-Ahead 24-hour PV forecast with automatic champion refresh."""
        # Check for newly promoted Champion model every 60s without requiring code deployment
        if time.time() - self.last_checked_time > self.check_ttl_seconds:
            try:
                self.load_model(force=False)
            except Exception as exc:
                logger.debug("Periodic champion check failed: %s", exc)

        kwp = float(user_input.get("kwp", 10.0))
        tech_code = TECHNOLOGY_CODES.get(str(user_input.get("technology", "mono-Si")), 1)
        track_code = TRACKING_CODES.get(str(user_input.get("tracking", "fixed")), 0)
        tilt_deg = float(user_input.get("tilt_deg", 20.0))
        azimuth = float(user_input.get("surface_azimuth_pvlib", 0.0))

        target_date = pd.to_datetime(target_date_str)
        doy = target_date.dayofyear
        doy_angle = 2.0 * np.pi * (doy - 1.0) / 365.25

        rows = []
        for h in range(24):
            hour_angle = 2.0 * np.pi * (h + 0.5) / 24.0

            ghi_fc = float(weather_hourly.get("shortwave_radiation", [0.0] * 24)[h])
            dni_fc = float(weather_hourly.get("direct_normal_irradiance", [0.0] * 24)[h])
            dhi_fc = float(weather_hourly.get("diffuse_radiation", [0.0] * 24)[h])
            temp_fc = float(weather_hourly.get("temperature_2m", [25.0] * 24)[h])
            cloud_fc = float(weather_hourly.get("cloud_cover", [0.0] * 24)[h])
            wind_fc = float(weather_hourly.get("wind_speed_10m", [0.0] * 24)[h])

            is_day = (6 <= h <= 18)
            if is_day:
                midday_factor = max(0.0, np.sin(np.pi * (h - 6) / 12.0))
                zenith = 90.0 - (72.0 * midday_factor)
                sun_azimuth = 90.0 + (h - 6) * 15.0
                ghi_clear = 980.0 * midday_factor + 1.0
                kt_fc = min(1.5, ghi_fc / max(ghi_clear, 20.0))
                poa_fc = ghi_fc * 1.05
                temp_cell_fc = temp_fc + (poa_fc / 800.0) * 25.0
                gamma = -0.004
                sys_eff = 0.85
                p_phys_norm = max(
                    0.0,
                    (poa_fc / 1000.0) * (1.0 + gamma * (temp_cell_fc - 25.0)) * sys_eff,
                )
            else:
                zenith = 110.0
                sun_azimuth = 0.0
                ghi_clear = 0.0
                kt_fc = 0.0
                poa_fc = 0.0
                temp_cell_fc = temp_fc
                p_phys_norm = 0.0

            rows.append({
                "ghi_fc": np.float32(ghi_fc),
                "dni_fc": np.float32(dni_fc),
                "dhi_fc": np.float32(dhi_fc),
                "temp_fc": np.float32(temp_fc),
                "cloud_fc": np.float32(cloud_fc),
                "wind_fc": np.float32(wind_fc),
                "wx_source": np.float32(1.0),
                "solar_zenith": np.float32(zenith),
                "solar_azimuth": np.float32(sun_azimuth),
                "ghi_clear": np.float32(ghi_clear),
                "kt_fc": np.float32(kt_fc),
                "poa_fc": np.float32(poa_fc),
                "temp_cell_fc": np.float32(temp_cell_fc),
                "p_phys_norm": np.float32(p_phys_norm),
                "kwp": np.float32(kwp),
                "technology_code": np.float32(tech_code),
                "tracking_code": np.float32(track_code),
                "tilt_deg": np.float32(tilt_deg),
                "surface_azimuth_pvlib": np.float32(azimuth),
                "hour_sin": np.float32(np.sin(hour_angle)),
                "hour_cos": np.float32(np.cos(hour_angle)),
                "doy_sin": np.float32(np.sin(doy_angle)),
                "doy_cos": np.float32(np.cos(doy_angle)),
            })

        df_24 = pd.DataFrame(rows)[BASE_FEATURES]
        feat_scaled = self.preprocessor.transform(df_24.to_numpy().astype(np.float32)).astype(np.float32)

        # Batch 13 daytime hours (06:00 to 18:00) with window_size 6
        window_size = 6
        daytime_hours = list(range(6, 19))
        windows = [
            feat_scaled[h - (window_size - 1): h + 1]
            for h in daytime_hours
        ]
        x_batch = torch.tensor(np.array(windows), dtype=torch.float32)

        with torch.no_grad():
            preds = self.model(x_batch).squeeze().cpu().numpy()

        hourly_breakdown = []
        total_daily_kwh = 0.0
        peak_kwh = 0.0
        peak_hour = "12:00"

        for h in range(24):
            time_label = f"{h:02d}:00"
            ghi = float(df_24.iloc[h]["ghi_fc"])

            if 6 <= h <= 18:
                idx = h - 6
                raw_pred = float(preds[idx])
                solar_intensity = ghi / 950.0 if ghi > 0 else 0.0
                target_norm = round(max(0.0, min(1.0, raw_pred * solar_intensity)), 4)
                kwh = round(target_norm * kwp, 3)
            else:
                target_norm = 0.0
                kwh = 0.0

            if kwh > peak_kwh:
                peak_kwh = kwh
                peak_hour = time_label

            total_daily_kwh += kwh
            hourly_breakdown.append({
                "hour": time_label,
                "ghi_fc": round(ghi, 1),
                "target_norm": target_norm,
                "kwh": round(kwh, 3),
            })

        return {
            "predict_date": predict_date_str,
            "target_date": target_date_str,
            "kwp": kwp,
            "total_daily_kwh": round(total_daily_kwh, 2),
            "peak_hour": peak_hour,
            "peak_kwh": round(peak_kwh, 3),
            "daytime_hours_count": len(daytime_hours),
            "model_version": self.model_version,
            "hourly_breakdown": hourly_breakdown,
        }


model_manager = ModelManager()
