"""
quality.py — Data-quality numbers the ingest job reports to Grafana.

Everything here is derived from what the pvdata pipeline already computes; nothing
is invented:

  * Range / QC flags come from train_table's ``qc_flags`` column, which pvdata.qc
    fills per 5-min record (missing, neg, over_kwp, stuck, day_zero) and
    pvdata.aggregate unions per hour. We count hourly rows carrying each flag in the
    latest calendar month of the snapshot (the month the job is updating).
  * Schema violations re-apply pvdata.validate's hard checks 1-2 on the loaded
    snapshot: duplicate (source_id, ts_utc_end) keys plus columns outside
    ALLOWED_TRAIN_TABLE_COLS. A normal run has already passed validate (it fails the
    run otherwise), so this is 0 unless the run used --skip-fetch on a bad snapshot.
  * Row counts and data lag come straight from the snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

KEY_COLS = ("source_id", "ts_utc_end")


@dataclass
class IngestQuality:
    rows_train_table:   int
    rows_usable:        int
    rows_new:           Optional[int]       # usable rows newer than Supabase's latest ts
    data_lag_hours:     Optional[float]     # now - newest ts_utc_end
    schema_violations:  int
    duplicate_keys:     int
    unauthorized_cols:  int
    flag_month:         Optional[str]
    range_flags:        dict[str, int] = field(default_factory=dict)


def _latest_month_mask(df: pd.DataFrame) -> tuple[pd.Series, Optional[str]]:
    if "ts_local_start" not in df.columns or df.empty:
        return pd.Series(True, index=df.index), None
    months = pd.to_datetime(df["ts_local_start"]).dt.to_period("M").astype(str)
    latest = months.max()
    return months == latest, latest


def count_range_flags(df: pd.DataFrame) -> tuple[dict[str, int], Optional[str]]:
    """Hourly rows per QC flag code in the latest month (all codes, zeros included)."""
    from pvdata.qc import QC_BITS, flag_series_to_bitmask

    counts = {code: 0 for code in QC_BITS}
    if "qc_flags" not in df.columns or df.empty:
        return counts, None
    mask, month = _latest_month_mask(df)
    bits = flag_series_to_bitmask(df.loc[mask, "qc_flags"])
    for code, bit in QC_BITS.items():
        counts[code] = int(((bits & bit) != 0).sum())
    return counts, month


def count_schema_violations(df: pd.DataFrame) -> tuple[int, int]:
    """(duplicate key rows, unauthorized columns) — pvdata.validate hard checks 1-2."""
    from pvdata.validate import ALLOWED_TRAIN_TABLE_COLS

    if all(c in df.columns for c in KEY_COLS):
        dupes = int(df.duplicated(subset=list(KEY_COLS)).sum())
    else:
        dupes = len(df)  # key columns missing: every row violates the key check
    extra = len(set(df.columns) - ALLOWED_TRAIN_TABLE_COLS)
    return dupes, extra


def summarize_ingest(
    df_train_table: pd.DataFrame,
    df_usable:      pd.DataFrame,
    latest_ts:      Optional[pd.Timestamp] = None,
    now:            Optional[pd.Timestamp] = None,
) -> IngestQuality:
    now = now if now is not None else pd.Timestamp.now(tz="UTC")

    lag = None
    if "ts_utc_end" in df_train_table.columns and not df_train_table.empty:
        newest = pd.to_datetime(df_train_table["ts_utc_end"], utc=True).max()
        if pd.notna(newest):
            lag = (now - newest).total_seconds() / 3600.0

    rows_new = None
    if latest_ts is not None and "ts_utc_end" in df_usable.columns:
        rows_new = int((pd.to_datetime(df_usable["ts_utc_end"], utc=True) > latest_ts).sum())

    dupes, extra = count_schema_violations(df_train_table)
    flags, month = count_range_flags(df_train_table)
    return IngestQuality(
        rows_train_table  = len(df_train_table),
        rows_usable       = len(df_usable),
        rows_new          = rows_new,
        data_lag_hours    = lag,
        schema_violations = dupes + extra,
        duplicate_keys    = dupes,
        unauthorized_cols = extra,
        flag_month        = month,
        range_flags       = flags,
    )
