"""Aggregate 5-min data to hourly UTC (hour-ending).

Rules:
- Bin by UTC hour-ending: ts_utc_start in [H-1h, H) → ts_utc_end = H.
- Per (source_id, ts_utc_end): power_kw_mean (valid records only), n_valid, coverage, qc_flags (union).
- target_kw = power_kw_mean if coverage >= min_coverage and array not excluded and hour inside validity window.
- target_norm = target_kw / kwp.
- Station: same binning → station_hourly.parquet.
- ts_local_start = (ts_utc_end - 1h) in local TZ.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from .config import PipelineConfig

logger = logging.getLogger(__name__)

# QC flags that invalidate a record
INVALID_FLAGS = {"missing", "neg", "over_kwp", "stuck", "day_zero"}


def _is_invalid(flag_str: str) -> bool:
    """True if the flag string contains any invalidating code."""
    if not flag_str:
        return False
    codes = set(f.strip() for f in flag_str.split(","))
    return bool(codes & INVALID_FLAGS)


def _union_flags(flags: pd.Series) -> str:
    """Union all flag strings in a group into a single sorted comma-separated string."""
    codes: set[str] = set()
    for f in flags:
        if f:
            codes.update(c.strip() for c in f.split(",") if c.strip())
    return ",".join(sorted(codes))


def aggregate_labels(
    labels_5min: pd.DataFrame,
    arrays_meta: pd.DataFrame,
    cfg: PipelineConfig,
) -> pd.DataFrame:
    """Aggregate 5-min power to hourly (UTC hour-ending).

    Args:
        labels_5min: Long 5-min DataFrame with (source_id, ts_utc_start, power_kw, source_file, qc_flag).
        arrays_meta: Arrays metadata with (source_id, kwp, excluded, valid_from_utc, valid_to_utc).
        cfg: Pipeline config.

    Returns:
        Hourly DataFrame with columns:
        source_id, ts_utc_end, ts_local_start, power_kw_mean, n_valid, coverage, qc_flags,
        target_kw, target_norm.
    """
    import pytz
    tz = pytz.timezone(cfg.tz)

    df = labels_5min[[c for c in ("source_id", "ts_utc_start", "power_kw", "qc_flag") if c in labels_5min.columns]].copy()

    # Assign to UTC hour-ending bin
    # record with ts_utc_start in [H-1h, H) → ts_utc_end = H
    # Use floor + 1h so that exact hour boundaries (e.g. 02:00:00) map to 03:00,
    # not to themselves (ceil is a no-op on exact boundaries).
    df["ts_utc_end"] = df["ts_utc_start"].dt.floor("h") + pd.Timedelta(hours=1)

    # Mark invalid records (vectorised via bitmask; qc_flag may be categorical)
    from .qc import QC_BITS, flag_series_to_bitmask
    if "qc_flag" not in df.columns:
        df["qc_flag"] = ""
    bits = flag_series_to_bitmask(df["qc_flag"])
    all_codes = sorted(INVALID_FLAGS)
    code_cols = []
    for code in all_codes:
        col = f"_f_{code}"
        df[col] = (bits & QC_BITS[code]) > 0
        code_cols.append(col)
    del bits
    df["_valid"] = ~(df[code_cols].any(axis=1) | df["power_kw"].isna())
    df["_valid_kw"] = df["power_kw"].where(df["_valid"])
    df = df.drop(columns=["qc_flag", "source_file"], errors="ignore")

    agg_spec = {"power_kw_mean": ("_valid_kw", "mean"), "n_valid": ("_valid", "sum")}
    agg_spec.update({c: (c, "any") for c in code_cols})
    agg = df.groupby(["source_id", "ts_utc_end"], sort=True).agg(**agg_spec).reset_index()

    # Union of flags per hour, sorted, comma-joined
    flags = pd.Series("", index=agg.index)
    for code, col in zip(all_codes, code_cols):
        has = agg[col]
        flags = flags.where(~has, np.where(flags == "", code, flags + "," + code))
    agg["qc_flags"] = flags
    agg = agg.drop(columns=code_cols)

    agg["n_valid"] = agg["n_valid"].astype("int16")
    agg["coverage"] = agg["n_valid"] / 12.0

    meta = arrays_meta.set_index("source_id")
    agg["_kwp"] = agg["source_id"].map(meta["kwp"])
    agg["_excluded"] = agg["source_id"].map(meta["excluded"]).fillna(False).astype(bool)
    vf = pd.to_datetime(agg["source_id"].map(meta["valid_from_utc"]), utc=True)
    vt = pd.to_datetime(agg["source_id"].map(meta["valid_to_utc"]), utc=True)
    in_window = (vf.isna() | (agg["ts_utc_end"] >= vf)) & (vt.isna() | (agg["ts_utc_end"] <= vt))

    agg["target_kw"] = agg["power_kw_mean"].where(
        (agg["coverage"] >= cfg.min_coverage) & ~agg["_excluded"] & in_window
    )
    agg["target_norm"] = (agg["target_kw"] / agg["_kwp"]).where(agg["_kwp"].notna())
    agg = agg.drop(columns=["_kwp", "_excluded"])

    # ts_local_start = (ts_utc_end - 1h) converted to local time
    utc_start = agg["ts_utc_end"] - pd.Timedelta(hours=1)
    agg["ts_local_start"] = utc_start.dt.tz_convert(tz)


    logger.info(
        "Aggregated to %d hourly records for %d arrays",
        len(agg),
        agg["source_id"].nunique(),
    )

    return agg


def aggregate_station(
    station_5min: pd.DataFrame,
    cfg: PipelineConfig,
) -> pd.DataFrame:
    """Aggregate 5-min station data to hourly (UTC hour-ending).

    Args:
        station_5min: Wide 5-min station DataFrame (ts_utc_start, ghi_obs, ...).
        cfg: Pipeline config.

    Returns:
        Hourly station DataFrame with ts_utc_end, ts_local_start, mean of each obs column, coverage.
    """
    import pytz
    tz = pytz.timezone(cfg.tz)

    if station_5min.empty or "ts_utc_start" not in station_5min.columns:
        return pd.DataFrame()

    df = station_5min.copy()
    df["ts_utc_end"] = df["ts_utc_start"].dt.floor("h") + pd.Timedelta(hours=1)

    obs_cols = [c for c in df.columns if c not in ("ts_utc_start", "ts_utc_end")]

    agg_dict = {c: "mean" for c in obs_cols}
    agg_dict["_count"] = ("ts_utc_start", "count")

    grouped = df.groupby("ts_utc_end", sort=True)
    agg = grouped.agg(**{c: pd.NamedAgg(column=c, aggfunc="mean") for c in obs_cols})
    agg["_count"] = grouped["ts_utc_start"].count()
    agg = agg.reset_index()

    agg["coverage"] = agg["_count"] / 12.0
    agg = agg.drop(columns=["_count"])

    utc_start = agg["ts_utc_end"] - pd.Timedelta(hours=1)
    agg["ts_local_start"] = utc_start.dt.tz_convert(tz)

    logger.info("Aggregated station to %d hourly records", len(agg))
    return agg
