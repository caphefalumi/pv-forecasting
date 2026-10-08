"""Solar geometry, clear-sky, and plane-of-array irradiance features.

Features computed:
- Solar position at interval midpoint (ts_utc_end - 30 min).
- Clear-sky GHI via Ineichen.
- kt_fc = ghi_fc / ghi_clear (clipped [0, 1.5], NaN when ghi_clear < 20).
- Per-array POA irradiance using pvlib with haydavies model.
- Cell temperature via pvlib sapm_cell (open_rack_glass_polymer).
- Tracker geometry: singleaxis (N-S axis, max_angle=60, backtrack=False) or dual_axis.
"""

from __future__ import annotations

import logging
import warnings

import numpy as np
import pandas as pd
import pvlib
from pvlib import irradiance as pv_irr, tracking as pv_track, temperature as pv_temp
from pvlib.location import Location

from .config import PipelineConfig

logger = logging.getLogger(__name__)

# SAPM module params: open_rack_glass_polymer
SAPM_PARAMS = pvlib.temperature.TEMPERATURE_MODEL_PARAMETERS["sapm"]["open_rack_glass_polymer"]


def _get_location(cfg: PipelineConfig) -> Location:
    return Location(
        latitude=cfg.latitude,
        longitude=cfg.longitude,
        tz="UTC",
        altitude=cfg.altitude_m,
    )


def _to_utc_index(ts) -> pd.DatetimeIndex:
    """Return a tz-aware UTC DatetimeIndex (pvlib needs an Index, not a Series)."""
    idx = pd.DatetimeIndex(ts)
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    return idx.tz_convert("UTC")


def compute_solar_position(
    ts_utc_end: pd.Series,
    cfg: PipelineConfig,
) -> pd.DataFrame:
    """Compute solar position at interval midpoint (ts_utc_end - 30 min).

    Returns DataFrame with: ts_utc_end, solar_zenith, solar_azimuth, ghi_clear, is_daylight.
    """
    loc = _get_location(cfg)
    ts_idx = _to_utc_index(ts_utc_end)
    midpoints = ts_idx - pd.Timedelta(minutes=30)

    solar_pos = loc.get_solarposition(midpoints)
    clearsky = loc.get_clearsky(midpoints, model="ineichen", solar_position=solar_pos)

    return pd.DataFrame({
        "ts_utc_end": ts_idx,
        "solar_zenith": solar_pos["apparent_zenith"].to_numpy(dtype="float32"),
        "solar_azimuth": solar_pos["azimuth"].to_numpy(dtype="float32"),
        "ghi_clear": clearsky["ghi"].to_numpy(dtype="float32"),
        "is_daylight": (solar_pos["apparent_zenith"] < 90).to_numpy(),
    })


def compute_kt_fc(
    hourly: pd.DataFrame,
) -> pd.DataFrame:
    """Add kt_fc = ghi_fc / ghi_clear column to hourly DataFrame.

    NaN when ghi_clear < 20. Clipped to [0, 1.5].
    """
    df = hourly.copy()
    gc = df["ghi_clear"]
    gf = df.get("ghi_fc", pd.Series(np.nan, index=df.index))

    kt = gf / gc
    kt = kt.where(gc >= 20)
    kt = kt.clip(lower=0, upper=1.5)
    df["kt_fc"] = kt.astype("float32")
    return df


