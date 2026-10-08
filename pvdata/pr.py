"""Recent performance ratio features (pr_7d, pr_30d).

Algorithm:
For each array and local day d, over hours with target_kw notna, p_phys_norm notna and ghi_clear > 0:
    actual_kwh[d]   = sum(target_kw)
    expected_kwh[d] = sum(p_phys_norm * kwp)
    valid_day[d]    = count of such hours >= pr_min_valid_hours_per_day

For a row on local day D:
    pr_Nd = sum(actual_kwh[d]) / sum(expected_kwh[d]) over valid days d in [D-N, D-1].
    NaN if fewer than min_days valid days.
    Clip to [0, 1.5].
    Same value for every hour of day D.

Leakage rule (Decision 4):
    The value for day D must not depend on any label from day D or later.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Dict, Tuple

import numpy as np
import pandas as pd

from .config import PipelineConfig

logger = logging.getLogger(__name__)


def compute_pr_features(
    df: pd.DataFrame,
    cfg: PipelineConfig,
) -> pd.DataFrame:
    """Compute pr_7d and pr_30d rolling performance ratio features.

    Args:
        df: DataFrame containing at least:
            source_id, ts_local_start (tz-aware or datetime), target_kw,
            p_phys_norm, kwp, ghi_clear.
        cfg: Pipeline configuration containing pr thresholds.

    Returns:
        DataFrame with columns pr_7d, pr_30d (float32) aligned to df.index.
    """
    min_hours = cfg.pr_min_valid_hours_per_day
    windows = cfg.pr_windows

    # Copy / extract needed columns
    needed = ["source_id", "ts_local_start", "target_kw", "p_phys_norm", "kwp", "ghi_clear"]
    for c in needed:
        if c not in df.columns:
            raise ValueError(f"Missing required column for PR calculation: {c}")

    res = pd.DataFrame(index=df.index)
    res["pr_7d"] = np.nan
    res["pr_30d"] = np.nan

    if df.empty:
        res["pr_7d"] = res["pr_7d"].astype("float32")
        res["pr_30d"] = res["pr_30d"].astype("float32")
        return res

    # Local date per row
    ts_local = pd.to_datetime(df["ts_local_start"])
    local_dates = ts_local.dt.date

    # Mask of qualifying hours: target_kw notna & p_phys_norm notna & ghi_clear > 0
    t_kw = pd.to_numeric(df["target_kw"], errors="coerce")
    p_norm = pd.to_numeric(df["p_phys_norm"], errors="coerce")
    kwp = pd.to_numeric(df["kwp"], errors="coerce")
    gc = pd.to_numeric(df["ghi_clear"], errors="coerce")

    mask = t_kw.notna() & p_norm.notna() & (gc > 0)

    # Compute daily aggregates per (source_id, local_date)
    sub = pd.DataFrame({
        "source_id": df["source_id"].to_numpy(),
        "local_date": local_dates.to_numpy(),
        "actual_kwh": np.where(mask, t_kw.to_numpy(), 0.0),
        "expected_kwh": np.where(mask, p_norm.to_numpy() * kwp.to_numpy(), 0.0),
        "valid_hour": mask.to_numpy().astype(int),
    })

    daily = sub.groupby(["source_id", "local_date"], sort=True).agg({
        "actual_kwh": "sum",
        "expected_kwh": "sum",
        "valid_hour": "sum",
    }).reset_index()

    daily["is_valid_day"] = daily["valid_hour"] >= min_hours

    # For each source_id, build daily time-series indexed by date
    # Then for each date D in df, compute PR over [D-N, D-1]
    pr_results = {}
    for win_name, win_cfg in windows.items():
        n_days = win_cfg.get("days", 7)
        min_days = win_cfg.get("min_days", 3)
        pr_results[win_name] = {}

    for sid, group in daily.groupby("source_id"):
        # Map date -> (actual_kwh, expected_kwh, is_valid_day)
        day_map = {
            row["local_date"]: (row["actual_kwh"], row["expected_kwh"], row["is_valid_day"])
            for _, row in group.iterrows()
        }

        # Unique dates for this array in df
        arr_dates = set(local_dates[df["source_id"] == sid])

        for d in arr_dates:
            for win_name, win_cfg in windows.items():
                n_days = win_cfg.get("days", 7)
                min_days = win_cfg.get("min_days", 3)

                # Days in [D-n_days, D-1]
                act_sum = 0.0
                exp_sum = 0.0
                n_valid = 0

                for i in range(1, n_days + 1):
                    past_d = d - timedelta(days=i)
                    val = day_map.get(past_d)
                    if val is not None and val[2]:  # is_valid_day
                        act_sum += val[0]
                        exp_sum += val[1]
                        n_valid += 1

                if n_valid >= min_days and exp_sum > 0:
                    val_pr = float(np.clip(act_sum / exp_sum, 0.0, 1.5))
                else:
                    val_pr = np.nan

                pr_results[win_name][(sid, d)] = val_pr

    # Map back to rows
    sid_arr = df["source_id"].to_numpy()
    date_arr = local_dates.to_numpy()
    keys = list(zip(sid_arr, date_arr))

    for win_name in windows.keys():
        win_dict = pr_results[win_name]
        vals = [win_dict.get(k, np.nan) for k in keys]
        res[win_name] = np.asarray(vals, dtype=np.float32)

    return res
