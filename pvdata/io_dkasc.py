"""Read DKASC yearly CSV files and API chunks → long 5-min table.

Ingestion rules:
- Pre-scan each file for malformed lines (bad field counts); skip them.
- Parse timestamp as Australia/Darwin (UTC+09:30, no DST) → UTC.
- Extract array_meter Active_Power columns + 101_* weather columns.
- Melt to long format: (source_id, ts_utc_start, power_kw, source_file).
- Station data → separate wide table.
- Load API chunks via source_names.csv mapping.
- Deduplicate on (source_id, ts_utc_start): yearly wins.
- Write interim/labels_5min.parquet and interim/station_5min.parquet.
"""

from __future__ import annotations

import csv
import logging
import re
import warnings
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from .config import PipelineConfig

logger = logging.getLogger(__name__)

# Weather column mapping from 101_* names in yearly files
STATION_COLUMN_RENAMES = {
    "ghi_obs": re.compile(r"101.*Global_Horizontal_Radiation", re.IGNORECASE),
    "dhi_obs": re.compile(r"101.*Diffuse_Horizontal_Radiation", re.IGNORECASE),
    "temp_obs": re.compile(r"101.*Weather_Temperature_Celsius", re.IGNORECASE),
    "wind_obs": re.compile(r"101.*Wind_Speed", re.IGNORECASE),
    "rh_obs": re.compile(r"101.*Weather_Relative_Humidity", re.IGNORECASE),
    "rain_obs": re.compile(r"101.*Weather_Daily_Rainfall", re.IGNORECASE),
    "gti_obs": re.compile(r"101.*Radiation_Global_Tilted", re.IGNORECASE),
    "wind_dir_obs": re.compile(r"101.*Wind_Direction", re.IGNORECASE),
    "dti_obs": re.compile(r"101.*Radiation_Diffuse_Tilted", re.IGNORECASE),
}

# API chunk weather column substrings → standard name
API_WEATHER_MAP = {
    "Global Horizontal Radiation": "ghi_obs",
    "Weather Temperature": "temp_obs",
    "Wind Speed": "wind_obs",
    "Daily Rainfall": "rain_obs",
}


def _extract_source_id(col: str) -> Optional[int]:
    """Return the numeric source_id prefix from a column name, or None."""
    m = re.match(r"^(\d+)_", col)
    if m:
        return int(m.group(1))
    return None


def _is_active_power(col: str) -> bool:
    """True if column ends with _Active_Power (not Reactive or Apparent)."""
    # Must end with _Active_Power and NOT contain Reactive or Apparent before Active
    return bool(re.search(r"_Active_Power$", col)) and "Reactive" not in col and "Apparent" not in col


def _pre_scan_file(filepath: Path) -> tuple[int, list[tuple[int, int, str]]]:
    """Count fields per row vs header; return (expected_fields, bad_lines).

    bad_lines: list of (file_line_number, actual_field_count, first_30_chars)
    Uses stdlib csv module (no pandas).
    """
    bad_lines: list[tuple[int, int, str]] = []
    expected = None

    with open(filepath, "r", encoding="utf-8", errors="replace") as fh:
        reader = csv.reader(fh)
        for lineno, row in enumerate(reader, start=1):
            n = len(row)
            if lineno == 1:
                expected = n
                continue
            if n != expected:
                # first 30 chars of the raw line (re-open not needed, reconstruct)
                snippet = ",".join(row)[:30]
                bad_lines.append((lineno, n, snippet))

    return expected or 0, bad_lines


