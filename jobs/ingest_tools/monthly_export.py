"""
monthly_export.py — Split train_table by data month and save as CSV.

Month is determined by ts_local_start (Alice Springs local time, UTC+09:30).
One CSV per calendar month: data/exports/YYYY-MM.csv

Column order in the export:
  1. Identifiers   (source_id, timestamps)
  2. Target        (target_kw → what the model predicts; target_norm for reference)
  3. Array metadata
  4. Weather features
  5. Solar geometry + derived
  6. Time encoding
  7. Optional PR features (included if present)
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

# Ordered export columns — columns not listed here are appended at the end
_ORDERED_COLS = [
    # Identifiers
    "source_id", "ts_utc_end", "ts_local_start",
    # Target (target_kw = hourly mean kW ≈ kWh; target_norm = target_kw / kWp)
    "target_kw", "target_norm",
    # Array metadata
    "kwp", "technology_code", "tracking_code", "tilt_deg", "surface_azimuth_pvlib",
    # Weather forecast features (day-ahead, _previous_day1)
    "ghi_fc", "dni_fc", "dhi_fc", "temp_fc", "cloud_fc", "wind_fc", "wx_source",
    # Solar geometry
    "solar_zenith", "solar_azimuth", "ghi_clear", "kt_fc",
    # Derived weather
    "poa_fc", "temp_cell_fc", "p_phys_norm",
    # Cyclic time encoding
    "hour_sin", "hour_cos", "doy_sin", "doy_cos",
    # Optional: rolling performance ratio (off by default in recipes)
    "pr_7d", "pr_30d",
]

# ── Row filtering ──────────────────────────────────────────────────────────────

def filter_usable_rows(df: pd.DataFrame) -> pd.DataFrame:
    """
    Retain only daytime rows with a valid target and weather forecast.

    Mirrors the row filter in dataset.load_split():
      target_kw not NaN  AND  ghi_clear > 0  AND  ghi_fc not NaN

    Args:
        df: Full train_table DataFrame.

    Returns:
        Filtered DataFrame (copy), sorted by (source_id, ts_utc_end).
    """
    mask = (
        df["target_kw"].notna()
        & (df["ghi_clear"] > 0)
        & df["ghi_fc"].notna()
    )
    n_total = len(df)
    df_out = df[mask].copy()
    n_kept = len(df_out)
    logger.info(
        "Row filter: %d / %d usable (%.1f%% daytime + valid)",
        n_kept, n_total, 100.0 * n_kept / n_total if n_total else 0,
    )
    return df_out


# ── Column ordering ────────────────────────────────────────────────────────────

def prepare_export_df(df: pd.DataFrame) -> pd.DataFrame:
    """
    Reorder columns: priority list first, then any remaining columns alphabetically.

    Args:
        df: Filtered train_table DataFrame.

    Returns:
        DataFrame with columns in export order.
    """
    priority = [c for c in _ORDERED_COLS if c in df.columns]
    extra = sorted(c for c in df.columns if c not in set(priority))
    return df[priority + extra]


# ── Monthly split ──────────────────────────────────────────────────────────────

def split_by_month(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """
    Split a DataFrame by the calendar month of ts_local_start.

    Month key = "YYYY-MM" string (Alice Springs local time, UTC+09:30).

    Args:
        df: Filtered + column-ordered DataFrame.

    Returns:
        Ordered dict {month_key: df_month} sorted chronologically.
    """
    ts = pd.to_datetime(df["ts_local_start"])
    # Period("M") gives YYYY-MM without timezone ambiguity
    month_keys = ts.dt.to_period("M").astype(str)

    monthly: dict[str, pd.DataFrame] = {}
    for ym in sorted(month_keys.unique()):
        chunk = (
            df[month_keys == ym]
            .sort_values(["source_id", "ts_utc_end"])
            .reset_index(drop=True)
        )
        monthly[ym] = chunk

    logger.info(
        "Months: %d total  (%s → %s)",
        len(monthly), min(monthly), max(monthly),
    )
    return monthly


# ── Save to disk ───────────────────────────────────────────────────────────────

def save_month_csv(df_month: pd.DataFrame, out_dir: Path, year_month: str) -> Path:
    """
    Save a single month's DataFrame to <out_dir>/<year_month>.csv.

    Args:
        df_month:   Monthly DataFrame slice.
        out_dir:    Local directory (created if missing), e.g. data/exports/.
        year_month: Month key string, e.g. "2026-09".

    Returns:
        Absolute path to the saved CSV file.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{year_month}.csv"

    df_month.to_csv(out_path, index=False, encoding="utf-8")
    size_mb = out_path.stat().st_size / 1e6
    logger.info(
        "Saved %s  (%d rows, %d arrays, %.2f MB)",
        out_path.name,
        len(df_month),
        df_month["source_id"].nunique() if "source_id" in df_month.columns else 0,
        size_mb,
    )
    return out_path
