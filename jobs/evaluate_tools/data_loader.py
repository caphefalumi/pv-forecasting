"""
data_loader.py — Load the latest evaluation window from Supabase Storage.

Strategy:
  1. List all YYYY-MM.csv files under the ``processed/`` prefix.
  2. Download the two most-recent monthly CSV files.
  3. Concatenate, apply the standard row filter:
       target_norm.notna() & ghi_clear > 0 & ghi_fc.notna()
  4. Sort by ts_utc_end (ascending) and return the last N rows.

This gives us the most-recent real production records — unseen during training —
to probe whether champion model performance has drifted.
"""

from __future__ import annotations

import logging
from typing import List

import pandas as pd

logger = logging.getLogger(__name__)

# Number of recent months to download for the evaluation window
N_RECENT_MONTHS = 2

# Number of most-recent filtered rows to evaluate against
N_EVAL_ROWS = 500

# Feature columns — must match the spec used during training
BASE_FEATURES: List[str] = [
    "ghi_fc", "dni_fc", "dhi_fc", "temp_fc", "cloud_fc", "wind_fc", "wx_source",
    "solar_zenith", "solar_azimuth", "ghi_clear", "kt_fc",
    "poa_fc", "temp_cell_fc", "p_phys_norm",
    "kwp", "technology_code", "tracking_code", "tilt_deg", "surface_azimuth_pvlib",
    "hour_sin", "hour_cos", "doy_sin", "doy_cos",
]

TARGET_COL = "target_norm"
ID_COLS    = ["source_id", "ts_utc_end", "ts_local_start", "kwp"]


def load_recent_rows(
    n_months: int = N_RECENT_MONTHS,
    n_rows: int = N_EVAL_ROWS,
    prefix: str = "processed",
    bucket_name: str | None = None,
) -> pd.DataFrame:
    """Download the most-recent monthly CSVs and return the last ``n_rows`` rows.

    Args:
        n_months:    Number of most-recent monthly CSV files to download.
        n_rows:      Number of filtered rows to return (most recent by ts_utc_end).
        prefix:      Bucket folder prefix (default: ``"processed"``).
        bucket_name: Supabase bucket (default: SUPABASE_RAW_DATA_BUCKET env var).

    Returns:
        DataFrame with at most ``n_rows`` rows, sorted ascending by ts_utc_end.

    Raises:
        RuntimeError: If no monthly CSVs are found or all downloads fail.
    """
    from jobs.ingest_tools.supabase_storage import list_files, download_csv

    logger.info("Listing files under bucket prefix '%s/' …", prefix)
    all_names = list_files(prefix=prefix, bucket_name=bucket_name)

    month_files = sorted(
        name for name in all_names
        if name.endswith(".csv") and len(name) == 11  # "YYYY-MM.csv"
    )

    if not month_files:
        raise RuntimeError(
            f"No monthly CSVs found under '{prefix}/' in the Supabase bucket. "
            "Run the ingest pipeline first."
        )

    # Take only the most-recent N months
    recent_files = month_files[-n_months:]
    logger.info(
        "Using %d most-recent monthly file(s): %s",
        len(recent_files),
        ", ".join(f.replace(".csv", "") for f in recent_files),
    )

    frames: list[pd.DataFrame] = []
    for name in recent_files:
        path = f"{prefix}/{name}"
        logger.debug("Downloading %s …", path)
        try:
            df_month = download_csv(path, bucket_name=bucket_name)
            frames.append(df_month)
        except Exception as exc:
            logger.warning("Failed to download %s — skipped: %s", path, exc)

    if not frames:
        raise RuntimeError(
            "All download attempts failed. Check Supabase credentials."
        )

    df = pd.concat(frames, ignore_index=True)
    logger.info("Raw dataset: %d rows × %d cols", *df.shape)

    # Parse timestamps
    for ts_col in ("ts_utc_end", "ts_local_start"):
        if ts_col in df.columns:
            df[ts_col] = pd.to_datetime(df[ts_col])

    # Standard row filter (mirrors data_loader.py / dataset.load_split)
    mask = (
        df[TARGET_COL].notna()
        & (df["ghi_clear"] > 0)
        & df["ghi_fc"].notna()
    )
    df_filtered = df[mask].copy()
    n_removed = len(df) - len(df_filtered)
    logger.info(
        "Row filter: kept %d / %d rows (removed %d nighttime/invalid)",
        len(df_filtered), len(df), n_removed,
    )

    if df_filtered.empty:
        raise RuntimeError(
            "No valid rows remain after applying the standard row filter."
        )

    # Sort by timestamp and take the most-recent n_rows
    df_filtered = df_filtered.sort_values("ts_utc_end").reset_index(drop=True)
    df_eval = df_filtered.tail(n_rows).reset_index(drop=True)

    ts_min = df_eval["ts_utc_end"].min()
    ts_max = df_eval["ts_utc_end"].max()
    logger.info(
        "Evaluation window: %d rows  |  %s → %s  |  arrays: %d",
        len(df_eval),
        ts_min,
        ts_max,
        df_eval["source_id"].nunique() if "source_id" in df_eval.columns else -1,
    )

    return df_eval