def _read_yearly_file(
    filepath: Path,
    array_source_ids: set[int],
    bad_line_numbers: set[int],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Read one yearly CSV, skip bad lines, return (power_long, station_wide).

    power_long columns: source_id (int16), ts_utc_start, power_kw (float32), source_file (category)
    station_wide columns: ts_utc_start, ghi_obs, dhi_obs, temp_obs, wind_obs, rh_obs, rain_obs, gti_obs, ...
    """
    # --- read header to find relevant columns ---
    with open(filepath, "r", encoding="utf-8", errors="replace") as fh:
        header = next(csv.reader(fh))

    # Identify columns to keep
    usecols = ["timestamp"]
    array_cols: dict[str, int] = {}  # col_name -> source_id
    station_cols: dict[str, str] = {}  # col_name -> standard_name

    for col in header:
        sid = _extract_source_id(col)
        if sid is None:
            continue

        if sid in array_source_ids and _is_active_power(col):
            array_cols[col] = sid
            usecols.append(col)
        elif sid == 101:
            for std_name, pattern in STATION_COLUMN_RENAMES.items():
                if pattern.search(col):
                    station_cols[col] = std_name
                    usecols.append(col)
                    break

    # Remove duplicates while preserving order
    seen = set()
    usecols_dedup = []
    for c in usecols:
        if c not in seen:
            seen.add(c)
            usecols_dedup.append(c)

    # Build skiprows — bad_line_numbers are 1-based line numbers in the file (header is line 1).
    # The callable passed to pandas skiprows receives 0-indexed line numbers of the file
    # (header is index 0, line 2 is index 1, ..., line N is index N-1).
    # Therefore, line N corresponds to index n - 1.
    skip_data_rows = {n - 1 for n in bad_line_numbers if n > 1}

    try:
        df = pd.read_csv(
            filepath,
            usecols=usecols_dedup,
            dtype={c: "float32" for c in usecols_dedup if c != "timestamp"},
            skiprows=lambda i: i in skip_data_rows,
            low_memory=False,
        )
    except Exception as exc:
        logger.error("Failed to read %s: %s", filepath, exc)
        raise

    # Parse timestamp → UTC
    df["ts_utc_start"] = (
        pd.to_datetime(df["timestamp"], format="%Y-%m-%d %H:%M:%S")
        .dt.tz_localize("Australia/Darwin")
        .dt.tz_convert("UTC")
    )
    df = df.drop(columns=["timestamp"])

    source_file_name = filepath.name

    # --- Station wide table ---
    station_rename = {k: v for k, v in station_cols.items() if k in df.columns}
    station_df = df[["ts_utc_start"] + list(station_rename.keys())].rename(columns=station_rename)

    # --- Power long table ---
    power_rows = []
    for col, sid in array_cols.items():
        if col not in df.columns:
            continue
        tmp = df[["ts_utc_start", col]].copy()
        tmp = tmp.rename(columns={col: "power_kw"})
        tmp["source_id"] = np.int16(sid)
        tmp["source_file"] = source_file_name
        power_rows.append(tmp)

    if power_rows:
        power_long = pd.concat(power_rows, ignore_index=True)
        power_long["source_id"] = power_long["source_id"].astype("int16")
        power_long["source_file"] = power_long["source_file"].astype("category")
        power_long["power_kw"] = power_long["power_kw"].astype("float32")
    else:
        power_long = pd.DataFrame(columns=["ts_utc_start", "power_kw", "source_id", "source_file"])

    return power_long, station_df


def _load_api_chunks(
    api_dir: Path,
    name_map_path: Path,
    array_source_ids: set[int],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Load all API chunk CSVs → (power_long, station_wide)."""
    if not api_dir.exists():
        logger.info("API dir %s does not exist — skipping", api_dir)
        return pd.DataFrame(), pd.DataFrame(), 0

    # Load source name map
    name_map = pd.read_csv(name_map_path)
    col_to_sid: dict[str, int] = dict(zip(name_map["export_column"], name_map["source_id"].astype(int)))

    chunk_files = sorted(api_dir.glob("*.csv"))
    chunk_files = [f for f in chunk_files if f.name != "source_names.csv"]

    if not chunk_files:
        logger.info("No API chunk files found in %s", api_dir)
        return pd.DataFrame(), pd.DataFrame(), 0

    all_power: list[pd.DataFrame] = []
    all_station: list[pd.DataFrame] = []

    for chunk_file in chunk_files:
        try:
            df = pd.read_csv(chunk_file, low_memory=False)
        except Exception as exc:
            logger.warning("Could not read API chunk %s: %s", chunk_file.name, exc)
            continue

        # Find timestamp column
        ts_col = next((c for c in df.columns if "timestamp" in c.lower() or c.lower() == "timestamp"), None)
        if ts_col is None:
            logger.warning("No timestamp column in %s, skipping", chunk_file.name)
            continue

        df["ts_utc_start"] = (
            pd.to_datetime(df[ts_col], format="%Y-%m-%d %H:%M:%S")
            .dt.tz_localize("Australia/Darwin")
            .dt.tz_convert("UTC")
        )

        station_row: dict = {"ts_utc_start": df["ts_utc_start"]}
        power_rows_chunk: list[pd.DataFrame] = []

        for col in df.columns:
            if col == ts_col or col == "ts_utc_start":
                continue

            sid = col_to_sid.get(col)
            if sid is not None and sid in array_source_ids:
                tmp = df[["ts_utc_start", col]].copy()
                tmp = tmp.rename(columns={col: "power_kw"})
                tmp["source_id"] = np.int16(sid)
                tmp["source_file"] = chunk_file.name
                tmp["power_kw"] = pd.to_numeric(tmp["power_kw"], errors="coerce").astype("float32")
                power_rows_chunk.append(tmp)
            else:
                # Check weather substring mapping
                for substring, std_name in API_WEATHER_MAP.items():
                    if substring in col:
                        station_row[std_name] = pd.to_numeric(df[col], errors="coerce").astype("float32")
                        break

        if power_rows_chunk:
            all_power.append(pd.concat(power_rows_chunk, ignore_index=True))

        if len(station_row) > 1:
            all_station.append(pd.DataFrame(station_row))

    power_df = pd.concat(all_power, ignore_index=True) if all_power else pd.DataFrame()
    station_df = pd.concat(all_station, ignore_index=True) if all_station else pd.DataFrame()

    # Dedup overlapping chunks: keep row from the most-recently-modified file.
    # "source_file" column holds the filename; map to mtime for sorting.
    api_api_duplicates = 0
    if not power_df.empty and power_df.duplicated(subset=["source_id", "ts_utc_start"]).any():
        api_api_duplicates = int(power_df.duplicated(subset=["source_id", "ts_utc_start"]).sum())
        mtime_map: dict[str, float] = {
            f.name: f.stat().st_mtime for f in chunk_files
        }
        power_df["_mtime"] = power_df["source_file"].map(mtime_map).fillna(0)
        power_df = (
            power_df
            .sort_values("_mtime", ascending=True)   # oldest first …
            .drop_duplicates(subset=["source_id", "ts_utc_start"], keep="last")  # … keep newest
            .drop(columns=["_mtime"])
        )
        logger.info(
            "API chunks: %d overlapping records deduplicated (kept latest-mtime file)",
            api_api_duplicates,
        )

    if not station_df.empty and station_df.duplicated(subset=["ts_utc_start"]).any():
        station_df = station_df.drop_duplicates(subset=["ts_utc_start"], keep="last")

    return power_df, station_df, api_api_duplicates


def load_dkasc(cfg: PipelineConfig) -> dict:
    """Load all DKASC sources → long 5-min tables written to interim/.

    Returns a dict of stats for the QC report.
    """
    # Load metadata to get array_meter source_ids
    meta_path = cfg.path(cfg.metadata_file)
    meta = pd.read_csv(meta_path, encoding="utf-8-sig", dtype=str)
    meta.columns = [c.strip() for c in meta.columns]
    meta["source_type"] = meta["source_type"].str.strip()
    array_source_ids: set[int] = set(
        meta.loc[meta["source_type"] == "array_meter", "source_id"].astype(int)
    )
    logger.info("Found %d array_meter source_ids in metadata", len(array_source_ids))

    stats: dict = {
        "bad_lines": [],
        "rows_read": 0,
        "duplicates_dropped": 0,
        "api_yearly_conflicts": 0,
        "api_api_duplicates": 0,
    }

    all_power: list[pd.DataFrame] = []
    all_station: list[pd.DataFrame] = []
    yearly_source_ts: set[tuple] = set()  # for dedup tracking

    # --- Yearly files ---
    for fname in cfg.yearly_files:
        fpath = cfg.path(fname)
        if not fpath.exists():
            logger.warning("Yearly file not found: %s", fpath)
            continue

        logger.info("Pre-scanning %s ...", fname)
        expected_fields, bad_lines = _pre_scan_file(fpath)
        if bad_lines:
            logger.warning(
                "%s: %d malformed line(s): %s",
                fname,
                len(bad_lines),
                [(ln, fc) for ln, fc, _ in bad_lines],
            )
            stats["bad_lines"].extend(
                {"file": fname, "line": ln, "fields": fc, "snippet": snip}
                for ln, fc, snip in bad_lines
            )

        bad_line_numbers = {bl[0] for bl in bad_lines}

        logger.info("Reading %s ...", fname)
        power_df, station_df = _read_yearly_file(fpath, array_source_ids, bad_line_numbers)

        stats["rows_read"] += len(power_df)
        all_power.append(power_df)
        all_station.append(station_df)

    # Concatenate yearly
    yearly_power = pd.concat(all_power, ignore_index=True) if all_power else pd.DataFrame()
    yearly_station = pd.concat(all_station, ignore_index=True) if all_station else pd.DataFrame()

    # --- API chunks ---
    api_dir = cfg.path(cfg.api_dir)
    api_name_map = cfg.path(cfg.api_name_map)
    api_power, api_station, api_api_dups = _load_api_chunks(api_dir, api_name_map, array_source_ids)
    stats["api_api_duplicates"] = api_api_dups

    # --- Deduplicate and merge ---
    if not yearly_power.empty and not api_power.empty:
        # Mark origin BEFORE concat so keep="first" correctly retains yearly value
        yearly_power["_origin"] = "yearly"
        api_power["_origin"] = "api"
        combined = pd.concat([yearly_power, api_power], ignore_index=True)

        # Count conflicts before dedup
        key_cols = ["source_id", "ts_utc_start"]
        dup_mask = combined.duplicated(subset=key_cols, keep=False)
        dup_pairs = combined[dup_mask].sort_values(key_cols + ["_origin"])
        if not dup_pairs.empty:
            yearly_vals = dup_pairs[dup_pairs["_origin"] == "yearly"].set_index(key_cols)["power_kw"]
            api_vals = dup_pairs[dup_pairs["_origin"] == "api"].set_index(key_cols)["power_kw"]
            common_idx = yearly_vals.index.intersection(api_vals.index)
            conflicts = (abs(yearly_vals.loc[common_idx] - api_vals.loc[common_idx]) > 0.01).sum()
            stats["api_yearly_conflicts"] = int(conflicts)
            if conflicts > 0:
                logger.warning(
                    "%d records differ by >0.01 kW between yearly and API sources (yearly value kept)",
                    conflicts,
                )

        # Keep first (yearly) on duplicate keys
        n_before = len(combined)
        combined = combined.drop_duplicates(subset=key_cols, keep="first")
        stats["duplicates_dropped"] = n_before - len(combined)
        combined = combined.drop(columns=["_origin"])
        power_final = combined

    elif not yearly_power.empty:
        power_final = yearly_power.drop(columns=["_origin"], errors="ignore")
    elif not api_power.empty:
        power_final = api_power
    else:
        power_final = pd.DataFrame(columns=["source_id", "ts_utc_start", "power_kw", "source_file"])

    # --- Station ---
    if not yearly_station.empty and not api_station.empty:
        station_final = pd.concat([yearly_station, api_station], ignore_index=True)
        station_final = station_final.drop_duplicates(subset=["ts_utc_start"], keep="first")
    elif not yearly_station.empty:
        station_final = yearly_station
    elif not api_station.empty:
        station_final = api_station
    else:
        station_final = pd.DataFrame(columns=["ts_utc_start"])

    station_final = station_final.sort_values("ts_utc_start").reset_index(drop=True)
    power_final = power_final.sort_values(["source_id", "ts_utc_start"]).reset_index(drop=True)

    logger.info(
        "DKASC load complete: %d power rows, %d station rows, %d yearly/API duplicates dropped, "
        "%d API-vs-yearly conflicts, %d API-vs-API duplicates resolved",
        len(power_final),
        len(station_final),
        stats["duplicates_dropped"],
        stats["api_yearly_conflicts"],
        stats["api_api_duplicates"],
    )

    # --- Write interim ---
    interim = cfg.path("interim")
    interim.mkdir(parents=True, exist_ok=True)

    labels_path = interim / "labels_5min.parquet"
    station_path = interim / "station_5min.parquet"

    power_final.to_parquet(labels_path, index=False)
    station_final.to_parquet(station_path, index=False)

    logger.info("Wrote %s (%d rows)", labels_path.name, len(power_final))
    logger.info("Wrote %s (%d rows)", station_path.name, len(station_final))

    stats["labels_rows"] = len(power_final)
    stats["station_rows"] = len(station_final)

    return stats