def compute_array_features(
    hourly: pd.DataFrame,
    arrays_meta: pd.DataFrame,
    cfg: PipelineConfig,
) -> pd.DataFrame:
    """Per-array plane-of-array irradiance and cell temperature (vectorised).

    hourly: one row per ts_utc_end (or more; duplicates are collapsed) with
        solar_zenith, solar_azimuth, ghi_fc, dni_fc, dhi_fc, temp_fc, wind_fc.
    arrays_meta: source_id, metadata_complete, tracking, tilt_deg, surface_azimuth_pvlib.

    Returns long DataFrame: source_id, ts_utc_end, poa_fc, temp_cell_fc
    (only arrays with complete metadata; merge onto the label table on both keys).
    """
    need = ["ts_utc_end", "solar_zenith", "solar_azimuth",
            "ghi_fc", "dni_fc", "dhi_fc", "temp_fc", "wind_fc"]
    wx = hourly[[c for c in need if c in hourly.columns]].drop_duplicates("ts_utc_end")
    wx = wx.sort_values("ts_utc_end").reset_index(drop=True)
    for c in need:
        if c not in wx.columns:
            wx[c] = np.nan
    times = _to_utc_index(wx["ts_utc_end"]) - pd.Timedelta(minutes=30)
    dni_extra = pvlib.irradiance.get_extra_radiation(times).to_numpy()

    zen = wx["solar_zenith"].to_numpy(dtype=float)
    azi = wx["solar_azimuth"].to_numpy(dtype=float)
    ghi = wx["ghi_fc"].to_numpy(dtype=float)
    dni = wx["dni_fc"].to_numpy(dtype=float)
    dhi = wx["dhi_fc"].to_numpy(dtype=float)
    tair = wx["temp_fc"].to_numpy(dtype=float)
    wind = wx["wind_fc"].to_numpy(dtype=float)

    meta = arrays_meta.copy()
    meta["_trk"] = meta["tracking"].astype("string").str.strip().str.lower()
    complete = meta[meta["metadata_complete"].fillna(False).astype(bool)]

    if (complete["_trk"] == "single_axis").any():
        logger.warning(
            "ASSUMPTION: single_axis trackers use N-S axis (axis_tilt=0, axis_azimuth=0), "
            "max_angle=60, backtrack=False. Arrays: %s",
            complete.loc[complete["_trk"] == "single_axis", "source_id"].tolist(),
        )

    out = []
    for _, arr in complete.iterrows():
        trk = arr["_trk"]
        if trk == "fixed":
            if pd.isna(arr["tilt_deg"]) or pd.isna(arr["surface_azimuth_pvlib"]):
                continue
            s_tilt = np.full(len(wx), float(arr["tilt_deg"]))
            s_azi = np.full(len(wx), float(arr["surface_azimuth_pvlib"]))
        elif trk == "single_axis":
            trk_out = pvlib.tracking.singleaxis(
                apparent_zenith=pd.Series(zen, index=times),
                apparent_azimuth=pd.Series(azi, index=times),
                axis_tilt=0, axis_azimuth=0, max_angle=60, backtrack=False,
            )
            # sun below horizon -> tracker stowed flat
            s_tilt = trk_out["surface_tilt"].fillna(0).to_numpy()
            s_azi = trk_out["surface_azimuth"].fillna(0).to_numpy()
        elif trk == "dual_axis":
            s_tilt = np.clip(zen, 0, 90)
            s_azi = azi
        else:
            continue

        irr = pvlib.irradiance.get_total_irradiance(
            surface_tilt=s_tilt, surface_azimuth=s_azi,
            solar_zenith=zen, solar_azimuth=azi,
            dni=dni, ghi=ghi, dhi=dhi, dni_extra=dni_extra, model="haydavies",
        )
        poa = np.asarray(irr["poa_global"], dtype=float)
        poa = np.where(np.isfinite(poa), np.clip(poa, 0, None), np.nan)
        tcell = pvlib.temperature.sapm_cell(
            poa_global=poa, temp_air=tair, wind_speed=wind, **SAPM_PARAMS
        )
        out.append(pd.DataFrame({
            "source_id": arr["source_id"],
            "ts_utc_end": wx["ts_utc_end"].to_numpy(),
            "poa_fc": poa.astype("float32"),
            "temp_cell_fc": np.asarray(tcell, dtype="float32"),
        }))

    if not out:
        return pd.DataFrame(columns=["source_id", "ts_utc_end", "poa_fc", "temp_cell_fc"])
    res = pd.concat(out, ignore_index=True)
    res["ts_utc_end"] = _to_utc_index(res["ts_utc_end"])
    return res


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


def compute_cyclical_time_features(ts_local_start: pd.Series) -> pd.DataFrame:
    """Compute cyclical time features: hour_sin, hour_cos, doy_sin, doy_cos.

    h = ts_local_start + 30 min as fractional hours, angle = 2π * h / 24
    doy = day of year of ts_local_start, angle = 2π * (doy - 1) / 365.25
    """
    ts = pd.to_datetime(ts_local_start)
    mid = ts + pd.Timedelta(minutes=30)
    frac_hour = mid.dt.hour + mid.dt.minute / 60.0 + mid.dt.second / 3600.0
    hour_angle = 2.0 * np.pi * frac_hour / 24.0

    doy = ts.dt.dayofyear
    doy_angle = 2.0 * np.pi * (doy - 1.0) / 365.25

    return pd.DataFrame({
        "hour_sin": np.sin(hour_angle).astype("float32"),
        "hour_cos": np.cos(hour_angle).astype("float32"),
        "doy_sin": np.sin(doy_angle).astype("float32"),
        "doy_cos": np.cos(doy_angle).astype("float32"),
    }, index=ts_local_start.index)


def encode_categories(df: pd.DataFrame) -> pd.DataFrame:
    """Encode technology_code (int8) and tracking_code (int8)."""
    tech = df["technology"].astype(str).str.strip() if "technology" in df.columns else pd.Series("", index=df.index)
    track = df["tracking"].astype(str).str.strip().str.lower() if "tracking" in df.columns else pd.Series("", index=df.index)

    tech_code = tech.map(TECHNOLOGY_CODES).fillna(-1).astype(np.int8)
    track_code = track.map(TRACKING_CODES).fillna(-1).astype(np.int8)

    return pd.DataFrame({
        "technology_code": tech_code,
        "tracking_code": track_code,
    }, index=df.index)

