"""Fetch and load Open-Meteo Previous Runs API data.

Features & guarantees:
- fetch(cfg, start, end): fetches models configured in open_meteo.segments.
  Raw JSON saved as previous_runs_<model>_<start>_<end>.json + .meta.json.
- load(cfg): reads raw JSONs, validates models, assigns each hour to its segment
  by local date of (ts_utc_end - 1h), adds wx_source (int8) and wx_model (string).
  Interim cache per model: interim/weather_hourly_<model>.parquet.
- coverage_start(weather_df, cfg): first full local day after first complete hour of first segment.
- detect_weather_gaps(weather_df, window_start_utc, window_end_utc): gap list.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import requests

from .config import PipelineConfig

logger = logging.getLogger(__name__)

WIND_KMH_TO_MS = 1 / 3.6  # km/h → m/s


def _year_chunks(start: str, end: str) -> List[Tuple[str, str]]:
    """Split a date range into calendar-year chunks.

    Args:
        start: YYYY-MM-DD
        end: YYYY-MM-DD

    Returns:
        List of (chunk_start, chunk_end) strings.
    """
    s = datetime.strptime(start, "%Y-%m-%d").date()
    e = datetime.strptime(end, "%Y-%m-%d").date()

    chunks = []
    year = s.year
    while True:
        chunk_start = max(s, datetime(year, 1, 1).date())
        chunk_end = min(e, datetime(year, 12, 31).date())
        if chunk_start > e:
            break
        chunks.append((str(chunk_start), str(chunk_end)))
        year += 1
        if year > e.year:
            break

    return chunks


def fetch(cfg: PipelineConfig, start: Optional[str] = None, end: Optional[str] = None) -> None:
    """Fetch Open-Meteo Previous Runs data for configured segments.

    Raw JSON is saved immutably as previous_runs_<model>_<start>_<end>.json.
    Existing files are skipped.
    """
    if not cfg.om_segments:
        raise ValueError("open_meteo.segments is missing or empty in config. No implicit best_match allowed.")

    raw_dir = cfg.path(cfg.om_raw_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)

    variables_with_suffix = [f"{v}{cfg.om_lead_suffix}" for v in cfg.om_variables]
    variables_str = ",".join(variables_with_suffix)

    darwin_today = datetime.now(timezone(timedelta(hours=9, minutes=30))).date()

    for segment in cfg.om_segments:
        model = segment["model"]
        from_l = segment.get("from_local")
        to_l = segment.get("to_local")

        # Range for segment with 1 day buffer on each side
        if from_l is not None:
            s_dt = date.fromisoformat(str(from_l)) - timedelta(days=1)
        else:
            s_dt = date.fromisoformat(start) if start else date(2024, 1, 1)

        if to_l is not None:
            e_dt = date.fromisoformat(str(to_l)) + timedelta(days=1)
        else:
            e_dt = date.fromisoformat(end) + timedelta(days=1) if end else (darwin_today + timedelta(days=1))

        if start:
            req_start = date.fromisoformat(start) - timedelta(days=1)
            s_dt = max(s_dt, req_start)
        if end:
            req_end = date.fromisoformat(end) + timedelta(days=1)
            e_dt = min(e_dt, req_end)

        if s_dt > e_dt:
            logger.info("Segment %s: date range %s -> %s is empty, skipping.", model, s_dt, e_dt)
            continue

        chunks = _year_chunks(str(s_dt), str(e_dt))
        logger.info("Segment %s: fetching %d chunk(s) for %s -> %s", model, len(chunks), s_dt, e_dt)

        for chunk_start, chunk_end in chunks:
            fname = f"previous_runs_{model}_{chunk_start}_{chunk_end}.json"
            fpath = raw_dir / fname
            meta_path = raw_dir / (fname + ".meta.json")

            if fpath.exists():
                logger.info("Skipping %s (already exists)", fname)
                continue

            params = {
                "latitude": cfg.latitude,
                "longitude": cfg.longitude,
                "hourly": variables_str,
                "models": model,
                "start_date": chunk_start,
                "end_date": chunk_end,
                "timezone": "GMT",
            }

            logger.info("Fetching Open-Meteo (%s): %s -> %s", model, chunk_start, chunk_end)

            resp = None
            for attempt in range(1, 4):
                try:
                    resp = requests.get(cfg.om_endpoint, params=params, timeout=60)
                    resp.raise_for_status()
                    break
                except Exception as exc:
                    logger.warning("Attempt %d/3 failed for %s (%s): %s", attempt, model, chunk_start, exc)
                    if attempt < 3:
                        time.sleep(cfg.om_sleep_s * (2 ** attempt))
                    else:
                        raise

            fetch_time = datetime.now(timezone.utc).isoformat()

            # Save raw JSON
            with open(fpath, "w", encoding="utf-8") as fh:
                fh.write(resp.text)

            # Save meta
            meta = {
                "url": resp.url,
                "params": params,
                "model": model,
                "fetch_time_utc": fetch_time,
                "http_status": resp.status_code,
            }
            with open(meta_path, "w", encoding="utf-8") as fh:
                json.dump(meta, fh, indent=2)

            logger.info("Saved %s (HTTP %d)", fname, resp.status_code)
            time.sleep(cfg.om_sleep_s)


def _parse_one_json(fpath: Path) -> Tuple[Optional[pd.DataFrame], Optional[dict]]:
    """Parse one raw Open-Meteo JSON into a DataFrame + grid info dict."""
    with open(fpath, "r", encoding="utf-8") as fh:
        data = json.load(fh)

    hourly = data.get("hourly", {})
    times = hourly.get("time")
    if not times:
        logger.warning("No hourly time in %s", fpath.name)
        return None, None

    var_map = {
        "ghi_fc": None,
        "dni_fc": None,
        "dhi_fc": None,
        "temp_fc": None,
        "cloud_fc": None,
        "wind_fc": None,
    }

    for key in hourly:
        lk = key.lower()
        if "shortwave_radiation" in lk:
            var_map["ghi_fc"] = key
        elif "direct_normal" in lk:
            var_map["dni_fc"] = key
        elif "diffuse_radiation" in lk:
            var_map["dhi_fc"] = key
        elif "temperature_2m" in lk:
            var_map["temp_fc"] = key
        elif "cloud_cover" in lk:
            var_map["cloud_fc"] = key
        elif "wind_speed_10m" in lk:
            var_map["wind_fc"] = key

    df = pd.DataFrame({"time": pd.to_datetime(times, utc=True)})

    for std_name, api_key in var_map.items():
        if api_key and api_key in hourly:
            vals = hourly[api_key]
            arr = pd.array(vals, dtype="Float64")
            df[std_name] = arr.astype("float32")
        else:
            df[std_name] = np.nan

    # Wind: km/h → m/s
    df["wind_fc"] = df["wind_fc"] * WIND_KMH_TO_MS

    # ts_utc_end = API time (radiation is preceding-hour mean)
    df = df.rename(columns={"time": "ts_utc_end"})

    # Grid info
    grid_info = {
        "latitude": data.get("latitude"),
        "longitude": data.get("longitude"),
        "elevation": data.get("elevation"),
    }

    # Attach fetch time from sibling meta if available
    meta_path = fpath.with_suffix(".json.meta.json")
    if meta_path.exists():
        try:
            with open(meta_path, "r", encoding="utf-8") as fh:
                meta = json.load(fh)
            df["_fetch_time"] = meta.get("fetch_time_utc", "")
        except Exception:
            df["_fetch_time"] = ""
    else:
        df["_fetch_time"] = ""

    return df, grid_info


def load(cfg: PipelineConfig) -> Tuple[pd.DataFrame, Dict[str, dict], List[dict]]:
    """Read all raw JSON files per segment → hourly weather DataFrame with wx_source and wx_model.

    Returns:
        (combined_weather_df, grid_info_by_model, gap_list)
    """
    if not cfg.om_segments:
        raise ValueError("open_meteo.segments is missing or empty in config. No implicit best_match allowed.")

    import pytz
    tz = pytz.timezone(cfg.tz)

    raw_dir = cfg.path(cfg.om_raw_dir)
    # Non-recursive glob; exclude .meta.json files and directories
    json_files = [
        f for f in raw_dir.glob("previous_runs_*.json")
        if not f.name.endswith(".meta.json") and f.is_file()
    ]

    if not json_files:
        raise FileNotFoundError(
            f"No Open-Meteo raw JSON files found in {raw_dir}. "
            "Run `python -m pvdata fetch-meteo` first."
        )

    # Validate filenames and extract models
    file_info = []
    for fpath in json_files:
        m = re.match(r"^previous_runs_([a-zA-Z0-9_]+)_(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})\.json$", fpath.name)
        if not m:
            raise ValueError(f"Raw Open-Meteo file lacks model in filename: {fpath.name}")
        model_name = m.group(1)

        meta_path = fpath.with_suffix(".json.meta.json")
        if meta_path.exists():
            with open(meta_path, "r", encoding="utf-8") as fh:
                meta_data = json.load(fh)
            meta_model = meta_data.get("model") or meta_data.get("params", {}).get("models")
            if meta_model and meta_model != model_name:
                raise ValueError(
                    f"Model in filename '{model_name}' does not match meta '{meta_model}' in {fpath.name}"
                )

        file_info.append((fpath, model_name))

    interim = cfg.path("interim")
    interim.mkdir(parents=True, exist_ok=True)

    grid_info_by_model: Dict[str, dict] = {}
    model_dfs: Dict[str, pd.DataFrame] = {}

    # Load and dedupe per model
    for segment in cfg.om_segments:
        seg_model = segment["model"]
        seg_files = [fp for fp, m in file_info if m == seg_model]

        if not seg_files:
            logger.warning("No files found for model %s", seg_model)
            continue

        dfs = []
        for fp in seg_files:
            df, gi = _parse_one_json(fp)
            if df is not None:
                dfs.append(df)
                if gi and seg_model not in grid_info_by_model:
                    grid_info_by_model[seg_model] = gi

        if not dfs:
            continue

        comb = pd.concat(dfs, ignore_index=True)
        comb["_fetch_time"] = pd.to_datetime(comb["_fetch_time"], utc=True, errors="coerce")
        comb = comb.sort_values(["ts_utc_end", "_fetch_time"])
        comb = comb.drop_duplicates(subset=["ts_utc_end"], keep="last")
        comb = comb.drop(columns=["_fetch_time"])
        comb = comb.sort_values("ts_utc_end").reset_index(drop=True)

        model_dfs[seg_model] = comb

        # Save per-model interim cache
        cache_path = interim / f"weather_hourly_{seg_model}.parquet"
        comb.to_parquet(cache_path, index=False)
        logger.info("Wrote %s (%d rows)", cache_path.name, len(comb))

    # Assign each hour to its segment by local date of (ts_utc_end - 1h)
    segment_slices = []

    for segment in cfg.om_segments:
        seg_model = segment["model"]
        wx_src = np.int8(segment["wx_source"])
        from_l = segment.get("from_local")
        to_l = segment.get("to_local")

        df = model_dfs.get(seg_model)
        if df is None or df.empty:
            continue

        # Local date of ts_utc_end - 1h
        ts_utc_start = df["ts_utc_end"] - pd.Timedelta(hours=1)
        ts_local = ts_utc_start.dt.tz_convert(tz)
        local_date = ts_local.dt.date

        mask = pd.Series(True, index=df.index)
        if from_l is not None:
            mask &= (local_date >= date.fromisoformat(str(from_l)))
        if to_l is not None:
            mask &= (local_date <= date.fromisoformat(str(to_l)))

        sl = df[mask].copy()
        sl["wx_source"] = wx_src
        sl["wx_model"] = seg_model
        segment_slices.append(sl)

    if not segment_slices:
        raise ValueError("No weather data available matching configured segments.")

    combined = pd.concat(segment_slices, ignore_index=True)

    # Verify no two models mix in any hour
    if combined.duplicated(subset=["ts_utc_end"]).any():
        dups = combined[combined.duplicated(subset=["ts_utc_end"], keep=False)]
        raise ValueError(
            f"Weather segments overlap: {len(dups)} hours covered by multiple models. "
            f"Timestamps: {dups['ts_utc_end'].head(5).tolist()}"
        )

    combined = combined.sort_values("ts_utc_end").reset_index(drop=True)

    # Write combined interim
    comb_interim = interim / "weather_hourly.parquet"
    combined.to_parquet(comb_interim, index=False)
    logger.info("Wrote %s (%d rows)", comb_interim.name, len(combined))

    # Detect gaps
    gaps = detect_weather_gaps(combined)

    return combined, grid_info_by_model, gaps


def detect_weather_gaps(weather_df: pd.DataFrame, window_start_utc=None, window_end_utc=None) -> List[dict]:
    """Find contiguous gaps (any _fc variable NaN) inside the window."""
    if weather_df.empty:
        return []

    fc_cols = ["ghi_fc", "dni_fc", "dhi_fc", "temp_fc", "cloud_fc", "wind_fc"]
    avail = [c for c in fc_cols if c in weather_df.columns]
    if not avail:
        return []

    df = weather_df.sort_values("ts_utc_end").copy()
    if window_start_utc is not None:
        df = df[df["ts_utc_end"] >= window_start_utc]
    if window_end_utc is not None:
        df = df[df["ts_utc_end"] <= window_end_utc]

    if df.empty:
        return []

    is_gap = df[avail].isna().any(axis=1).to_numpy()
    gaps = []

    if not np.any(is_gap):
        return gaps

    models = df["wx_model"].to_numpy() if "wx_model" in df.columns else np.full(len(df), "unknown")
    times = pd.to_datetime(df["ts_utc_end"], utc=True).to_numpy()

    in_gap = False
    start_idx = 0

    for i, flag in enumerate(is_gap):
        if flag and not in_gap:
            in_gap = True
            start_idx = i
        elif not flag and in_gap:
            in_gap = False
            gaps.append({
                "model": str(models[start_idx]),
                "start_utc": pd.Timestamp(times[start_idx]).isoformat(),
                "end_utc": pd.Timestamp(times[i - 1]).isoformat(),
                "hours": i - start_idx,
            })
        elif in_gap and flag and (models[i] != models[start_idx]):
            # Model boundary inside gap
            gaps.append({
                "model": str(models[start_idx]),
                "start_utc": pd.Timestamp(times[start_idx]).isoformat(),
                "end_utc": pd.Timestamp(times[i - 1]).isoformat(),
                "hours": i - start_idx,
            })
            start_idx = i

    if in_gap:
        gaps.append({
            "model": str(models[start_idx]),
            "start_utc": pd.Timestamp(times[start_idx]).isoformat(),
            "end_utc": pd.Timestamp(times[-1]).isoformat(),
            "hours": len(df) - start_idx,
        })

    return gaps


def coverage_start(weather_df: pd.DataFrame, cfg: PipelineConfig) -> str:
    """Find first full local day after first complete hour of the first segment.

    Mid-series gaps must not move the window start.
    Returns local date string YYYY-MM-DD.
    """
    import pytz
    tz = pytz.timezone(cfg.tz)

    fc_cols = ["ghi_fc", "dni_fc", "dhi_fc", "temp_fc", "cloud_fc", "wind_fc"]
    available_cols = [c for c in fc_cols if c in weather_df.columns]

    df = weather_df.sort_values("ts_utc_end").copy()

    # Filter to first segment's model if segments are configured
    if cfg.om_segments and len(cfg.om_segments) > 0:
        first_model = cfg.om_segments[0]["model"]
        if "wx_model" in df.columns:
            seg_df = df[df["wx_model"] == first_model]
            if not seg_df.empty:
                df = seg_df

    complete_mask = ~df[available_cols].isna().any(axis=1)
    if not complete_mask.any():
        raise ValueError("Open-Meteo data has no complete coverage window.")

    first_complete_utc = df.loc[complete_mask, "ts_utc_end"].iloc[0]

    # Convert to local, get next full day
    first_local = first_complete_utc.astimezone(tz).date()
    next_day = first_local + timedelta(days=1)

    logger.info(
        "Open-Meteo coverage starts at %s UTC → first full local day = %s",
        first_complete_utc,
        next_day,
    )
    return str(next_day)
