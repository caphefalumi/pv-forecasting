"""Validate a versioned build.

Dataset snapshot validation:
Hard checks (exit non-zero on failure):
1. train_table key (source_id, ts_utc_end) unique.
2. No unauthorized (station-derived) columns in train_table.
3. Weather variables use lead suffix '_previous_day1' (checked via manifest).
4. Time alignment: shift 0 is best among -2..+2h and in half-hour table (r & RMSE printed, no threshold).
5. Window inside segment models coverage.
6. Every row's wx_source/wx_model matches its segment; no other model present.
7. feature_spec.json (when present): all features exist, none banned, dtypes numeric.

Soft checks (report only):
- Weather gaps list.
- Per-array correlation p_phys_norm vs target_norm (flag r < 0.8).
- Rows per array per month.
- Arrays excluded and why.
- Azimuth sanity (east/west peak hour).
- kWp sanity (daily yield 3-7 kWh/kWp/day).
- target_norm distribution.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import List, Optional

import numpy as np
import pandas as pd

from .config import PipelineConfig

logger = logging.getLogger(__name__)

# Columns allowed in train_table (no station-derived)
ALLOWED_TRAIN_TABLE_COLS = {
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
}


def validate(version: str, cfg: PipelineConfig) -> int:
    """Run all validation checks. Returns 0 on success, 1 on hard failure."""
    out_dir = cfg.path("processed") / version
    qc_path = out_dir / "qc_report.md"
    manifest_path = out_dir / "manifest.json"

    if not out_dir.exists():
        logger.error("Version folder not found: %s", out_dir)
        return 1

    # Load outputs
    tt_path = out_dir / "train_table.parquet"
    station_path = out_dir / "station_hourly.parquet"
    weather_path = out_dir / "weather_hourly.parquet"

    train_table = pd.read_parquet(tt_path)
    station_hourly = pd.read_parquet(station_path) if station_path.exists() else pd.DataFrame()
    weather_hourly = pd.read_parquet(weather_path) if weather_path.exists() else pd.DataFrame()
    arrays_meta = pd.read_parquet(out_dir / "arrays.parquet") if (out_dir / "arrays.parquet").exists() else pd.DataFrame()

    with open(manifest_path, "r", encoding="utf-8") as fh:
        manifest = json.load(fh)

    hard_failures: List[str] = []
    report_lines: List[str] = [
        "",
        "## Validation Results",
        "",
        f"**Validated:** {datetime.now(timezone.utc).isoformat()}",
        "",
        "### Hard Checks",
        "",
    ]

    # ---- Hard Check 1: Unique key ----
    key_cols = ["source_id", "ts_utc_end"]
    available_keys = [c for c in key_cols if c in train_table.columns]
    if len(available_keys) == 2:
        dupes = train_table.duplicated(subset=available_keys)
        n_dupes = dupes.sum()
        if n_dupes > 0:
            msg = f"FAIL: train_table has {n_dupes} duplicate (source_id, ts_utc_end) keys"
            hard_failures.append(msg)
            report_lines.append(f"- ❌ {msg}")
        else:
            report_lines.append("- ✅ train_table key (source_id, ts_utc_end) is unique")
    else:
        hard_failures.append("FAIL: key columns missing from train_table")
        report_lines.append("- ❌ Key columns missing from train_table")

    # ---- Hard Check 2: No station columns ----
    station_cols_in_tt = set(train_table.columns) - ALLOWED_TRAIN_TABLE_COLS
    if station_cols_in_tt:
        msg = f"FAIL: train_table has unauthorized columns: {sorted(station_cols_in_tt)}"
        hard_failures.append(msg)
        report_lines.append(f"- ❌ {msg}")
    else:
        report_lines.append("- ✅ No unauthorized (station-derived) columns in train_table")

    # ---- Hard Check 3: Weather columns from _previous_day1 ----
    suffix = manifest.get("config", {}).get("open_meteo", {}).get("lead_suffix", "_previous_day1")
    if suffix == "_previous_day1":
        report_lines.append(f"- ✅ Weather variables use lead suffix '{suffix}' (verified from manifest)")
    else:
        msg = f"FAIL: Weather lead_suffix is '{suffix}', expected '_previous_day1'"
        hard_failures.append(msg)
        report_lines.append(f"- ❌ {msg}")

    # ---- Hard Check 4: Time alignment (D1: shift 0 is best, no threshold) ----
    alignment_result = _check_time_alignment(train_table, station_hourly, report_lines)
    st5_path = cfg.path("interim") / "station_5min.parquet"
    half_hour_ok = True
    if st5_path.exists():
        half_hour_ok = _half_hour_alignment_table(pd.read_parquet(st5_path), weather_hourly, report_lines)
    if alignment_result is False or half_hour_ok is False:
        hard_failures.append("FAIL: Time alignment check failed (shift 0 not best)")

    # ---- Hard Check 5: Window inside segment model coverage ----
    window = manifest.get("window", {})
    w_start = window.get("start")
    w_end = window.get("end")
    if not weather_hourly.empty and "ts_utc_end" in weather_hourly.columns:
        wh_ts = pd.to_datetime(weather_hourly["ts_utc_end"], utc=True)
        om_min = wh_ts.min()
        om_max = wh_ts.max()
        import pytz
        try:
            site_tz = pytz.timezone(manifest.get("config", {}).get("site", {}).get("tz", "Australia/Darwin"))
            ws_utc = pd.Timestamp(w_start, tz=site_tz).tz_convert("UTC")
            we_utc = pd.Timestamp(w_end, tz=site_tz).tz_convert("UTC")
            if ws_utc < om_min or we_utc > om_max:
                msg = f"FAIL: Window [{w_start}, {w_end}] partially outside weather coverage [{om_min}, {om_max}]"
                hard_failures.append(msg)
                report_lines.append(f"- ❌ {msg}")
            else:
                report_lines.append(f"- ✅ Window inside weather coverage ({om_min.date()} → {om_max.date()})")
        except Exception as exc:
            report_lines.append(f"- ⚠️ Could not verify window vs coverage: {exc}")
    else:
        report_lines.append("- ⚠️ weather_hourly not available for window check")

    # ---- Hard Check 6: Segment model matching ----
    segments = manifest.get("config", {}).get("open_meteo", {}).get("segments", [])
    if segments and "wx_source" in train_table.columns and "wx_model" in train_table.columns:
        seg_match_ok = True
        import pytz
        site_tz = pytz.timezone(cfg.tz)
        local_dates = pd.to_datetime(train_table["ts_local_start"]).dt.date
        valid_models = {seg["model"] for seg in segments}

        # Check for unauthorized models
        tt_models = set(train_table["wx_model"].unique())
        unauth_models = tt_models - valid_models
        if unauth_models:
            msg = f"FAIL: train_table has unauthorized weather models: {unauth_models}"
            hard_failures.append(msg)
            report_lines.append(f"- ❌ {msg}")
            seg_match_ok = False

        for seg in segments:
            m = seg["model"]
            src = seg["wx_source"]
            f_l = date.fromisoformat(str(seg["from_local"])) if seg.get("from_local") else None
            t_l = date.fromisoformat(str(seg["to_local"])) if seg.get("to_local") else None

            mask = pd.Series(True, index=train_table.index)
            if f_l:
                mask &= (local_dates >= f_l)
            if t_l:
                mask &= (local_dates <= t_l)

            sub = train_table[mask]
            if not sub.empty:
                wrong_m = (sub["wx_model"] != m).sum()
                wrong_s = (sub["wx_source"] != src).sum()
                if wrong_m > 0 or wrong_s > 0:
                    msg = f"FAIL: Segment {m} has {wrong_m} mismatched models and {wrong_s} mismatched sources"
                    hard_failures.append(msg)
                    report_lines.append(f"- ❌ {msg}")
                    seg_match_ok = False

        if seg_match_ok:
            report_lines.append("- ✅ Every row's wx_source and wx_model matches its segment; no other model present")
    elif segments:
        hard_failures.append("FAIL: wx_source or wx_model missing from train_table")
        report_lines.append("- ❌ wx_source or wx_model missing from train_table")

    # ---- Hard Check 7: feature_spec.json validation ----
    spec_path = out_dir / "feature_spec.json"
    if spec_path.exists():
        try:
            with open(spec_path, "r", encoding="utf-8") as fh:
                spec = json.load(fh)
            spec_ok = True
            banned = set(spec.get("banned", []))
            groups = spec.get("feature_groups", {})

            for g_name, f_cols in groups.items():
                for c in f_cols:
                    if c not in train_table.columns:
                        hard_failures.append(f"FAIL: Feature '{c}' from group '{g_name}' missing in train_table")
                        spec_ok = False
                    if c in banned:
                        hard_failures.append(f"FAIL: Feature '{c}' is in banned list!")
                        spec_ok = False
                    if c in train_table.columns and not pd.api.types.is_numeric_dtype(train_table[c]):
                        hard_failures.append(f"FAIL: Feature '{c}' dtype is not numeric ({train_table[c].dtype})")
                        spec_ok = False

            if spec_ok:
                report_lines.append("- ✅ feature_spec.json valid (all features exist, none banned, numeric dtypes)")
            else:
                report_lines.append("- ❌ feature_spec.json validation failed (see errors above)")
        except Exception as exc:
            hard_failures.append(f"FAIL: Could not validate feature_spec.json: {exc}")
            report_lines.append(f"- ❌ feature_spec.json error: {exc}")

    # ---- Soft Checks ----
    report_lines += [
        "",
        "### Soft Checks",
        "",
    ]

    _soft_checks(train_table, arrays_meta, manifest, report_lines)

    # ---- Write results ----
    existing = qc_path.read_text(encoding="utf-8") if qc_path.exists() else ""
    marker = "\n## Validation Results"
    if marker in existing:
        existing = existing[: existing.index(marker)] + "\n"
    with open(qc_path, "w", encoding="utf-8") as fh:
        fh.write(existing)
        fh.write("\n".join(report_lines))
        fh.write("\n")

    if hard_failures:
        logger.error("=== VALIDATION FAILED ===")
        for f in hard_failures:
            logger.error("  %s", f)
        return 1

    logger.info("=== VALIDATION PASSED ===")
    return 0


def _check_time_alignment(
    train_table: pd.DataFrame,
    station_hourly: pd.DataFrame,
    report_lines: list[str],
) -> bool:
    """Check time alignment: shift 0 must be best among -2..+2h (D1: no r threshold)."""
    if station_hourly.empty or "ghi_obs" not in station_hourly.columns:
        report_lines.append("- ⚠️ station_hourly missing ghi_obs — time alignment check skipped")
        return True

    if "ghi_fc" not in train_table.columns:
        report_lines.append("- ⚠️ ghi_fc missing from train_table — time alignment check skipped")
        return True

    station_ts = pd.to_datetime(station_hourly["ts_utc_end"], utc=True) if "ts_utc_end" in station_hourly.columns else None
    if station_ts is None:
        report_lines.append("- ⚠️ station_hourly missing ts_utc_end — time alignment check skipped")
        return True

    obs = station_hourly.set_index(station_ts)["ghi_obs"].dropna()

    if "is_daylight" in train_table.columns:
        tt_day = train_table[train_table["is_daylight"]].drop_duplicates(subset=["ts_utc_end"])
    else:
        tt_day = train_table.drop_duplicates(subset=["ts_utc_end"])

    fc_ts = pd.to_datetime(tt_day["ts_utc_end"], utc=True)
    fc = pd.Series(tt_day["ghi_fc"].values, index=fc_ts).dropna()

    report_lines.append("")
    report_lines.append("#### Time Alignment (ghi_obs vs ghi_fc, daylight hours)")
    report_lines.append("")
    report_lines.append("| Shift (h) | r | RMSE (W/m²) |")
    report_lines.append("|-----------|---|-------------|")

    best_r = -999.0
    best_shift = None
    shift_results = {}

    for shift in [-2, -1, 0, 1, 2]:
        obs_shifted = obs.copy()
        if shift != 0:
            obs_shifted.index = obs_shifted.index + pd.Timedelta(hours=shift)

        common = obs_shifted.index.intersection(fc.index)
        if len(common) < 100:
            report_lines.append(f"| {shift:+d} | N/A (n={len(common)}) | N/A |")
            continue

        o = obs_shifted.loc[common]
        f = fc.loc[common]
        r = float(np.corrcoef(o.values, f.values)[0, 1])
        rmse = float(np.sqrt(np.mean((o.values - f.values) ** 2)))
        report_lines.append(f"| {shift:+d} | {r:.4f} | {rmse:.1f} |")
        shift_results[shift] = (r, rmse)

        if r > best_r:
            best_r = r
            best_shift = shift

    report_lines.append("")

    # D1 decision: shift 0 must be best among -2..+2h. r & RMSE printed, no threshold.
    if best_shift == 0:
        report_lines.append(f"- ✅ Alignment OK: shift 0 is best (r={best_r:.4f}, RMSE={shift_results[0][1]:.1f} W/m²)")
        return True
    else:
        report_lines.append(
            f"- ❌ Alignment FAILED: best shift is {best_shift:+d}h (r={best_r:.4f}), not 0. "
            "Do NOT fix silently — stop and investigate."
        )
        return False


def _half_hour_alignment_table(
    station_5min: pd.DataFrame,
    weather_hourly: pd.DataFrame,
    report_lines: list[str],
) -> bool:
    """Check half-hour alignment: offset 0 must be best."""
    if station_5min.empty or "ghi_obs" not in station_5min.columns or weather_hourly.empty:
        return True
    st = station_5min[["ts_utc_start", "ghi_obs"]].dropna()
    fc = weather_hourly.set_index(pd.to_datetime(weather_hourly["ts_utc_end"], utc=True))["ghi_fc"].dropna()
    report_lines += [
        "",
        "#### Half-hour alignment (station 5-min re-binned, informational)",
        "",
        "| Offset (min) | r daylight | RMSE daylight | r all hours | RMSE all hours |",
        "|---|---|---|---|---|",
    ]

    best_rd = -999.0
    best_offset = None

    for off in (-60, -30, 0, 30, 60):
        t = st["ts_utc_start"] - pd.Timedelta(minutes=off)
        lab = pd.DatetimeIndex(t.dt.floor("h") + pd.Timedelta(hours=1))
        g = st["ghi_obs"].groupby(lab)
        obs = g.mean()[g.size() >= 10]
        common = obs.index.intersection(fc.index)
        o, f = obs.loc[common], fc.loc[common]
        day = (o > 20) | (f > 20)
        def _rr(a, b):
            if len(a) < 100:
                return "N/A", "N/A", -999.0
            r_val = float(np.corrcoef(a, b)[0, 1])
            return f"{r_val:.4f}", f"{np.sqrt(np.mean((a - b) ** 2)):.1f}", r_val
        rd, ed, rd_num = _rr(o[day].values, f[day].values)
        ra, ea, _ = _rr(o.values, f.values)
        report_lines.append(f"| {off:+d} | {rd} | {ed} | {ra} | {ea} |")
        if rd_num > best_rd:
            best_rd = rd_num
            best_offset = off
    report_lines.append("")

    if best_offset == 0:
        report_lines.append("- ✅ Half-hour alignment OK: offset 0 min is best")
        return True
    else:
        report_lines.append(f"- ❌ Half-hour alignment FAILED: best offset is {best_offset:+d} min (not 0)")
        return False


def _df_to_markdown(df: pd.DataFrame, index: bool = True) -> str:
    """Safely format DataFrame to markdown string, falling back if tabulate is missing."""
    try:
        return df.to_markdown(index=index)
    except (ImportError, ModuleNotFoundError):
        if df.empty:
            return ""
        if index:
            df = df.reset_index()
        headers = [str(c) for c in df.columns]
        rows = [[str(val) for val in row] for row in df.values]
        sep = ["---"] * len(headers)
        lines = [
            "| " + " | ".join(headers) + " |",
            "| " + " | ".join(sep) + " |",
        ]
        for r in rows:
            lines.append("| " + " | ".join(r) + " |")
        return "\n".join(lines)


def _soft_checks(
    train_table: pd.DataFrame,
    arrays_meta: pd.DataFrame,
    manifest: dict,
    report_lines: list[str],
) -> None:
    """Run all soft checks and append to report_lines."""
    # 1. Weather gap list
    gaps = manifest.get("weather_gaps", [])
    report_lines.append("#### Weather Gaps")
    report_lines.append("")
    if gaps:
        report_lines.append(f"Found {len(gaps)} gap(s) inside window:")
        for g in gaps:
            report_lines.append(f"- Model `{g.get('model')}`: {g.get('start_utc')} → {g.get('end_utc')} ({g.get('hours')} hours)")
    else:
        report_lines.append("None. All forecast variables continuous within the window. ✅")
    report_lines.append("")

    # 2. Per-array correlation p_phys_norm vs target_norm
    if "p_phys_norm" in train_table.columns and "target_norm" in train_table.columns:
        row_filter = (
            train_table["target_norm"].notna()
            & (train_table.get("ghi_clear", 1) > 0)
            & train_table.get("ghi_fc", 1).notna()
            & train_table["p_phys_norm"].notna()
        )
        filtered = train_table[row_filter]
        if not filtered.empty:
            report_lines.append("#### Physical Baseline Correlation (p_phys_norm vs target_norm)")
            report_lines.append("")
            low_r_arrays = []
            for sid, grp in filtered.groupby("source_id"):
                if len(grp) >= 50:
                    r_val = float(np.corrcoef(grp["p_phys_norm"].values, grp["target_norm"].values)[0, 1])
                    if r_val < 0.8:
                        low_r_arrays.append((sid, r_val))
            if low_r_arrays:
                report_lines.append(f"Arrays with r < 0.8 ({len(low_r_arrays)}):")
                for sid, r_val in low_r_arrays:
                    report_lines.append(f"- source {sid}: r = {r_val:.4f}")
            else:
                report_lines.append("All qualifying arrays have r ≥ 0.8 with physics baseline. ✅")
            report_lines.append("")

    # 3. Azimuth sanity
    need = {"source_id", "ts_local_start", "target_kw", "tracking", "surface_azimuth_pvlib", "array_no"}
    if need.issubset(train_table.columns):
        fx = train_table[(train_table["tracking"] == "fixed") & train_table["target_kw"].notna()]
        if not fx.empty:
            loc = pd.to_datetime(fx["ts_local_start"])
            frac_hour = loc.dt.hour + loc.dt.minute / 60 + 0.5
            prof = fx.assign(h=frac_hour).groupby(["source_id", "h"])["target_kw"].mean()
            peak = prof.groupby(level=0).idxmax().map(lambda t: t[1])
            az = fx.groupby("source_id")["surface_azimuth_pvlib"].first()
            arr = fx.groupby("source_id")["array_no"].first()
            north = peak[az.reindex(peak.index).isin([0.0])]
            north_med = float(north.median()) if len(north) else float("nan")
            report_lines += [
                "#### Azimuth Sanity (peak hour of mean power, local time)",
                "",
                f"- North-facing fixed arrays: median peak {north_med:.1f} h (n={len(north)})",
            ]
            for sid, a in az.items():
                if pd.isna(a) or a == 0 or sid not in peak.index:
                    continue
                pk = peak[sid]
                ok = (pk < north_med) if 45 <= a <= 135 else (pk > north_med) if 225 <= a <= 315 else True
                report_lines.append(f"- source {sid} (array {arr[sid]}), azimuth {a:.0f}: peak {pk:.1f} h {'OK' if ok else 'CONTRADICTS orientation'}")
            report_lines.append("")

    # 4. Rows per array per month
    if "ts_utc_end" in train_table.columns and "source_id" in train_table.columns:
        tt = train_table.copy()
        tt["month"] = pd.to_datetime(tt["ts_utc_end"], utc=True).dt.to_period("M")
        monthly = tt.groupby(["source_id", "month"]).size().unstack(fill_value=0)
        report_lines.append("#### Rows per Array per Month (first 5 arrays shown)")
        report_lines.append("")
        report_lines.append(_df_to_markdown(monthly.head(5)))
        report_lines.append("")

    # 5. Arrays excluded
    if not arrays_meta.empty and "excluded" in arrays_meta.columns:
        excluded = arrays_meta[arrays_meta["excluded"]]
        report_lines.append(f"#### Excluded Arrays ({len(excluded)})")
        report_lines.append("")
        if not excluded.empty:
            exc_cols = ["source_id", "meter_name", "exclude_reason"]
            exc_cols = [c for c in exc_cols if c in excluded.columns]
            report_lines.append(_df_to_markdown(excluded[exc_cols], index=False))
        else:
            report_lines.append("None excluded.")
        report_lines.append("")

    # 6. Metadata incomplete
    if not arrays_meta.empty and "metadata_complete" in arrays_meta.columns:
        incomplete = arrays_meta[~arrays_meta["metadata_complete"].fillna(False)]
        report_lines.append(f"#### Arrays with Incomplete Metadata ({len(incomplete)})")
        report_lines.append("")
        if not incomplete.empty:
            ic_cols = ["source_id", "meter_name"]
            ic_cols = [c for c in ic_cols if c in incomplete.columns]
            report_lines.append(_df_to_markdown(incomplete[ic_cols], index=False))
        else:
            report_lines.append("All arrays have complete metadata.")
        report_lines.append("")

    # 7. kWp sanity: mean daily yield
    if "target_kw" in train_table.columns and "kwp" in train_table.columns:
        tt2 = train_table[train_table["target_kw"].notna() & train_table["kwp"].notna()].copy()
        if not tt2.empty:
            tt2["date"] = pd.to_datetime(tt2["ts_utc_end"], utc=True).dt.date
            daily_kwh = tt2.groupby(["source_id", "date", "kwp"])["target_kw"].sum().reset_index()
            daily_kwh["kwh_per_kwp"] = daily_kwh["target_kw"] / daily_kwh["kwp"]
            mean_yield = daily_kwh.groupby("source_id")["kwh_per_kwp"].mean()
            flagged = mean_yield[(mean_yield < 3) | (mean_yield > 7)]
            report_lines.append("#### kWp Sanity (mean daily yield, expected 3–7 kWh/kWp/day)")
            report_lines.append("")
            if not flagged.empty:
                report_lines.append(f"Arrays outside [3, 7]: {flagged.to_dict()}")
            else:
                report_lines.append("All arrays within expected range [3, 7] kWh/kWp/day. ✅")
            report_lines.append("")

    # 8. target_norm distribution
    if "target_norm" in train_table.columns:
        tn = train_table["target_norm"].dropna()
        if not tn.empty:
            report_lines.append("#### target_norm Distribution")
            report_lines.append("")
            report_lines.append(f"- min: {tn.min():.4f}")
            report_lines.append(f"- max: {tn.max():.4f}")
            report_lines.append(f"- mean: {tn.mean():.4f}")
            report_lines.append(f"- pct > 1.1: {(tn > 1.1).mean():.1%}")
            report_lines.append("")
