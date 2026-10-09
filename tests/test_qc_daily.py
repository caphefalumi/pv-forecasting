"""Unit tests for daily QC rules (dead-day label cleaning).

Tests cover §6 of PV_DEADDAY_SPEC.md:
1. One array near zero on a normal fleet day -> all hours get NaN targets and dead_day in qc_flags.
2. Whole fleet low on a day (fleet median < 0.5) -> nothing flagged.
3. Array with fewer than dead_day_min_hours valid hours -> not flagged.
4. Array at 30% of expectation (partial output) -> not flagged.
5. pr_7d computed after cleaning does not include the dead day.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import numpy as np
import pandas as pd
import pytest

from pvdata.config import PipelineConfig
from pvdata.qc_daily import apply_dead_day_qc
from pvdata.pr import compute_pr_features


def _make_cfg(**kwargs) -> PipelineConfig:
    data = {
        "qc": {
            "dead_day_max_ratio": kwargs.get("dead_day_max_ratio", 0.05),
            "dead_day_min_fleet_ratio": kwargs.get("dead_day_min_fleet_ratio", 0.5),
            "dead_day_min_hours": kwargs.get("dead_day_min_hours", 6),
        },
        "features": {
            "pr_min_valid_hours_per_day": 6,
            "pr_windows": {
                "pr_7d": {"days": 7, "min_days": 3},
                "pr_30d": {"days": 30, "min_days": 10},
            },
        },
    }
    return PipelineConfig(data)


def _make_day_df(
    source_ids: list[int],
    date_str: str,
    target_ratios: dict[int, float],
    n_daytime_hours: int = 8,
    p_phys_val: float = 0.8,
) -> pd.DataFrame:
    """Create 24 hours for given source_ids on a date."""
    rows = []
    base_dt = datetime.fromisoformat(f"{date_str}T00:00:00")
    for sid in source_ids:
        ratio = target_ratios.get(sid, 0.9)
        for h in range(24):
            dt = base_dt + timedelta(hours=h)
            is_day = 8 <= h < (8 + n_daytime_hours)
            p_phys = p_phys_val if is_day else 0.0
            ghi_clear = 500.0 if is_day else 0.0
            ghi_fc = 500.0 if is_day else 0.0
            kwp = 10.0
            if is_day:
                t_norm = p_phys * ratio
                t_kw = t_norm * kwp
            else:
                t_norm = np.nan
                t_kw = np.nan
            rows.append({
                "source_id": sid,
                "array_no": f"A{sid}",
                "ts_local_start": dt,
                "p_phys_norm": p_phys,
                "target_norm": t_norm,
                "target_kw": t_kw,
                "ghi_clear": ghi_clear,
                "ghi_fc": ghi_fc,
                "kwp": kwp,
                "qc_flags": "",
                "array_split": "train",
            })
    return pd.DataFrame(rows)


def test_dead_day_one_array_near_zero():
    """1. One array near zero on a day when the fleet is normal -> flagged."""
    cfg = _make_cfg()
    df = _make_day_df(
        source_ids=[1, 2, 3],
        date_str="2025-05-10",
        target_ratios={1: 0.01, 2: 0.9, 3: 0.9},
    )

    out, summary = apply_dead_day_qc(df, cfg)

    assert summary.total_dead_days == 1
    assert 1 in summary.dead_days_dict
    assert summary.dead_days_dict[1] == 1
    assert summary.hourly_rows_affected == 24

    # Array 1: all 24 hours have NaN target and dead_day flag
    df_arr1 = out[out["source_id"] == 1]
    assert df_arr1["target_kw"].isna().all()
    assert df_arr1["target_norm"].isna().all()
    assert (df_arr1["qc_flags"] == "dead_day").all()

    # Array 2 and 3: unchanged
    for sid in [2, 3]:
        df_arr = out[out["source_id"] == sid]
        daytime = df_arr[df_arr["p_phys_norm"] > 0]
        assert daytime["target_kw"].notna().all()
        assert (df_arr["qc_flags"] == "").all()


def test_dead_day_whole_fleet_low():
    """2. Whole fleet low on a day (fleet median < 0.5) -> nothing flagged."""
    cfg = _make_cfg()
    df = _make_day_df(
        source_ids=[1, 2, 3],
        date_str="2025-05-10",
        target_ratios={1: 0.01, 2: 0.2, 3: 0.3},  # fleet median = 0.2 < 0.5
    )

    out, summary = apply_dead_day_qc(df, cfg)

    assert summary.total_dead_days == 0
    assert len(summary.dead_days_dict) == 0
    assert summary.hourly_rows_affected == 0

    # Array 1 not flagged because fleet was also low
    df_arr1 = out[out["source_id"] == 1]
    daytime1 = df_arr1[df_arr1["p_phys_norm"] > 0]
    assert daytime1["target_kw"].notna().all()
    assert (df_arr1["qc_flags"] == "").all()


def test_dead_day_fewer_than_min_hours():
    """3. Array with fewer than dead_day_min_hours valid hours -> not flagged."""
    cfg = _make_cfg(dead_day_min_hours=6)

    # Array 1 has only 4 daytime hours (< 6)
    df_arr1 = _make_day_df([1], "2025-05-10", {1: 0.01}, n_daytime_hours=4)
    # Arrays 2, 3 have 8 daytime hours
    df_fleet = _make_day_df([2, 3], "2025-05-10", {2: 0.9, 3: 0.9}, n_daytime_hours=8)
    df = pd.concat([df_arr1, df_fleet], ignore_index=True)

    out, summary = apply_dead_day_qc(df, cfg)

    assert summary.total_dead_days == 0
    assert 1 not in summary.dead_days_dict


def test_dead_day_partial_output_not_flagged():
    """4. Array at 30% of expectation (partial output) -> not flagged."""
    cfg = _make_cfg(dead_day_max_ratio=0.05)
    df = _make_day_df(
        source_ids=[1, 2, 3],
        date_str="2025-05-10",
        target_ratios={1: 0.30, 2: 0.9, 3: 0.9},
    )

    out, summary = apply_dead_day_qc(df, cfg)

    assert summary.total_dead_days == 0
    assert 1 not in summary.dead_days_dict


def test_pr_7d_ignores_dead_day():
    """5. pr_7d computed after cleaning does not include the dead day."""
    cfg = _make_cfg()

    # Create 8 days: 2025-05-01 through 2025-05-08
    # Days 01-06: array 1 ratio = 1.0 (normal)
    # Day 07: array 1 ratio = 0.01 (dead day)
    # Day 08: array 1 ratio = 1.0 (target evaluation day)
    # Fleet arrays 2, 3 are normal (0.9) on all days
    days = []
    base_date = datetime(2025, 5, 1)
    for i in range(8):
        d_str = (base_date + timedelta(days=i)).strftime("%Y-%m-%d")
        r1 = 0.01 if i == 6 else 1.0  # Day 7 (index 6) is dead
        day_df = _make_day_df(
            source_ids=[1, 2, 3],
            date_str=d_str,
            target_ratios={1: r1, 2: 0.9, 3: 0.9},
        )
        days.append(day_df)

    df_full = pd.concat(days, ignore_index=True)

    # Apply dead-day QC
    cleaned, summary = apply_dead_day_qc(df_full, cfg)
    assert summary.total_dead_days == 1
    assert summary.dead_days_dict[1] == 1

    # Compute PR features on cleaned dataframe
    pr_df = compute_pr_features(cleaned, cfg)
    cleaned["pr_7d"] = pr_df["pr_7d"]

    # Check PR on Day 8 (2025-05-08) for array 1
    day8_arr1 = cleaned[(cleaned["source_id"] == 1) & (cleaned["ts_local_start"].dt.date == datetime(2025, 5, 8).date())]
    day8_pr = day8_arr1["pr_7d"].dropna().iloc[0]

    # Lookback window for Day 8 is [Day 1, Day 7].
    # Without dead-day cleaning, day 7 would pull PR down to (6 * 1.0 + 0.01) / 7 ~ 0.858.
    # With dead-day cleaning, day 7 has target_kw == NaN, so 0 valid hours (< 6), so day 7 is excluded.
    # The valid days are days 1..6 (6 valid days >= min_days 3), each with ratio 1.0.
    # Therefore, pr_7d on Day 8 must be exactly 1.0!
    assert np.isclose(day8_pr, 1.0, atol=1e-3)
