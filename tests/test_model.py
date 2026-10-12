"""
tests/test_model.py — End-to-end integration test simulating API backend inference.

Scenario:
  1. An end-user on the web UI provides only their PV panel parameters:
     - kwp (installed peak capacity, kWp)
     - technology (mono-Si, poly-Si, etc.)
     - tracking (fixed, single_axis, dual_axis)
     - tilt_deg (tilt angle)
     - surface_azimuth_pvlib (surface azimuth angle)
     - predict_date (date of prediction request, e.g. today)

  2. The API backend automatically resolves Day-Ahead target date:
     - target_date = predict_date + 1 day (tomorrow)
     - Enriches all 24 hours of tomorrow with weather forecasts (GHI, DNI, DHI, Temp, Wind, Cloud)
     - Computes solar geometry, POA irradiance, cell temperature, physics baseline
     - Encodes cyclical time and categorical variables for all 23 BASE_FEATURES

  3. Model inference:
     - Extracts 13 daytime sliding windows (06:00 to 18:00) of length 6
     - Batches them into a single tensor of shape (13, 6, 23)
     - PyTorch LSTM predicts normalized output: target_norm in a single forward pass
     - Computes hourly energy: hourly_kwh = target_norm * kwp

  4. API output returned to the user:
     - Hourly generation curve for tomorrow
     - Total daily energy for tomorrow: total_daily_kwh = sum(hourly_kwh)
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

import numpy as np
import pandas as pd
import pytest
import torch
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

# Ensure repository root is on sys.path
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.retrain_tools.data_loader import BASE_FEATURES
from jobs.retrain_tools.lstm_model import _make_module

logger = logging.getLogger(__name__)


# ── 1. Backend Service Simulation ─────────────────────────────────────────────

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


def enrich_full_day_backend_data(
    user_input: Dict[str, Any],
) -> pd.DataFrame:
    """Enrich user panel specs into a full 24-hour sequence for tomorrow (Day-Ahead).

    Args:
        user_input: Dictionary with user panel specs and predict_date.

    Returns:
        pd.DataFrame with 24 rows (hours 0 to 23) and the 23 BASE_FEATURES columns.
    """
    # Target date is always the day after predict_date (Day-Ahead protocol)
    predict_date = None
    if "predict_date" in user_input:
        predict_date = pd.to_datetime(user_input["predict_date"])
        target_date = predict_date + pd.Timedelta(days=1)
    elif "target_date" in user_input:
        target_date = pd.to_datetime(user_input["target_date"])
        predict_date = target_date - pd.Timedelta(days=1)
    else:
        # Default: user predicts today -> forecast for tomorrow
        predict_date = pd.Timestamp.now().normalize()
        target_date = predict_date + pd.Timedelta(days=1)

    kwp = float(user_input.get("kwp", 10.0))
    tech_str = str(user_input.get("technology", "mono-Si"))
    track_str = str(user_input.get("tracking", "fixed"))
    tilt_deg = float(user_input.get("tilt_deg", 20.0))
    azimuth = float(user_input.get("surface_azimuth_pvlib", 0.0))

    tech_code = TECHNOLOGY_CODES.get(tech_str, 1)
    track_code = TRACKING_CODES.get(track_str, 0)

    rows = []
    # Build 24 hours of day-ahead data (00:00 to 23:00)
    for h in range(24):
        ts_start = target_date.replace(hour=h, minute=0, second=0)

        # Daytime: hours 6 to 18 (sunrise ~06:00, sunset ~18:00)
        is_day = (6 <= h <= 18)
        if is_day:
            midday_factor = max(0.0, np.sin(np.pi * (h - 6) / 12.0))
            ghi_fc = 950.0 * midday_factor
            dni_fc = 850.0 * midday_factor
            dhi_fc = 100.0 * midday_factor + 15.0
            temp_fc = 20.0 + 12.0 * midday_factor
            cloud_fc = 5.0
            wind_fc = 3.5
            zenith = 90.0 - (72.0 * midday_factor)
            sun_azimuth = 90.0 + (h - 6) * 15.0
            ghi_clear = 980.0 * midday_factor + 1.0
            kt_fc = min(1.5, ghi_fc / max(ghi_clear, 20.0))
            poa_fc = ghi_fc * 1.05
            temp_cell_fc = temp_fc + (poa_fc / 800.0) * 25.0

            # Physics baseline model
            gamma = -0.004
            sys_eff = 0.85
            p_phys_norm = max(
                0.0,
                (poa_fc / 1000.0) * (1.0 + gamma * (temp_cell_fc - 25.0)) * sys_eff,
            )
        else:
            # Nighttime hours
            ghi_fc = 0.0
            dni_fc = 0.0
            dhi_fc = 0.0
            temp_fc = 18.0
            cloud_fc = 10.0
            wind_fc = 2.0
            zenith = 110.0
            sun_azimuth = 0.0
            ghi_clear = 0.0
            kt_fc = 0.0
            poa_fc = 0.0
            temp_cell_fc = 18.0
            p_phys_norm = 0.0

        wx_source = 1  # 1 = ECMWF

        # Cyclical time encoding
        hour_angle = 2.0 * np.pi * (h + 0.5) / 24.0
        doy = ts_start.dayofyear
        doy_angle = 2.0 * np.pi * (doy - 1.0) / 365.25

        row = {
            "ghi_fc": np.float32(ghi_fc),
            "dni_fc": np.float32(dni_fc),
            "dhi_fc": np.float32(dhi_fc),
            "temp_fc": np.float32(temp_fc),
            "cloud_fc": np.float32(cloud_fc),
            "wind_fc": np.float32(wind_fc),
            "wx_source": np.float32(wx_source),
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
        }
        rows.append(row)

    df_enriched = pd.DataFrame(rows)
    df_out = df_enriched[BASE_FEATURES].copy()
    df_out.attrs["predict_date"] = str(predict_date.date()) if predict_date is not None else "N/A"
    df_out.attrs["target_date"] = str(target_date.date())
    return df_out


# ── 2. Model Loader (Champion or Standalone Mock Checkpoint) ──────────────────

def load_or_create_checkpoint(use_live: bool = False) -> Tuple[Any, Any, str]:
    """Load champion checkpoint if requested and available; otherwise create mock checkpoint.

    Args:
        use_live: If True, attempts to connect to MLflow & Supabase.

    Returns:
        (model, preprocessor, model_source)
    """
    import os
    if use_live or os.environ.get("USE_LIVE_CHAMPION") == "1":
        try:
            from jobs.evaluate_tools.model_loader import (
                build_model_from_checkpoint,
                load_champion_checkpoint,
            )
            checkpoint, version, run_id, _ = load_champion_checkpoint()
            model = build_model_from_checkpoint(checkpoint)
            preprocessor = checkpoint["preprocessor"]
            return model, preprocessor, f"MLflow Champion (v{version}, run {run_id})"
        except Exception as exc:
            logger.info("Using standalone mock checkpoint: %s", exc)

    # Standalone mock model & fitted preprocessor for testing without cloud credentials
    input_size = len(BASE_FEATURES)  # 23
    model = _make_module(
        input_size=input_size,
        hidden_size=128,
        num_layers=2,
        dropout=0.2,
    )
    # Initialize head bias to 0.65 for realistic daytime PV output simulation (~65% capacity)
    with torch.no_grad():
        if hasattr(model, "head") and hasattr(model.head, "bias") and model.head.bias is not None:
            model.head.bias.fill_(0.65)
    model.eval()

    # Create fitted preprocessor
    synthetic_x = np.random.randn(50, input_size).astype(np.float32)
    preprocessor = Pipeline([
        ("imputer", SimpleImputer(strategy="median")),
        ("scaler", StandardScaler()),
    ])
    preprocessor.fit(synthetic_x)

    return model, preprocessor, "Standalone Mock LSTM (Architecture identical to Champion)"


# Initialise model and preprocessor once
model, preprocessor, model_source_name = load_or_create_checkpoint()


# ── 3. Full Day-Ahead Total Energy Prediction Function ────────────────────────

def predict_pv_daily_kwh(user_input: Dict[str, Any]) -> Dict[str, Any]:
    """API Endpoint Handler: Forecasts full 24-hour generation and total kWh for tomorrow.

    Args:
        user_input: Dict containing user panel specs (kwp, technology, tracking, etc.).

    Returns:
        Dict with total_daily_kwh, hourly_breakdown, and peak hour details.
    """
    # 1. API backend generates 24 hours of enriched features for tomorrow
    df_24 = enrich_full_day_backend_data(user_input)
    predict_date = df_24.attrs.get("predict_date")
    target_date = df_24.attrs.get("target_date")
    kwp = float(user_input.get("kwp", 10.0))

    # Scale all 24 hours with the fitted preprocessor
    feat_values = df_24.to_numpy().astype(np.float32)
    feat_scaled = preprocessor.transform(feat_values).astype(np.float32)

    # 2. Batch Inference: Process only the 13 daytime hours (06:00 to 18:00)
    window_size = 6
    daytime_hours = list(range(6, 19))  # 13 daytime hours (06:00 - 18:00)
    windows = []
    for h in daytime_hours:
        # 6 consecutive hours ending at hour h: [h-5, ..., h]
        win = feat_scaled[h - (window_size - 1) : h + 1]
        windows.append(win)

    # Stack 13 windows into a single batch tensor: shape (13, 6, 23)
    x_batch = torch.tensor(np.array(windows), dtype=torch.float32)

    # Execute model forward pass in a single batch call for all daytime hours
    with torch.no_grad():
        preds = model(x_batch).squeeze().cpu().numpy()

    # 3. Aggregate full 24-hour results
    hourly_breakdown: List[Dict[str, Any]] = []
    total_daily_kwh = 0.0
    peak_kwh = 0.0
    peak_hour = "12:00"

    for h in range(24):
        time_label = f"{h:02d}:00"
        ghi = float(df_24.iloc[h]["ghi_fc"])

        if 6 <= h <= 18:
            idx = h - 6
            raw_pred = float(preds[idx])
            solar_intensity = ghi / 950.0
            target_norm = round(max(0.0, min(1.0, raw_pred * solar_intensity)), 4)
            kwh = round(target_norm * kwp, 3)
        else:
            # Nighttime: automatically zero without running model
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
        "predict_date": predict_date,
        "target_date": target_date,
        "kwp": kwp,
        "total_daily_kwh": round(total_daily_kwh, 2),
        "peak_hour": peak_hour,
        "peak_kwh": round(peak_kwh, 3),
        "daytime_hours_count": len(daytime_hours),
        "hourly_breakdown": hourly_breakdown,
    }


# ── 4. Unit / Integration Tests (pytest) ──────────────────────────────────────

def test_enrichment_columns():
    """Verify that backend enrichment produces exactly 24 rows and 23 BASE_FEATURES."""
    mock_user_input = {
        "kwp": 8.0,
        "technology": "mono-Si",
        "tracking": "fixed",
        "tilt_deg": 25.0,
        "surface_azimuth_pvlib": 0.0,
        "predict_date": "2026-10-12",
    }
    df = enrich_full_day_backend_data(mock_user_input)
    assert df.shape == (24, 23), f"Expected (24, 23), got {df.shape}"
    assert list(df.columns) == BASE_FEATURES
    assert df.attrs["predict_date"] == "2026-10-12"
    assert df.attrs["target_date"] == "2026-10-13", "Target date must be tomorrow"


def test_predict_daily_kwh_pipeline():
    """Verify end-to-end full day prediction and total kWh sum."""
    mock_user_input = {
        "kwp": 15.0,
        "technology": "mono-Si",
        "tracking": "fixed",
        "tilt_deg": 22.5,
        "surface_azimuth_pvlib": 0.0,
        "predict_date": "2026-10-12",
    }
    result = predict_pv_daily_kwh(mock_user_input)

    assert result["predict_date"] == "2026-10-12"
    assert result["target_date"] == "2026-10-13"
    assert result["total_daily_kwh"] > 0.0, "Daily energy must be positive"
    assert len(result["hourly_breakdown"]) == 24
    assert result["peak_kwh"] > 0.0

    # Ensure sum of hourly matches total daily energy
    sum_hours = sum(item["kwh"] for item in result["hourly_breakdown"])
    assert abs(sum_hours - result["total_daily_kwh"]) < 0.1


# ── 5. Standalone Execution Entry Point ───────────────────────────────────────

if __name__ == "__main__":
    print("=" * 75)
    print("PV FORECASTING — DAY-AHEAD TOTAL DAILY ENERGY INFERENCE SIMULATION")
    print("=" * 75)
    print(f"Model Source: {model_source_name}")
    print(f"Feature Count: {len(BASE_FEATURES)} features")
    print("-" * 75)

    # Simulated user input from Web UI
    mock_user_input = {
        "kwp": 15.0,                        # Installed system capacity (15 kWp)
        "technology": "mono-Si",            # Monocrystalline silicon
        "tracking": "fixed",                # Fixed mounting structure
        "tilt_deg": 22.5,                   # Array tilt angle (22.5 deg)
        "surface_azimuth_pvlib": 0.0,       # Azimuth angle (0 deg, North-facing)
        "predict_date": "2026-10-12",       # Request date (Today)
    }

    print("\n[1] User Input (Web Client Parameters):")
    for k, v in mock_user_input.items():
        print(f"    - {k:22s}: {v}")

    print("\n[2] Backend Processing (Automated Day-Ahead Enrichment):")
    result = predict_pv_daily_kwh(mock_user_input)
    print(f"    - Prediction request date (predict_date) : {result['predict_date']} (Today)")
    print(f"    - Forecast target date (target_date)     : {result['target_date']} (Tomorrow / Day-Ahead)")
    print(f"    - LSTM sliding window scan              : 13 daytime hours (06:00 - 18:00, single batch call)")
    print(f"    - Nighttime handling                    : Automatically set to 0.0 kWh (model bypassed)")

    print("\n[3] Hourly Generation Breakdown for Tomorrow:")
    print("    +" + "-" * 10 + "+" + "-" * 18 + "+" + "-" * 16 + "+" + "-" * 18 + "+")
    print("    |   Hour   | Irradiance(W/m²) |   target_norm  | Generation (kWh) |")
    print("    +" + "-" * 10 + "+" + "-" * 18 + "+" + "-" * 16 + "+" + "-" * 18 + "+")

    # Display daytime hours from 06:00 to 18:00
    for item in result["hourly_breakdown"]:
        h_str = item["hour"]
        h_int = int(h_str.split(":")[0])
        if 6 <= h_int <= 18:
            print(f"    |  {h_str:6s}  |  {item['ghi_fc']:12.1f}    |     {item['target_norm']:6.4f}     |   {item['kwh']:8.3f} kWh  |")

    print("    +" + "-" * 10 + "+" + "-" * 18 + "+" + "-" * 16 + "+" + "-" * 18 + "+")

    print("\n[4] DAY-AHEAD TOTAL ENERGY SUMMARY:")
    print(f"    - System peak capacity       : {result['kwp']} kWp")
    print(f"    - Peak generation hour       : {result['peak_hour']} ({result['peak_kwh']} kWh)")
    print("    " + "=" * 55)
    print(f"    => TOTAL DAY-AHEAD GENERATION : {result['total_daily_kwh']} kWh")
    print("    " + "=" * 55)
