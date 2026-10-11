"""
data_loader.py — Load current and reference windows from Supabase Storage.

Window definitions (both daytime-only, no overlap):
  current   : last CURRENT_DAYS days  (default: 7)
  reference : REFERENCE_DAYS days immediately before the current window (default: 30)

Timeline (example, today = 2026-10-11):
  reference  : 2026-09-04  →  2026-10-03  (30 days)
  current    : 2026-10-04  →  2026-10-10  (7 days)
  [today]    : 2026-10-11

Daytime filter: ghi_clear > 0
  Night-time rows have zero or near-zero irradiance, which would suppress
  real drift signal. We keep only rows where modelled clear-sky GHI > 0.

Data source:
  Monthly CSV files at Supabase bucket path ``processed/YYYY-MM.csv``.
  Only the months that overlap with each window are downloaded.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta, timezone
from typing import List, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

# Window sizes (days)
CURRENT_DAYS   = 7
REFERENCE_DAYS = 30

# Columns checked for drift (BASE_FEATURES + target)
DRIFT_FEATURES: List[str] = [
    "ghi_fc", "dni_fc", "dhi_fc", "temp_fc", "cloud_fc", "wind_fc",
    "solar_zenith", "solar_azimuth", "ghi_clear", "kt_fc",
    "poa_fc", "temp_cell_fc", "p_phys_norm",
    "kwp", "tilt_deg", "surface_azimuth_pvlib",
    "hour_sin", "hour_cos", "doy_sin", "doy_cos",
    "target_norm",          # PV output — key drift indicator
]

# Daytime filter column
IRRADIANCE_COL = "ghi_clear"


def _months_for_range(start: date, end: date) -> List[str]:
    """Return sorted list of 'YYYY-MM' strings that overlap [start, end]."""
    months: list[str] = []
    cur = date(start.year, start.month, 1)
    while cur <= end:
        months.append(cur.strftime("%Y-%m"))
        # Advance to first day of next month
        if cur.month == 12:
            cur = date(cur.year + 1, 1, 1)
        else:
            cur = date(cur.year, cur.month + 1, 1)
    return months


def _download_months(
    months: List[str],
    prefix: str,
    bucket_name: str | None,
) -> pd.DataFrame:
    """Download and concatenate a list of monthly CSVs from Supabase."""
    from jobs.ingest_tools.supabase_storage import download_csv

    frames: list[pd.DataFrame] = []
    for month in months:
        path = f"{prefix}/{month}.csv"
        logger.debug("Downloading %s …", path)
        try:
            frames.append(download_csv(path, bucket_name=bucket_name))
        except Exception as exc:
            logger.warning("Failed to download %s — skipped: %s", path, exc)

    if not frames:
        raise RuntimeError(
            f"No monthly CSVs could be downloaded for months: {months}. "
            "Check Supabase credentials and ensure ingestion has run."
        )
    return pd.concat(frames, ignore_index=True)


def _filter_window(
    df: pd.DataFrame,
    start: date,
    end: date,
    ts_col: str = "ts_utc_end",
) -> pd.DataFrame:
    """Keep rows whose ts_utc_end date falls within [start, end] (inclusive)."""
    ts = pd.to_datetime(df[ts_col], utc=True)
    start_ts = pd.Timestamp(start, tz="UTC")
    end_ts   = pd.Timestamp(end,   tz="UTC") + pd.Timedelta(days=1)
    mask = (ts >= start_ts) & (ts < end_ts)
    return df[mask].copy().reset_index(drop=True)


def load_windows(
    current_days:   int = CURRENT_DAYS,
    reference_days: int = REFERENCE_DAYS,
    reference_date: date | None = None,
    prefix:         str = "processed",
    bucket_name:    str | None = None,
) -> Tuple[pd.DataFrame, pd.DataFrame, date, date, date, date]:
    """Download and return the current and reference daytime DataFrames.

    Args:
        current_days:   Length of the current window in days.
        reference_days: Length of the reference window in days.
        reference_date: Anchor date (default: today UTC).
                        Current window ends on reference_date - 1.
        prefix:         Supabase bucket folder prefix.
        bucket_name:    Supabase bucket name.

    Returns:
        Tuple of:
          - df_current   : daytime rows in the current window
          - df_reference : daytime rows in the reference window
          - cur_start, cur_end   : current window date range (inclusive)
          - ref_start, ref_end   : reference window date range (inclusive)

    Raises:
        RuntimeError: If either window is empty after filtering.
    """
    from jobs.ingest_tools.supabase_storage import list_files

    if reference_date is None:
        from datetime import datetime
        reference_date = datetime.now(timezone.utc).date()

    # Define window boundaries
    cur_end   = reference_date - timedelta(days=1)
    cur_start = cur_end - timedelta(days=current_days - 1)
    ref_end   = cur_start - timedelta(days=1)
    ref_start = ref_end - timedelta(days=reference_days - 1)

    logger.info(
        "Windows — reference: %s → %s (%d days)  |  current: %s → %s (%d days)",
        ref_start, ref_end, reference_days,
        cur_start, cur_end, current_days,
    )

    # Collect unique months needed for both windows
    ref_months = _months_for_range(ref_start, ref_end)
    cur_months = _months_for_range(cur_start, cur_end)
    all_months = sorted(set(ref_months) | set(cur_months))

    # Verify months exist in the bucket
    available = {
        f.replace(".csv", "")
        for f in list_files(prefix=prefix, bucket_name=bucket_name)
        if f.endswith(".csv") and len(f) == 11
    }
    missing = [m for m in all_months if m not in available]
    if missing:
        logger.warning(
            "%d month(s) missing from Supabase bucket: %s", len(missing), missing
        )

    to_download = [m for m in all_months if m in available]
    if not to_download:
        raise RuntimeError(
            "None of the required monthly CSVs exist in the Supabase bucket. "
            f"Required: {all_months}. Available: {sorted(available)}"
        )

    logger.info("Downloading %d monthly CSV file(s): %s", len(to_download), to_download)
    df_raw = _download_months(to_download, prefix, bucket_name)

    # Parse timestamps
    for col in ("ts_utc_end", "ts_local_start"):
        if col in df_raw.columns:
            df_raw[col] = pd.to_datetime(df_raw[col], utc=True)

    # Slice into windows
    df_ref_raw = _filter_window(df_raw, ref_start, ref_end)
    df_cur_raw = _filter_window(df_raw, cur_start, cur_end)

    # Daytime-only filter: keep rows where clear-sky GHI > 0
    def daytime_filter(df: pd.DataFrame) -> pd.DataFrame:
        if IRRADIANCE_COL not in df.columns:
            logger.warning(
                "'%s' column not found — skipping daytime filter.", IRRADIANCE_COL
            )
            return df
        mask = df[IRRADIANCE_COL] > 0
        n_removed = (~mask).sum()
        df_day = df[mask].copy().reset_index(drop=True)
        logger.debug(
            "Daytime filter: kept %d / %d rows (removed %d night rows)",
            len(df_day), len(df), n_removed,
        )
        return df_day

    df_current   = daytime_filter(df_cur_raw)
    df_reference = daytime_filter(df_ref_raw)

    for name, df_w in (("current", df_current), ("reference", df_reference)):
        if df_w.empty:
            raise RuntimeError(
                f"The {name} window is empty after daytime filtering. "
                "Check that the relevant monthly CSVs have been ingested."
            )
        logger.info(
            "%s window: %d rows  |  arrays: %d",
            name.capitalize(),
            len(df_w),
            df_w["source_id"].nunique() if "source_id" in df_w.columns else -1,
        )

    return df_current, df_reference, cur_start, cur_end, ref_start, ref_end
