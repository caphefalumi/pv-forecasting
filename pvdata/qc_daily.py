"""Daily QC rules: dead-day label cleaning.

Rule:
For each usable array (array_split in ['train', 'coldstart']) and local day d (date of ts_local_start),
over hours with target_kw notna, p_phys_norm notna and ghi_clear > 0:
    hours[d]    = number of such hours
    ratio[d]    = sum(target_norm) / sum(p_phys_norm)
    valid[d]    = hours[d] >= qc.dead_day_min_hours
    fleet[d]    = median of ratio[d] over usable arrays with valid[d]
    dead[d]     = valid[d] and ratio[d] < qc.dead_day_max_ratio and fleet[d] >= qc.dead_day_min_fleet_ratio

For every hour of a dead array-day: set target_kw and target_norm to NaN and append dead_day to qc_flags.
Leave power_kw_mean and coverage unchanged. Applies to all periods and all splits.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

import numpy as np
import pandas as pd

from .config import PipelineConfig

logger = logging.getLogger(__name__)


@dataclass
class DeadDaySummary:
    total_dead_days: int
    hourly_rows_affected: int
    usable_rows_removed: int
    by_array: pd.DataFrame  # columns: source_id, array_no, days, first, last
    dead_days_dict: Dict[int, int]  # {source_id: count}


def apply_dead_day_qc(
    df: pd.DataFrame,
    cfg: PipelineConfig,
) -> Tuple[pd.DataFrame, DeadDaySummary]:
    """Identify and clean dead-day inverter/standby dropouts.

    Args:
        df: train_table with columns source_id, ts_local_start, target_kw, target_norm,
            p_phys_norm, ghi_clear, qc_flags, and optional array_split, array_no.
        cfg: Pipeline configuration containing dead_day thresholds.

    Returns:
        Tuple of (cleaned_df, DeadDaySummary).
    """
    if df.empty:
        empty_summary = DeadDaySummary(
            total_dead_days=0,
            hourly_rows_affected=0,
            usable_rows_removed=0,
            by_array=pd.DataFrame(columns=["source_id", "array_no", "days", "first", "last"]),
            dead_days_dict={},
        )
        return df, empty_summary

    min_hours = cfg.dead_day_min_hours
    max_ratio = cfg.dead_day_max_ratio
    min_fleet = cfg.dead_day_min_fleet_ratio

    # Usable arrays filter: array_split in ['train', 'coldstart']
    if "array_split" in df.columns:
        usable_mask = df["array_split"].isin(["train", "coldstart"])
    else:
        usable_mask = pd.Series(True, index=df.index)

    # Local date
    local_dates = pd.to_datetime(df["ts_local_start"]).dt.date

    # Hours with target_kw notna, p_phys_norm notna and ghi_clear > 0
    t_kw = pd.to_numeric(df["target_kw"], errors="coerce")
    p_norm = pd.to_numeric(df["p_phys_norm"], errors="coerce")
    t_norm = pd.to_numeric(df["target_norm"], errors="coerce")
    gc = pd.to_numeric(df["ghi_clear"], errors="coerce") if "ghi_clear" in df.columns else pd.Series(1, index=df.index)

    qualifying = usable_mask & t_kw.notna() & p_norm.notna() & (gc > 0)

    # If no qualifying rows, return immediately
    if not qualifying.any():
        empty_summary = DeadDaySummary(
            total_dead_days=0,
            hourly_rows_affected=0,
            usable_rows_removed=0,
            by_array=pd.DataFrame(columns=["source_id", "array_no", "days", "first", "last"]),
            dead_days_dict={},
        )
        return df, empty_summary

    # Aggregate qualifying hours by (source_id, local_date)
    sub = pd.DataFrame({
        "source_id": df.loc[qualifying, "source_id"],
        "date": local_dates[qualifying],
        "target_norm": t_norm[qualifying],
        "p_phys_norm": p_norm[qualifying],
    })

    agg = sub.groupby(["source_id", "date"]).agg(
        n_hours=("target_norm", "count"),
        sum_target=("target_norm", "sum"),
        sum_phys=("p_phys_norm", "sum"),
    ).reset_index()

    # Ratio and validity
    agg["ratio"] = np.where(agg["sum_phys"] > 0, agg["sum_target"] / agg["sum_phys"], np.nan)
    agg["valid"] = (agg["n_hours"] >= min_hours) & agg["ratio"].notna()

    # Fleet median per day over valid arrays
    valid_agg = agg[agg["valid"]]
    if not valid_agg.empty:
        fleet = valid_agg.groupby("date")["ratio"].median().rename("fleet_ratio")
        agg = agg.merge(fleet, on="date", how="left")
    else:
        agg["fleet_ratio"] = np.nan

    # Dead condition
    agg["dead"] = (
        agg["valid"]
        & (agg["ratio"] < max_ratio)
        & (agg["fleet_ratio"] >= min_fleet)
    )

    dead_records = agg[agg["dead"]]
    dead_pairs = set(zip(dead_records["source_id"], dead_records["date"]))

    if not dead_pairs:
        empty_summary = DeadDaySummary(
            total_dead_days=0,
            hourly_rows_affected=0,
            usable_rows_removed=0,
            by_array=pd.DataFrame(columns=["source_id", "array_no", "days", "first", "last"]),
            dead_days_dict={},
        )
        return df, empty_summary

    # Flag hourly rows
    row_pairs = list(zip(df["source_id"], local_dates))
    is_dead_hour = pd.Series([p in dead_pairs for p in row_pairs], index=df.index)

    # Count usable rows removed (where row_filter would have passed: target_norm notna, ghi_clear > 0, ghi_fc notna)
    ghi_fc = pd.to_numeric(df["ghi_fc"], errors="coerce") if "ghi_fc" in df.columns else pd.Series(1, index=df.index)
    usable_rows_removed = int((is_dead_hour & t_norm.notna() & (gc > 0) & ghi_fc.notna()).sum())
    total_hourly_rows_affected = int(is_dead_hour.sum())

    # Update df
    out = df.copy()
    out.loc[is_dead_hour, "target_kw"] = np.nan
    out.loc[is_dead_hour, "target_norm"] = np.nan

    if "qc_flags" in out.columns:
        existing_flags = out.loc[is_dead_hour, "qc_flags"].fillna("").astype(str)
        new_flags = np.where(
            existing_flags == "",
            "dead_day",
            np.where(
                existing_flags.str.contains("dead_day"),
                existing_flags,
                existing_flags + ",dead_day",
            ),
        )
        out.loc[is_dead_hour, "qc_flags"] = new_flags

    # Build by_array summary table
    arr_map = out.groupby("source_id")["array_no"].first() if "array_no" in out.columns else pd.Series(dtype=object)

    array_stats = []
    for sid, grp in dead_records.groupby("source_id"):
        array_stats.append({
            "source_id": sid,
            "array_no": arr_map.get(sid, str(sid)),
            "days": len(grp),
            "first": grp["date"].min().isoformat(),
            "last": grp["date"].max().isoformat(),
        })

    by_array = pd.DataFrame(array_stats).sort_values("source_id").reset_index(drop=True)
    dead_days_dict = {int(r["source_id"]): int(r["days"]) for _, r in by_array.iterrows()}

    summary = DeadDaySummary(
        total_dead_days=len(dead_pairs),
        hourly_rows_affected=total_hourly_rows_affected,
        usable_rows_removed=usable_rows_removed,
        by_array=by_array,
        dead_days_dict=dead_days_dict,
    )

    logger.info(
        "Dead-day QC complete: %d dead array-days, %d hourly rows flagged (%d usable rows removed)",
        summary.total_dead_days,
        summary.hourly_rows_affected,
        summary.usable_rows_removed,
    )

    return out, summary
