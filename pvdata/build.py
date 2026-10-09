"""Orchestrate a versioned build of the PV training table.

Pipeline steps:
- Load config, resolve window.
- Run io_dkasc → qc → aggregate → io_openmeteo → features → physics → pr.
- Join into train_table (with 40 columns total).
- Write processed/<version>/ with all output files + manifest.json + qc_report.md
  + feature_spec.json + recipes.json.
- Refuse to overwrite existing version unless --overwrite.
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import pvlib

from .config import PipelineConfig, load_config
from .io_dkasc import load_dkasc
from .io_openmeteo import load as load_openmeteo, coverage_start, detect_weather_gaps
from .metadata import load_metadata
from .qc import apply_qc
from .aggregate import aggregate_labels, aggregate_station
from .features import (
    compute_solar_position,
    compute_kt_fc,
    compute_array_features,
    compute_cyclical_time_features,
    encode_categories,
    TECHNOLOGY_CODES,
    TRACKING_CODES,
)
from .physics import compute_p_phys_norm
from .pr import compute_pr_features
from .qc_daily import apply_dead_day_qc, DeadDaySummary

logger = logging.getLogger(__name__)

# Final train_table column order (existing columns unchanged + new appended columns)
TRAIN_TABLE_COLUMNS = [
    "source_id", "ts_utc_end", "ts_local_start", "array_no", "kwp", "technology",
    "tracking", "tilt_deg", "surface_azimuth_pvlib",
    "ghi_fc", "dni_fc", "dhi_fc", "temp_fc", "cloud_fc", "wind_fc",
    "solar_zenith", "solar_azimuth", "ghi_clear", "kt_fc",
    "poa_fc", "temp_cell_fc", "is_daylight",
    "power_kw_mean", "coverage", "qc_flags",
    "target_kw", "target_norm",
    "time_split", "array_split",
    # Engineered features and encoded categorical columns
    "wx_source", "wx_model", "technology_code", "tracking_code",
    "p_phys_norm", "hour_sin", "hour_cos", "doy_sin", "doy_cos",
    "pr_7d", "pr_30d",
]

COLUMNS_DATA_DICTIONARY: Dict[str, Dict[str, str]] = {
    "source_id": {"unit": "—", "description": "Unique DKASC array identifier"},
    "ts_utc_end": {"unit": "UTC", "description": "UTC hour-ending timestamp"},
    "ts_local_start": {"unit": "ACST", "description": "Local start of the hour interval (UTC+09:30)"},
    "array_no": {"unit": "—", "description": "Array identifier in DKASC documentation"},
    "kwp": {"unit": "kWp", "description": "Rated peak DC power of the array"},
    "technology": {"unit": "—", "description": "PV cell technology name"},
    "tracking": {"unit": "—", "description": "Mounting/tracking configuration"},
    "tilt_deg": {"unit": "deg", "description": "Array tilt angle from horizontal"},
    "surface_azimuth_pvlib": {"unit": "deg", "description": "Array surface azimuth in pvlib convention (0=N, 90=E, 180=S, 270=W)"},
    "ghi_fc": {"unit": "W/m²", "description": "Forecast global horizontal irradiance (day-ahead)"},
    "dni_fc": {"unit": "W/m²", "description": "Forecast direct normal irradiance (day-ahead)"},
    "dhi_fc": {"unit": "W/m²", "description": "Forecast diffuse horizontal irradiance (day-ahead)"},
    "temp_fc": {"unit": "°C", "description": "Forecast 2m air temperature (day-ahead)"},
    "cloud_fc": {"unit": "%", "description": "Forecast total cloud cover (day-ahead)"},
    "wind_fc": {"unit": "m/s", "description": "Forecast 10m wind speed (day-ahead)"},
    "solar_zenith": {"unit": "deg", "description": "Solar zenith angle at interval midpoint"},
    "solar_azimuth": {"unit": "deg", "description": "Solar azimuth angle at interval midpoint"},
    "ghi_clear": {"unit": "W/m²", "description": "Clear-sky GHI (Ineichen model) at interval midpoint"},
    "kt_fc": {"unit": "—", "description": "Forecast clearness index ghi_fc / ghi_clear"},
    "poa_fc": {"unit": "W/m²", "description": "Forecast plane-of-array irradiance (Hay-Davies model)"},
    "temp_cell_fc": {"unit": "°C", "description": "Forecast PV cell temperature (SAPM model)"},
    "is_daylight": {"unit": "bool", "description": "True when solar zenith < 90 deg"},
    "power_kw_mean": {"unit": "kW", "description": "Mean active power over valid 5-min records in the hour"},
    "coverage": {"unit": "—", "description": "Fraction of valid 5-min records in the hour"},
    "qc_flags": {"unit": "—", "description": "Union of 5-min QC flags in the hour"},
    "target_kw": {"unit": "kW", "description": "Target active power for training (NaN if invalid/excluded)"},
    "target_norm": {"unit": "—", "description": "Normalized target power target_kw / kwp"},
    "time_split": {"unit": "—", "description": "Time split label (unassigned, recipes applied by loader)"},
    "array_split": {"unit": "—", "description": "Array split label (train, coldstart, or excluded)"},
    "wx_source": {"unit": "int", "description": "Weather data source indicator (0=ICON, 1=ECMWF)"},
    "wx_model": {"unit": "string", "description": "Numerical weather prediction model name (provenance only)"},
    "technology_code": {"unit": "int", "description": "Encoded integer for PV cell technology"},
    "tracking_code": {"unit": "int", "description": "Encoded integer for tracking configuration"},
    "p_phys_norm": {"unit": "kW/kWp", "description": "Physics baseline normalized power output"},
    "hour_sin": {"unit": "—", "description": "Sine of local time of day"},
    "hour_cos": {"unit": "—", "description": "Cosine of local time of day"},
    "doy_sin": {"unit": "—", "description": "Sine of local day of year"},
    "doy_cos": {"unit": "—", "description": "Cosine of local day of year"},
    "pr_7d": {"unit": "—", "description": "7-day rolling performance ratio (past days [D-7, D-1])"},
    "pr_30d": {"unit": "—", "description": "30-day rolling performance ratio (past days [D-30, D-1])"},
}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _git_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return "unknown"


def _resolve_window(cfg: PipelineConfig, weather_df: pd.DataFrame, end_override: Optional[str] = None) -> Tuple[str, str]:
    """Resolve window start/end dates.

    Returns (start_local, end_local) as YYYY-MM-DD strings.
    """
    end = end_override or cfg.window_end

    if cfg.window_start.lower() == "auto":
        start = coverage_start(weather_df, cfg)
        logger.info("Auto window start: %s (from Open-Meteo coverage)", start)
    else:
        start = cfg.window_start

    return start, end


def build(
    version: str,
    cfg: PipelineConfig,
    end_override: Optional[str] = None,
    force: bool = False,
    overwrite: bool = False,
) -> None:
    """Run the full build pipeline.

    Args:
        version: Version string e.g. "v0".
        cfg: Pipeline configuration.
        end_override: Override window end date (local YYYY-MM-DD).
        force: Force rebuild of interim files.
        overwrite: Allow overwriting existing version folder.
    """
    out_dir = cfg.path("processed") / version
    if out_dir.exists() and not overwrite:
        raise FileExistsError(
            f"Version folder already exists: {out_dir}. "
            "Use --overwrite to replace it."
        )
    out_dir.mkdir(parents=True, exist_ok=True)

    # ---- Step 1: Load DKASC ----
    labels_interim = cfg.path("interim") / "labels_5min.parquet"
    station_interim = cfg.path("interim") / "station_5min.parquet"
    sig_path = cfg.path("interim") / "labels_5min.inputs.json"
    signature = _dkasc_input_signature(cfg)

    cached = None
    if sig_path.exists() and labels_interim.exists() and station_interim.exists():
        try:
            cached = json.loads(sig_path.read_text(encoding="utf-8"))
        except Exception:
            cached = None

    if force or cached is None or cached.get("signature") != signature:
        logger.info("=== Loading DKASC data (inputs changed or no cache) ===")
        dkasc_stats = load_dkasc(cfg)
        sig_path.write_text(json.dumps({"signature": signature, "stats": dkasc_stats},
                                       indent=2, default=str), encoding="utf-8")
    else:
        logger.info("Reusing %s (inputs unchanged)", labels_interim.name)
        dkasc_stats = cached.get("stats", {})

    labels_5min = pd.read_parquet(labels_interim)
    station_5min = pd.read_parquet(station_interim)

    # ---- Step 2: Metadata ----
    logger.info("=== Loading metadata ===")
    arrays_meta = load_metadata(cfg)

    # ---- Step 3: Open-Meteo ----
    logger.info("=== Loading Open-Meteo (from raw JSON) ===")
    weather_df, grid_info_by_model, gaps = load_openmeteo(cfg)

    # ---- Resolve window ----
    window_start, window_end = _resolve_window(cfg, weather_df, end_override)
    import pytz
    tz = pytz.timezone(cfg.tz)
    window_start_utc = pd.Timestamp(window_start, tz=tz).tz_convert("UTC")
    window_end_utc = pd.Timestamp(window_end, tz=tz) + pd.Timedelta(hours=23, minutes=59, seconds=59)
    window_end_utc = window_end_utc.tz_convert("UTC")

    logger.info("Window: %s → %s (local)", window_start, window_end)

    # Filter gaps strictly inside window
    window_gaps = [
        g for g in gaps
        if pd.Timestamp(g["end_utc"], tz="UTC") >= window_start_utc
        and pd.Timestamp(g["start_utc"], tz="UTC") <= window_end_utc
    ]

    # ---- Step 4: Solar position for QC ----
    logger.info("=== Computing solar position ===")
    ts_5min = labels_5min["ts_utc_start"].drop_duplicates().sort_values().reset_index(drop=True)
    solar_5min = compute_solar_position(ts_5min + pd.Timedelta(seconds=1950), cfg)
    solar_5min["ts_utc_end"] = pd.DatetimeIndex(ts_5min)
    solar_5min = solar_5min.rename(columns={"ts_utc_end": "ts_utc_start"})

    # ---- Step 5: QC ----
    logger.info("=== Applying QC ===")
    labels_5min_qc = apply_qc(labels_5min, arrays_meta, station_5min, solar_5min, cfg)
    del labels_5min, solar_5min, ts_5min
    import gc; gc.collect()

    # ---- Step 6: Aggregate ----
    logger.info("=== Aggregating to hourly ===")
    labels_hourly = aggregate_labels(labels_5min_qc, arrays_meta, cfg)
    station_hourly = aggregate_station(station_5min, cfg)

    # ---- Step 7: Solar position at hourly midpoint ----
    ts_hourly = labels_hourly["ts_utc_end"].drop_duplicates().sort_values()
    solar_hourly = compute_solar_position(ts_hourly, cfg)

    # ---- Step 8: Join weather forecast ----
    logger.info("=== Joining weather forecast ===")
    weather_df_w = weather_df.copy()
    if weather_df_w["ts_utc_end"].dt.tz is None:
        weather_df_w["ts_utc_end"] = weather_df_w["ts_utc_end"].dt.tz_localize("UTC")

    weather_window = weather_df_w[
        (weather_df_w["ts_utc_end"] >= window_start_utc) &
        (weather_df_w["ts_utc_end"] <= window_end_utc)
    ]

    if labels_hourly["ts_utc_end"].dt.tz is None:
        labels_hourly["ts_utc_end"] = labels_hourly["ts_utc_end"].dt.tz_localize("UTC")
    labels_window = labels_hourly[
        (labels_hourly["ts_utc_end"] >= window_start_utc) &
        (labels_hourly["ts_utc_end"] <= window_end_utc)
    ].copy()

    # Merge solar position
    if solar_hourly["ts_utc_end"].dt.tz is None:
        solar_hourly["ts_utc_end"] = solar_hourly["ts_utc_end"].dt.tz_localize("UTC")
    labels_window = labels_window.merge(solar_hourly, on="ts_utc_end", how="left")

    # Merge weather forecast
    weather_fc_cols = [
        "ts_utc_end", "ghi_fc", "dni_fc", "dhi_fc", "temp_fc", "cloud_fc", "wind_fc",
        "wx_source", "wx_model",
    ]
    weather_fc_cols = [c for c in weather_fc_cols if c in weather_window.columns]
    labels_window = labels_window.merge(
        weather_window[weather_fc_cols], on="ts_utc_end", how="left"
    )

    # Compute kt_fc
    labels_window = compute_kt_fc(labels_window)

    # ---- Step 9: Per-array POA and cell temp ----
    logger.info("=== Computing per-array features (POA, cell temp) ===")
    arr_feats = compute_array_features(labels_window, arrays_meta, cfg)
    labels_window = labels_window.merge(arr_feats, on=["source_id", "ts_utc_end"], how="left")
    if "poa_fc" not in labels_window.columns:
        labels_window["poa_fc"] = np.nan
        labels_window["temp_cell_fc"] = np.nan

    # ---- Step 10: Join array static metadata ----
    meta_cols = ["source_id", "array_no", "kwp", "technology", "tracking",
                 "tilt_deg", "surface_azimuth_pvlib"]
    meta_cols = [c for c in meta_cols if c in arrays_meta.columns]
    train_table = labels_window.merge(
        arrays_meta[meta_cols],
        on="source_id",
        how="left",
        suffixes=("", "_meta"),
    )

    # ---- Step 11: Physics, Split Columns, Dead-Day QC, and PR Features ----
    logger.info("=== Computing physics baseline (p_phys_norm) ===")
    train_table["p_phys_norm"] = compute_p_phys_norm(
        poa_fc=train_table["poa_fc"],
        temp_cell_fc=train_table["temp_cell_fc"],
        technology=train_table["technology"],
        cfg=cfg,
    )

    # Compute array_split and time_split before dead-day QC
    coldstart_set = set(cfg.coldstart_source_ids)
    excluded_set = set(arrays_meta.loc[arrays_meta["excluded"].fillna(False).astype(bool), "source_id"])

    def _get_array_split(sid: int) -> str:
        if sid in coldstart_set:
            return "coldstart"
        if sid in excluded_set:
            return "excluded"
        return "train"

    train_table["array_split"] = train_table["source_id"].map(_get_array_split)
    train_table["time_split"] = "unassigned"

    logger.info("=== Applying daily QC (dead days) ===")
    train_table, dead_days_summary = apply_dead_day_qc(train_table, cfg)

    logger.info("=== Computing categorical codes and cyclical time features ===")
    cats = encode_categories(train_table)
    train_table["technology_code"] = cats["technology_code"]
    train_table["tracking_code"] = cats["tracking_code"]

    cyc = compute_cyclical_time_features(train_table["ts_local_start"])
    train_table["hour_sin"] = cyc["hour_sin"]
    train_table["hour_cos"] = cyc["hour_cos"]
    train_table["doy_sin"] = cyc["doy_sin"]
    train_table["doy_cos"] = cyc["doy_cos"]

    logger.info("=== Computing performance ratio features (pr_7d, pr_30d) ===")
    pr_df = compute_pr_features(train_table, cfg)
    train_table["pr_7d"] = pr_df["pr_7d"]
    train_table["pr_30d"] = pr_df["pr_30d"]

    # ---- Reorder columns ----
    final_cols = [c for c in TRAIN_TABLE_COLUMNS if c in train_table.columns]
    extra_cols = [c for c in train_table.columns if c not in TRAIN_TABLE_COLUMNS]
    if extra_cols:
        logger.debug("Extra columns not in spec (dropping): %s", extra_cols)
    train_table = train_table[final_cols]

    # ---- Write outputs ----
    logger.info("=== Writing outputs to %s ===", out_dir)

    # labels_hourly
    lh_path = out_dir / "labels_hourly.parquet"
    labels_hourly.to_parquet(lh_path, index=False)

    # weather_hourly
    wh_path = out_dir / "weather_hourly.parquet"
    weather_df.to_parquet(wh_path, index=False)

    # station_hourly
    sh_path = out_dir / "station_hourly.parquet"
    station_hourly.to_parquet(sh_path, index=False)

    # arrays
    arr_path = out_dir / "arrays.parquet"
    arrays_meta.to_parquet(arr_path, index=False)

    # train_table
    tt_path = out_dir / "train_table.parquet"
    train_table.to_parquet(tt_path, index=False)

    logger.info("train_table: %d rows × %d columns", len(train_table), len(train_table.columns))

    # ---- Feature Spec & Recipes ----
    feature_spec = {
        "version": version,
        "target": "target_norm",
        "unnormalize_by": "kwp",
        "id_columns": ["source_id", "ts_utc_end", "ts_local_start"],
        "row_filter": "target_norm.notna() & ghi_clear > 0 & ghi_fc.notna()",
        "feature_groups": {
            "base": [
                "ghi_fc", "dni_fc", "dhi_fc", "temp_fc", "cloud_fc", "wind_fc", "wx_source",
                "solar_zenith", "solar_azimuth", "ghi_clear", "kt_fc", "poa_fc", "temp_cell_fc",
                "p_phys_norm", "kwp", "technology_code", "tracking_code", "tilt_deg",
                "surface_azimuth_pvlib", "hour_sin", "hour_cos", "doy_sin", "doy_cos"
            ],
            "pr": ["pr_7d", "pr_30d"],
        },
        "default_groups": ["base"],
        "categorical": {
            "technology_code": TECHNOLOGY_CODES,
            "tracking_code": TRACKING_CODES,
        },
        "banned": [
            "power_kw_mean", "coverage", "qc_flags", "target_kw", "target_norm", "array_no",
            "source_id", "wx_model", "is_daylight", "time_split", "array_split"
        ],
        "columns": COLUMNS_DATA_DICTIONARY,
    }

    spec_path = out_dir / "feature_spec.json"
    with open(spec_path, "w", encoding="utf-8") as fh:
        json.dump(feature_spec, fh, indent=2, default=str)
    logger.info("Wrote feature_spec.json")

    recipes_path = out_dir / "recipes.json"
    with open(recipes_path, "w", encoding="utf-8") as fh:
        json.dump(cfg.recipes, fh, indent=2, default=str)
    logger.info("Wrote recipes.json")

    # ---- Weather segments info for manifest ----
    weather_segments_summary = []
    if cfg.om_segments:
        for seg in cfg.om_segments:
            m = seg["model"]
            seg_rows = weather_df[weather_df.get("wx_model", "") == m]
            weather_segments_summary.append({
                "model": m,
                "wx_source": seg.get("wx_source"),
                "from_local": seg.get("from_local"),
                "to_local": seg.get("to_local"),
                "first_hour_present": seg_rows["ts_utc_end"].min().isoformat() if not seg_rows.empty else None,
                "last_hour_present": seg_rows["ts_utc_end"].max().isoformat() if not seg_rows.empty else None,
                "gaps": [g for g in window_gaps if g.get("model") == m],
            })

    # ---- Manifest ----
    input_files = []
    for fname in cfg.yearly_files:
        fp = cfg.path(fname)
        if fp.exists():
            input_files.append({
                "name": fname,
                "size": fp.stat().st_size,
                "sha256": _sha256(fp),
            })

    manifest = {
        "version": version,
        "build_time_utc": datetime.now(timezone.utc).isoformat(),
        "window": {"start": window_start, "end": window_end},
        "config": cfg.as_dict(),
        "input_files": input_files,
        "open_meteo_grid": grid_info_by_model,
        "weather_segments": weather_segments_summary,
        "weather_gaps": window_gaps,
        "dead_days": dead_days_summary.dead_days_dict,
        "row_counts": {
            "labels_hourly": len(labels_hourly),
            "weather_hourly": len(weather_df),
            "station_hourly": len(station_hourly),
            "arrays": len(arrays_meta),
            "train_table": len(train_table),
        },
        "pipeline_git_commit": _git_commit(),
    }

    manifest_path = out_dir / "manifest.json"
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, default=str)
    logger.info("Wrote manifest.json")

    # ---- QC Report ----
    qc_report = _generate_qc_report(
        version=version,
        train_table=train_table,
        arrays_meta=arrays_meta,
        dkasc_stats=dkasc_stats,
        window_start=window_start,
        window_end=window_end,
        window_gaps=window_gaps,
        dead_days_summary=dead_days_summary,
    )
    qc_path = out_dir / "qc_report.md"
    with open(qc_path, "w", encoding="utf-8") as fh:
        fh.write(qc_report)
    logger.info("Wrote qc_report.md")

    logger.info("=== Build complete: %s ===", version)


def _dkasc_input_signature(cfg: PipelineConfig) -> list:
    """Name/size/mtime of every file that feeds labels_5min (cache key)."""
    files = [cfg.path(f) for f in cfg.yearly_files]
    api_dir = cfg.path(cfg.api_dir)
    if api_dir.exists():
        files += sorted(api_dir.glob("*.csv"))
    files.append(cfg.path(cfg.metadata_file))  # defines which source_ids are loaded
    sig = []
    for f in files:
        if f.exists():
            st = f.stat()
            sig.append([f.name, st.st_size, int(st.st_mtime)])
    return sig


def _generate_qc_report(
    version: str,
    train_table: pd.DataFrame,
    arrays_meta: pd.DataFrame,
    dkasc_stats: dict,
    window_start: str,
    window_end: str,
    window_gaps: Optional[list] = None,
    dead_days_summary: Optional[DeadDaySummary] = None,
) -> str:
    """Generate initial QC report content (validate.py will append to this)."""
    lines = [
        f"# QC Report — {version}",
        "",
        f"**Window:** {window_start} → {window_end}",
        f"**Generated:** {datetime.now(timezone.utc).isoformat()}",
        "",
        "## DKASC Load Stats",
        "",
    ]

    labels_map = {
        "rows_read": "Rows read",
        "duplicates_dropped": "Duplicates dropped (yearly vs API)",
        "api_yearly_conflicts": "API-vs-yearly conflicts (>0.01 kW)",
        "api_api_duplicates": "API-vs-API duplicates resolved (kept latest mtime)",
        "labels_rows": "5-min label rows written",
        "station_rows": "5-min station rows written",
    }

    for k, v in dkasc_stats.items():
        if k == "bad_lines":
            lines.append(f"- **Bad lines:** {len(v)}")
            for bl in v:
                lines.append(f"  - File `{bl['file']}`, line {bl['line']}: {bl['fields']} fields, snippet: `{bl['snippet']}`")
        else:
            label = labels_map.get(k, k)
            lines.append(f"- **{label}:** {v}")

    lines += [
        "",
        "## Array Summary",
        "",
        f"- Total arrays: {len(arrays_meta)}",
        f"- Excluded arrays: {arrays_meta['excluded'].sum() if 'excluded' in arrays_meta else 'N/A'}",
        f"- Metadata complete: {arrays_meta['metadata_complete'].sum() if 'metadata_complete' in arrays_meta else 'N/A'}",
        "",
        "## train_table Summary",
        "",
        f"- Rows: {len(train_table)}",
        f"- Arrays: {train_table['source_id'].nunique() if 'source_id' in train_table else 'N/A'}",
        f"- target_kw NaN rate: {train_table['target_kw'].isna().mean():.1%}" if "target_kw" in train_table else "",
        "",
        "## Weather Gaps",
        "",
    ]

    if window_gaps:
        lines.append(f"Found {len(window_gaps)} gap(s) inside window:")
        for g in window_gaps:
            lines.append(f"- Model `{g['model']}`: {g['start_utc']} → {g['end_utc']} ({g['hours']} hours)")
    else:
        lines.append("No weather gaps inside the window. ✅")

    if dead_days_summary is not None:
        lines += [
            "",
            "## Dead Days",
            "",
            f"- **Total dead array-days:** {dead_days_summary.total_dead_days}",
            f"- **Hourly rows affected:** {dead_days_summary.hourly_rows_affected} ({dead_days_summary.usable_rows_removed} usable rows removed)",
            "",
        ]
        if not dead_days_summary.by_array.empty:
            cols = ["source_id", "array_no", "days", "first", "last"]
            header = ["source_id", "array_no", "days", "first", "last"]
            lines.append("| " + " | ".join(header) + " |")
            lines.append("| " + " | ".join(["---"] * len(header)) + " |")
            for _, row in dead_days_summary.by_array.iterrows():
                r_vals = [str(row[c]) for c in cols]
                lines.append("| " + " | ".join(r_vals) + " |")
        else:
            lines.append("No dead days detected. ✅")

    lines += [
        "",
        "---",
        "",
        "*(validate.py will append hard and soft check results below)*",
        "",
    ]

    return "\n".join(lines)
