"""
data_loader.py — Download and merge all monthly CSVs from Supabase Storage.

Downloads every processed/YYYY-MM.csv found in the bucket, concatenates them
into a single DataFrame, and applies the standard row filter (daytime rows
with a valid target and weather forecast).

BASE_FEATURES matches the "base" feature group defined in feature_spec.json.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List

import pandas as pd

logger = logging.getLogger(__name__)

# Base feature group — mirrors feature_spec.json "base" group
BASE_FEATURES: List[str] = [
    "ghi_fc", "dni_fc", "dhi_fc", "temp_fc", "cloud_fc", "wind_fc", "wx_source",
    "solar_zenith", "solar_azimuth", "ghi_clear", "kt_fc",
    "poa_fc", "temp_cell_fc", "p_phys_norm",
    "kwp", "technology_code", "tracking_code", "tilt_deg", "surface_azimuth_pvlib",
    "hour_sin", "hour_cos", "doy_sin", "doy_cos",
]

TARGET_COL = "target_norm"

# Identifier and metadata columns kept alongside features
ID_COLS = ["source_id", "ts_utc_end", "ts_local_start", "kwp"]


def load_all_monthly_csvs(
    prefix: str = "processed",
    bucket_name: str | None = None,
) -> pd.DataFrame:
    """Download all monthly CSVs from Supabase and return a filtered DataFrame.

    Steps:
        1. List all YYYY-MM.csv files under the given prefix in the bucket.
        2. Download each file and concatenate.
        3. Apply the standard row filter:
           target_norm.notna() & ghi_clear > 0 & ghi_fc.notna()
        4. Return the filtered DataFrame sorted by (source_id, ts_utc_end).

    Args:
        prefix:      Folder prefix in the bucket (default: "processed").
        bucket_name: Bucket name (default: SUPABASE_RAW_DATA_BUCKET env var).

    Returns:
        Concatenated, filtered DataFrame with all available monthly data.

    Raises:
        RuntimeError: If no CSV files are found in the bucket.
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

    logger.info("Found %d monthly CSV files (%s → %s)",
                len(month_files),
                month_files[0].replace(".csv", ""),
                month_files[-1].replace(".csv", ""))

    frames: list[pd.DataFrame] = []
    for name in month_files:
        path = f"{prefix}/{name}"
        logger.debug("Downloading %s …", path)
        try:
            df_month = download_csv(path, bucket_name=bucket_name)
            frames.append(df_month)
        except Exception as exc:
            logger.warning("Failed to download %s — skipped: %s", path, exc)

    if not frames:
        raise RuntimeError("All download attempts failed. Check Supabase credentials.")

    logger.info("Concatenating %d monthly DataFrames …", len(frames))
    df = pd.concat(frames, ignore_index=True)
    logger.info("Raw dataset: %d rows × %d cols", *df.shape)

    # Parse timestamps
    for ts_col in ("ts_utc_end", "ts_local_start"):
        if ts_col in df.columns:
            df[ts_col] = pd.to_datetime(df[ts_col])

    # Standard row filter (mirrors dataset.load_split)
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

    df_filtered = df_filtered.sort_values(
        ["source_id", "ts_utc_end"]
    ).reset_index(drop=True)

    return df_filtered
