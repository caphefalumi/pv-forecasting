"""Metadata loader for array parameters.

Rules:
- Read arrays_metadata_template.csv / arrays_metadata.csv by column name.
- Apply optional array_overrides.csv.
- Derive metadata_complete, surface_azimuth_pvlib, validity windows, excluded flags.
- Output arrays.parquet.

Excel-tolerance (Item 3):
- encoding="utf-8-sig" (handles BOM written by Excel's "Save As CSV UTF-8").
- All cells stripped of leading/trailing whitespace and tabs.
- Date columns parsed with explicit formats only (no ambiguous pd.to_datetime inference).
- tracking values validated against allowed set.
- exclude in overrides normalised from true/false/1/0/yes/no.
"""

from __future__ import annotations

import logging
import warnings
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from .config import PipelineConfig

logger = logging.getLogger(__name__)

# Allowed tracking values (canonical lowercase forms)
TRACKING_ALLOWED = {"fixed", "single_axis", "dual_axis"}

# Date formats tried in order — exact order from specification
DATE_FORMATS = [
    "%Y-%m-%d",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%d/%m/%Y",
    "%d/%m/%Y %H:%M",
    "%d/%m/%Y %H:%M:%S",
]


def _parse_date_strict(value: str, *, file: str, source_id, column: str) -> Optional[pd.Timestamp]:
    """Parse a date string using explicit formats only.

    Args:
        value: Raw cell value (already stripped).
        file: Filename for error messages.
        source_id: Row identifier for error messages.
        column: Column name for error messages.

    Returns:
        pd.Timestamp or None for empty/NaN.

    Raises:
        ValueError if non-empty value matches no format.
    """
    if not value or pd.isna(value):
        return None
    v = str(value).strip()
    if not v:
        return None
    for fmt in DATE_FORMATS:
        try:
            return pd.to_datetime(v, format=fmt, exact=True)
        except (ValueError, TypeError):
            continue
    raise ValueError(
        f"Unrecognised date '{v}' in file '{file}', source_id={source_id}, column='{column}'. "
        f"Accepted formats: {DATE_FORMATS}"
    )


def _normalise_exclude(value) -> bool:
    """Normalise exclude cell: true/1/yes/TRUE → True; false/0/no/FALSE → False."""
    if pd.isna(value):
        return False
    s = str(value).strip().lower()
    if s in ("1", "true", "yes"):
        return True
    if s in ("0", "false", "no", ""):
        return False
    raise ValueError(f"Unrecognised exclude value: '{value}'. Use true/false/1/0/yes/no.")


def _read_hand_edited_csv(path: Path) -> pd.DataFrame:
    """Read a hand-edited CSV with Excel BOM tolerance and cell whitespace stripping."""
    df = pd.read_csv(path, encoding="utf-8-sig", dtype=str)
    # Strip whitespace/tabs from column names
    df.columns = [c.strip() if isinstance(c, str) else c for c in df.columns]
    # Strip whitespace/tabs from every cell (handles object, string, StringDtype, etc.)
    mapper = lambda x: x.strip() if isinstance(x, str) else x
    if hasattr(df, "map"):
        df = df.map(mapper)
    else:
        df = df.applymap(mapper)
    # Replace empty strings with NaN for uniformity
    df = df.replace("", pd.NA)
    return df


def dkasc_to_pvlib_azimuth(azimuth_dkasc: float) -> float:
    """Convert DKASC azimuth (0=solar north) to pvlib azimuth (0=N, 90=E, 180=S, 270=W).

    DKASC uses 0=solar north (i.e., geographic north in Southern Hemisphere context,
    pointing toward equator = north). The convention is degrees clockwise from solar north.

    For Alice Springs (Southern Hemisphere):
    - Solar north = geographic north
    - 0° DKASC = North-facing (toward equator) → pvlib 0° (N)
    - 90° DKASC = East-facing → pvlib 90° (E)
    - 180° DKASC = South-facing → pvlib 180° (S)
    - 270° DKASC = West-facing → pvlib 270° (W)

    The conversion is a direct pass-through since DKASC "solar north" = geographic north.
    """
    if pd.isna(azimuth_dkasc):
        return float("nan")
    # Normalize to [0, 360)
    return float(azimuth_dkasc % 360)


