"""
splitter.py — Chronological train / val / test split for the retrain pipeline.

Month boundaries are determined by ts_local_start (Alice Springs local time).
Split ratios: 70% train | 15% val | 15% test.

The split is computed once on the set of unique calendar months present in the
data, ensuring no data leakage across splits.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Tuple

import pandas as pd

logger = logging.getLogger(__name__)

# Default split ratios (must sum to 1.0)
TRAIN_RATIO = 0.70
VAL_RATIO   = 0.15
# TEST_RATIO = 1 - TRAIN_RATIO - VAL_RATIO = 0.15


@dataclass
class SplitInfo:
    """Summary of the chronological split."""
    train_start: str   # "YYYY-MM"
    train_end:   str
    val_start:   str
    val_end:     str
    test_start:  str
    test_end:    str
    n_months_total: int
    n_months_train: int
    n_months_val:   int
    n_months_test:  int
    n_rows_train: int
    n_rows_val:   int
    n_rows_test:  int


def split_by_time(
    df: pd.DataFrame,
    train_ratio: float = TRAIN_RATIO,
    val_ratio:   float = VAL_RATIO,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, SplitInfo]:
    """Split df chronologically into train / val / test subsets.

    Month boundaries are derived from ts_local_start.
    No rows from later months appear in earlier splits (strict cutoff).

    Args:
        df:          Full filtered DataFrame (output of data_loader).
        train_ratio: Fraction of months allocated to train (default 0.70).
        val_ratio:   Fraction of months allocated to val (default 0.15).

    Returns:
        (df_train, df_val, df_test, SplitInfo)
    """
    if "ts_local_start" not in df.columns:
        raise ValueError("DataFrame must contain 'ts_local_start' column.")

    ts = pd.to_datetime(df["ts_local_start"])
    month_keys = ts.dt.to_period("M").astype(str)

    unique_months = sorted(month_keys.unique())
    n_total = len(unique_months)

    n_train = round(n_total * train_ratio)
    n_val   = round(n_total * val_ratio)
    n_test  = n_total - n_train - n_val

    # Guard against edge cases with very few months
    n_train = max(n_train, 1)
    n_val   = max(n_val,   1)
    n_test  = max(n_test,  1)

    train_months = set(unique_months[:n_train])
    val_months   = set(unique_months[n_train: n_train + n_val])
    test_months  = set(unique_months[n_train + n_val:])

    df_train = df[month_keys.isin(train_months)].copy()
    df_val   = df[month_keys.isin(val_months)].copy()
    df_test  = df[month_keys.isin(test_months)].copy()

    info = SplitInfo(
        train_start     = unique_months[0],
        train_end       = unique_months[n_train - 1],
        val_start       = unique_months[n_train],
        val_end         = unique_months[n_train + n_val - 1],
        test_start      = unique_months[n_train + n_val],
        test_end        = unique_months[-1],
        n_months_total  = n_total,
        n_months_train  = n_train,
        n_months_val    = n_val,
        n_months_test   = n_test,
        n_rows_train    = len(df_train),
        n_rows_val      = len(df_val),
        n_rows_test     = len(df_test),
    )

    logger.info(
        "Split: Train %s→%s (%d mo, %d rows) | "
        "Val %s→%s (%d mo, %d rows) | "
        "Test %s→%s (%d mo, %d rows)",
        info.train_start, info.train_end, n_train, len(df_train),
        info.val_start,   info.val_end,   n_val,   len(df_val),
        info.test_start,  info.test_end,  n_test,  len(df_test),
    )

    return df_train, df_val, df_test, info
