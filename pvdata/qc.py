"""5-minute QC flagging module.

Flags are added to a qc_flag column (comma-joined codes).
Flags never delete records — they mark them as invalid.

Codes:
  missing   - value is NaN
  neg       - value < neg_clip_kw (invalid)
  (clip)    - value in [neg_clip_kw, 0) → set to 0, no flag
  over_kwp  - value > max_ratio_kwp × kwp (invalid; skip if kwp unknown)
  stuck     - >= stuck_min_records consecutive identical non-zero daytime values
  day_zero  - power==0 while station GHI > 300 W/m² AND fleet median power/kwp > 0.3
"""

from __future__ import annotations

import logging
import warnings
from typing import Optional

import numpy as np
import pandas as pd

from .config import PipelineConfig

logger = logging.getLogger(__name__)


# Bit per QC code. The 5-min table can have >13M rows, so flags are kept as a
# small integer bitmask while computing and exposed as a categorical string column.
QC_BITS = {"missing": 1, "neg": 2, "over_kwp": 4, "stuck": 8, "day_zero": 16}


def bitmask_to_flag(mask: int) -> str:
    """Bitmask -> comma-joined codes in a fixed order."""
    return ",".join(code for code, bit in QC_BITS.items() if mask & bit)


def flag_series_to_bitmask(flags: pd.Series) -> np.ndarray:
    """qc_flag strings (object or categorical) -> uint8 bitmask array (vectorised via categories)."""
    cat = flags.fillna("").astype(str).astype("category")
    lookup = np.zeros(len(cat.cat.categories), dtype=np.uint8)
    for i, value in enumerate(cat.cat.categories):
        for code in (c.strip() for c in value.split(",") if c.strip()):
            lookup[i] |= QC_BITS.get(code, 0)
    return lookup[cat.cat.codes.to_numpy()]


def apply_qc(
    labels: pd.DataFrame,
    arrays_meta: pd.DataFrame,
    station_5min: pd.DataFrame,
    solar_pos_5min: pd.DataFrame,
    cfg: PipelineConfig,
) -> pd.DataFrame:
    """Apply QC flags to the 5-min labels DataFrame.

    Args:
        labels: Long 5-min DataFrame (source_id, ts_utc_start, power_kw, source_file).
        arrays_meta: must have source_id, kwp.
        station_5min: wide 5-min station table (ts_utc_start, ghi_obs, ...). May be empty.
        solar_pos_5min: ts_utc_start + solar_zenith (or zenith). May be empty.
        cfg: Pipeline config.

    Returns:
        labels sorted by (source_id, ts_utc_start) with power_kw clipped and a
        categorical qc_flag column ("" = OK).
    """
    df = labels.sort_values(["source_id", "ts_utc_start"], kind="stable").reset_index(drop=True)
    df["power_kw"] = df["power_kw"].astype("float32")
    p = df["power_kw"].to_numpy()
    bits = np.zeros(len(df), dtype=np.uint8)
    # Integer code per timestamp (avoids 13M Python Timestamp objects in maps/groupbys)
    ts_codes, ts_uniques = pd.factorize(df["ts_utc_start"], sort=False)
    ts_uniques = pd.DatetimeIndex(ts_uniques)

    def _per_ts(frame: pd.DataFrame, col: str) -> np.ndarray:
        """Value of frame[col] for every row of df, looked up via the unique timestamps."""
        ser = frame.drop_duplicates("ts_utc_start").set_index("ts_utc_start")[col]
        ser.index = pd.DatetimeIndex(ser.index).tz_convert("UTC").as_unit(ts_uniques.unit)
        return ser.reindex(ts_uniques).to_numpy(dtype="float64")[ts_codes]

    # missing
    bits[np.isnan(p)] |= QC_BITS["missing"]

    # clip small negatives to 0; flag large negatives
    clip = (p >= cfg.neg_clip_kw) & (p < 0)
    p = np.where(clip, np.float32(0), p)
    df["power_kw"] = p
    bits[p < cfg.neg_clip_kw] |= QC_BITS["neg"]

    # over_kwp
    kwp = df["source_id"].map(arrays_meta.set_index("source_id")["kwp"]).to_numpy(dtype="float64")
    if np.isnan(kwp).any():
        n_unknown = df.loc[np.isnan(kwp), "source_id"].nunique()
        warnings.warn(
            f"{n_unknown} source_id(s) have unknown kwp — over_kwp check skipped for those records.",
            UserWarning, stacklevel=2,
        )
    with np.errstate(invalid="ignore"):
        bits[(~np.isnan(kwp)) & (p > cfg.max_ratio_kwp * kwp)] |= QC_BITS["over_kwp"]

    # stuck: >= stuck_min_records consecutive identical non-zero daytime values per source_id
    zen_col = None
    if not solar_pos_5min.empty:
        zen_col = next((c for c in ("solar_zenith", "zenith") if c in solar_pos_5min.columns), None)
    if zen_col is not None:
        zen = _per_ts(solar_pos_5min, zen_col)
        sid = df["source_id"].to_numpy()
        with np.errstate(invalid="ignore"):
            cand = (zen < cfg.daytime_zenith_max) & ~np.isnan(p) & (p != 0)
        same = np.zeros(len(df), dtype=bool)
        if len(df) > 1:
            same[1:] = cand[1:] & cand[:-1] & (p[1:] == p[:-1]) & (sid[1:] == sid[:-1])
        run_id = np.cumsum(~same)
        run_len = np.bincount(run_id)[run_id]
        bits[cand & (run_len >= cfg.stuck_min_records)] |= QC_BITS["stuck"]
        del zen, sid, cand, same, run_id, run_len
    else:
        logger.warning("No solar position supplied - 'stuck' check skipped")

    # day_zero: power == 0 while station GHI > 300 and fleet median (non-zero) power/kWp > 0.3
    if not station_5min.empty and "ghi_obs" in station_5min.columns:
        ghi = _per_ts(station_5min, "ghi_obs")
        with np.errstate(invalid="ignore", divide="ignore"):
            norm = np.where(p != 0, p / kwp, np.nan)
        fleet_median = pd.Series(norm).groupby(ts_codes).median()
        fleet = fleet_median.reindex(np.arange(len(ts_uniques))).to_numpy(dtype="float64")[ts_codes]
        del norm
        with np.errstate(invalid="ignore"):
            dz = (p == 0) & (ghi > 300) & (fleet > 0.3)
        bits[dz] |= QC_BITS["day_zero"]

    uniq, inv = np.unique(bits, return_inverse=True)
    df["qc_flag"] = pd.Categorical.from_codes(inv, categories=[bitmask_to_flag(int(u)) for u in uniq])

    n_flagged = int((bits != 0).sum())
    logger.info("QC complete: %d / %d records flagged (%.1f%%)",
                n_flagged, len(df), 100 * n_flagged / max(len(df), 1))
    return df