def load_metadata(cfg: PipelineConfig) -> pd.DataFrame:
    """Load and process array metadata.

    Returns:
        DataFrame with columns:
        source_id, meter_name, array_no, manufacturer, kwp, technology,
        tilt_deg, azimuth_deg_dkasc, surface_azimuth_pvlib, tracking, inverter,
        commissioned, valid_from_utc, valid_to_utc, excluded, exclude_reason,
        metadata_complete
    """
    import pytz
    tz = pytz.timezone(cfg.tz)

    meta_path = cfg.path(cfg.metadata_file)
    if not meta_path.exists():
        raise FileNotFoundError(f"Metadata file not found: {meta_path}")

    # Read with BOM tolerance and cell stripping
    raw = _read_hand_edited_csv(meta_path)

    # Keep only array_meter rows
    arrays = raw[raw["source_type"] == "array_meter"].copy()
    logger.info("Loaded %d array_meter rows from metadata", len(arrays))

    # --- Validate tracking values ---
    for _, row in arrays.iterrows():
        raw_tv = row.get("tracking")
        tv = "" if pd.isna(raw_tv) else str(raw_tv).strip().lower()
        if tv and tv not in TRACKING_ALLOWED:
            raise ValueError(
                f"Invalid tracking value '{row['tracking']}' for source_id={row['source_id']}. "
                f"Must be one of: {sorted(TRACKING_ALLOWED)}"
            )

    arrays["tracking"] = arrays["tracking"].where(arrays["tracking"].isna(),
                                                  arrays["tracking"].astype(str).str.strip().str.lower())

    # --- Azimuth conversion ---
    arrays["azimuth_deg_dkasc"] = pd.to_numeric(arrays["azimuth_deg"], errors="coerce")
    arrays["surface_azimuth_pvlib"] = arrays["azimuth_deg_dkasc"].apply(dkasc_to_pvlib_azimuth)

    # --- metadata_complete flag ---
    def _is_complete(row: pd.Series) -> bool:
        kwp_ok = pd.notna(row.get("kwp")) and str(row.get("kwp", "")).strip() != ""
        tech_ok = pd.notna(row.get("technology")) and str(row.get("technology", "")).strip() != ""
        tracking_ok = pd.notna(row.get("tracking")) and str(row.get("tracking", "")).strip() != ""
        tracking_val = str(row.get("tracking", "")).strip().lower()
        if tracking_val == "fixed":
            tilt_ok = pd.notna(row.get("tilt_deg")) and str(row.get("tilt_deg", "")).strip() != ""
            az_ok = pd.notna(row.get("azimuth_deg")) and str(row.get("azimuth_deg", "")).strip() != ""
            return kwp_ok and tech_ok and tracking_ok and tilt_ok and az_ok
        else:
            return kwp_ok and tech_ok and tracking_ok

    arrays["metadata_complete"] = arrays.apply(_is_complete, axis=1)

    # Warn if kwp unknown
    missing_kwp = arrays[pd.to_numeric(arrays["kwp"], errors="coerce").isna()]
    if not missing_kwp.empty:
        warnings.warn(
            f"{len(missing_kwp)} array(s) have unknown kwp: {missing_kwp['source_id'].tolist()}",
            UserWarning,
            stacklevel=2,
        )

    # --- Validity windows (strict date parsing) ---
    meta_fname = meta_path.name

    def _to_utc(local_ts: Optional[pd.Timestamp]) -> Optional[pd.Timestamp]:
        if local_ts is None:
            return None
        if local_ts.tzinfo is None:
            local_ts = local_ts.tz_localize(tz)
        return local_ts.tz_convert("UTC")

    # Validity windows come ONLY from array_overrides.csv.
    # first_output / last_output in the metadata CSV are informational snapshots
    # (computed on data up to 2026-02-11) and must not truncate later data.
    arrays["valid_from_utc"] = pd.Series([None] * len(arrays), index=arrays.index, dtype="object")
    arrays["valid_to_utc"] = pd.Series([None] * len(arrays), index=arrays.index, dtype="object")

    # --- Commissioned date (strict parsing & normalization) ---
    def _parse_commissioned_val(row: pd.Series):
        c_val = row.get("commissioned")
        if not c_val or pd.isna(c_val):
            return None
        dt = _parse_date_strict(c_val, file=meta_fname, source_id=row.get("source_id"), column="commissioned")
        if dt is None:
            return None
        if dt.hour == 0 and dt.minute == 0 and dt.second == 0:
            return dt.strftime("%Y-%m-%d")
        return dt.strftime("%Y-%m-%d %H:%M:%S")

    if "commissioned" in arrays.columns:
        arrays["commissioned"] = arrays.apply(_parse_commissioned_val, axis=1)

    # --- Load overrides ---
    overrides_path = cfg.path(cfg.overrides_file)
    if overrides_path.exists():
        try:
            overrides = _read_hand_edited_csv(overrides_path)
            ov_fname = overrides_path.name
            if not overrides.empty and "source_id" in overrides.columns:
                logger.info("Applying %d override(s)", len(overrides))
                for _, ov in overrides.iterrows():
                    sid = int(str(ov["source_id"]).strip())
                    mask = arrays["source_id"].astype(str).str.strip() == str(sid)
                    if mask.any():
                        vfl = ov.get("valid_from_local")
                        if pd.notna(vfl) and str(vfl).strip():
                            arrays.loc[mask, "valid_from_utc"] = _to_utc(
                                _parse_date_strict(str(vfl), file=ov_fname,
                                                   source_id=sid, column="valid_from_local")
                            )
                        vtl = ov.get("valid_to_local")
                        if pd.notna(vtl) and str(vtl).strip():
                            arrays.loc[mask, "valid_to_utc"] = _to_utc(
                                _parse_date_strict(str(vtl), file=ov_fname,
                                                   source_id=sid, column="valid_to_local")
                            )
                        exc_raw = ov.get("exclude")
                        if pd.notna(exc_raw):
                            arrays.loc[mask, "_override_exclude"] = _normalise_exclude(exc_raw)
                            arrays.loc[mask, "_override_reason"] = ov.get("reason", "override") or "override"
        except ValueError:
            raise  # re-raise validation errors verbatim
        except Exception as exc:
            logger.warning("Could not load overrides file %s: %s", overrides_path, exc)
    else:
        logger.info("No overrides file found at %s", overrides_path)

    # --- Excluded flag ---
    auto_exclude_flags = {"near_zero_output", "stopped_early"}

    def _is_excluded(row: pd.Series) -> tuple[bool, str]:
        reasons = []
        oe = row.get("_override_exclude")
        if pd.notna(oe) and oe is True:
            reasons.append(str(row.get("_override_reason", "override")))
        raw_flag = row.get("auto_flag")
        flag = "" if pd.isna(raw_flag) else str(raw_flag).strip().lower()
        if flag in auto_exclude_flags:
            reasons.append(f"auto_flag:{flag}")
        return bool(reasons), ",".join(reasons)

    exc_results = arrays.apply(_is_excluded, axis=1, result_type="expand")
    arrays["excluded"] = exc_results[0]
    arrays["exclude_reason"] = exc_results[1]

    # --- Clean up numeric types ---
    # source_id is always a plain integer
    arrays["source_id"] = pd.to_numeric(arrays["source_id"], errors="coerce")
    # array_no is kept as string — values like "1A", "16D", "9A" are valid identifiers
    arrays["array_no"] = arrays["array_no"].astype(str).where(arrays["array_no"].notna()).str.strip()
    for col in ["kwp", "tilt_deg"]:
        arrays[col] = pd.to_numeric(arrays[col], errors="coerce").astype("float32")

    for col in ["valid_from_utc", "valid_to_utc"]:
        arrays[col] = pd.to_datetime(arrays[col], utc=True)

    # --- Select output columns ---
    out_cols = [
        "source_id", "meter_name", "array_no", "manufacturer", "kwp", "technology",
        "tilt_deg", "azimuth_deg_dkasc", "surface_azimuth_pvlib", "tracking", "inverter",
        "commissioned", "valid_from_utc", "valid_to_utc", "excluded", "exclude_reason",
        "metadata_complete",
    ]
    out_cols = [c for c in out_cols if c in arrays.columns or c in {
        "azimuth_deg_dkasc", "surface_azimuth_pvlib", "metadata_complete",
        "valid_from_utc", "valid_to_utc", "excluded", "exclude_reason"
    }]

    arrays = arrays.reindex(columns=out_cols)

    # Write to interim (will also be copied to processed/ by build.py)
    interim = cfg.path("interim")
    interim.mkdir(parents=True, exist_ok=True)
    out_path = interim / "arrays.parquet"
    arrays.to_parquet(out_path, index=False)
    logger.info("Wrote %s (%d arrays)", out_path.name, len(arrays))

    return arrays
