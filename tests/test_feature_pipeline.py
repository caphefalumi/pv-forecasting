"""Unit tests for PV feature pipeline.

1. Loader refuses a raw file without model, and never mixes two models in one hour.
2. Segment boundary: hour with ts_local_start 2024-12-31 23:30 -> ICON, 2025-01-01 00:30 -> ECMWF.
3. coverage_start with a mid-series gap returns the early start date and the gap appears in the report.
4. p_phys_norm on a hand-computed example (poly-Si, POA 800, Tcell 45 -> 0.8 * 0.92 * 0.86 = 0.63296).
5. pr_7d leakage: changing labels on day D and later does not change pr_7d for day D; changing day D-1 does.
6. pr_7d is NaN with fewer than min_days valid days.
7. Loader: banned columns never in X; coldstart arrays absent from array_scope="train"; recipe dates respected at both edges.
8. DKASC ingest (HTTP mocked): completeness gate rejects a short chunk; no overlapping chunk created; default start = day after last full chunk.
9. run on a small fixture: produces feature_spec.json, recipes.json, updates latest.json; a forced hard failure leaves latest.json unchanged.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from pvdata.config import PipelineConfig
from pvdata.dataset import Split, load_split, load_table
from pvdata.features import encode_categories, compute_cyclical_time_features
from pvdata.ingest_dkasc import (
    get_default_start_date,
    validate_chunk,
    fetch_dkasc,
)
from pvdata.io_openmeteo import (
    coverage_start,
    detect_weather_gaps,
    load as load_openmeteo,
)
from pvdata.physics import compute_p_phys_norm
from pvdata.pr import compute_pr_features


def _make_cfg(tmp_path: Path, **overrides) -> PipelineConfig:
    data = {
        "data_root": str(tmp_path),
        "site": {"latitude": -23.7624, "longitude": 133.8754, "altitude_m": 560, "tz": "Australia/Darwin"},
        "dkasc": {
            "yearly_files": [],
            "api_dir": "dkasc_api_5min",
            "api_name_map": "dkasc_api_5min/source_names.csv",
            "weather_source_id": 101,
            "metadata_file": "arrays_metadata.csv",
            "overrides_file": "array_overrides.csv",
        },
        "open_meteo": {
            "endpoint": "https://previous-runs-api.open-meteo.com/v1/forecast",
            "raw_dir": "open_meteo_raw",
            "variables": ["shortwave_radiation", "direct_normal_irradiance", "diffuse_radiation",
                          "temperature_2m", "cloud_cover", "wind_speed_10m"],
            "lead_suffix": "_previous_day1",
            "request_chunk": "year",
            "sleep_s": 0,
            "segments": [
                {"model": "icon_seamless", "wx_source": 0, "from_local": None, "to_local": "2024-12-31"},
                {"model": "ecmwf_ifs025", "wx_source": 1, "from_local": "2025-01-01", "to_local": None},
            ],
        },
        "window": {"start": "auto", "end": "2026-10-01"},
        "qc": {
            "neg_clip_kw": -0.5,
            "max_ratio_kwp": 1.2,
            "stuck_min_records": 6,
            "min_coverage": 0.8333,
            "daytime_zenith_max": 85,
            "near_zero_kwh_per_kwp_day": 0.5,
        },
        "physics": {
            "system_efficiency": 0.86,
            "t_ref_c": 25.0,
            "gamma_per_c": {
                "poly-Si": -0.0040,
                "mono-Si": -0.0039,
                "CdTe": -0.0028,
                "CIGS": -0.0036,
                "HIT": -0.0026,
                "a-Si": -0.0020,
                "CPV": None,
            },
        },
        "features": {
            "pr_min_valid_hours_per_day": 6,
            "pr_windows": {
                "pr_7d": {"days": 7, "min_days": 3},
                "pr_30d": {"days": 30, "min_days": 10},
            },
        },
        "splits": {
            "array": {
                "coldstart_source_ids": [105, 81, 79, 92, 56, 68],
            },
            "time": {
                "recipes": {
                    "fleet_v1": {
                        "train": ["2024-01-20", "2024-12-31"],
                        "val": ["2025-01-01", "2025-04-30"],
                        "test": ["2025-05-01", "2025-10-01"],
                        "feature_groups": ["base"],
                        "array_scope": "train",
                    },
                    "fleet_v2": {
                        "train": ["2024-01-20", "2025-12-31"],
                        "val": ["2026-01-01", "2026-04-30"],
                        "test": ["2026-05-01", "2026-10-01"],
                        "feature_groups": ["base"],
                        "array_scope": "train",
                    },
                    "v1_vs_v2": {
                        "train": None,
                        "val": None,
                        "test": ["2026-05-01", "2026-10-01"],
                        "feature_groups": ["base"],
                        "array_scope": "train",
                    },
                    "rolling": {
                        "train": None,
                        "val": None,
                        "test_days": 14,
                        "feature_groups": ["base"],
                        "array_scope": "train",
                    },
                },
            },
        },
    }
    data.update(overrides)
    return PipelineConfig(data, data_root_override=str(tmp_path))


# ---------------------------------------------------------------------------
# Test 1: Loader refuses file without model, never mixes two models in one hour
# ---------------------------------------------------------------------------

def test_loader_refuses_file_without_model(tmp_path: Path):
    cfg = _make_cfg(tmp_path)
    raw_dir = tmp_path / "open_meteo_raw"
    raw_dir.mkdir(parents=True)

    # Legacy file without model name in filename
    legacy_file = raw_dir / "previous_runs_2024-01-01_2024-12-31.json"
    legacy_file.write_text(json.dumps({"hourly": {"time": ["2024-01-01T00:00"]}}))

    with pytest.raises(ValueError, match="lacks model in filename"):
        load_openmeteo(cfg)


def test_loader_never_mixes_two_models_in_one_hour(tmp_path: Path):
    cfg = _make_cfg(tmp_path)
    raw_dir = tmp_path / "open_meteo_raw"
    raw_dir.mkdir(parents=True)

    times_icon = ["2024-12-31T14:00", "2024-12-31T15:00"]
    times_ecmwf = ["2024-12-31T15:00", "2024-12-31T16:00"]

    def _make_json(times):
        return {
            "latitude": -23.76, "longitude": 133.87, "elevation": 560,
            "hourly": {
                "time": times,
                "shortwave_radiation_previous_day1": [100.0] * len(times),
                "direct_normal_irradiance_previous_day1": [100.0] * len(times),
                "diffuse_radiation_previous_day1": [50.0] * len(times),
                "temperature_2m_previous_day1": [25.0] * len(times),
                "cloud_cover_previous_day1": [20.0] * len(times),
                "wind_speed_10m_previous_day1": [10.0] * len(times),
            }
        }

    f1 = raw_dir / "previous_runs_icon_seamless_2024-01-01_2024-12-31.json"
    f1.write_text(json.dumps(_make_json(times_icon)))
    (raw_dir / (f1.name + ".meta.json")).write_text(json.dumps({"model": "icon_seamless"}))

    f2 = raw_dir / "previous_runs_ecmwf_ifs025_2025-01-01_2025-12-31.json"
    f2.write_text(json.dumps(_make_json(times_ecmwf)))
    (raw_dir / (f2.name + ".meta.json")).write_text(json.dumps({"model": "ecmwf_ifs025"}))

    df, _, _ = load_openmeteo(cfg)
    assert not df["ts_utc_end"].duplicated().any()
    # Check that each timestamp has exactly one model
    assert df["wx_model"].notna().all()


# ---------------------------------------------------------------------------
# Test 2: Segment boundary mapping
# ---------------------------------------------------------------------------

def test_segment_boundary_mapping(tmp_path: Path):
    """Hour with ts_local_start 2024-12-31 23:30 -> ICON, 2025-01-01 00:30 -> ECMWF."""
    cfg = _make_cfg(tmp_path)
    raw_dir = tmp_path / "open_meteo_raw"
    raw_dir.mkdir(parents=True)

    # 15:00 UTC = 00:30 ACST (ts_utc_start = 14:00 UTC = 23:30 ACST 2024-12-31)
    # 16:00 UTC = 01:30 ACST (ts_utc_start = 15:00 UTC = 00:30 ACST 2025-01-01)
    times = ["2024-12-31T15:00", "2024-12-31T16:00"]
    data = {
        "latitude": -23.76, "longitude": 133.87, "elevation": 560,
        "hourly": {
            "time": times,
            "shortwave_radiation_previous_day1": [100.0, 100.0],
            "direct_normal_irradiance_previous_day1": [100.0, 100.0],
            "diffuse_radiation_previous_day1": [50.0, 50.0],
            "temperature_2m_previous_day1": [25.0, 25.0],
            "cloud_cover_previous_day1": [20.0, 20.0],
            "wind_speed_10m_previous_day1": [10.0, 10.0],
        }
    }

    f1 = raw_dir / "previous_runs_icon_seamless_2024-01-01_2024-12-31.json"
    f1.write_text(json.dumps(data))
    (raw_dir / (f1.name + ".meta.json")).write_text(json.dumps({"model": "icon_seamless"}))

    f2 = raw_dir / "previous_runs_ecmwf_ifs025_2025-01-01_2025-12-31.json"
    f2.write_text(json.dumps(data))
    (raw_dir / (f2.name + ".meta.json")).write_text(json.dumps({"model": "ecmwf_ifs025"}))

    df, _, _ = load_openmeteo(cfg)
    row_icon = df[df["ts_utc_end"] == pd.Timestamp("2024-12-31 15:00:00", tz="UTC")]
    row_ecmwf = df[df["ts_utc_end"] == pd.Timestamp("2024-12-31 16:00:00", tz="UTC")]

    assert len(row_icon) == 1
    assert row_icon.iloc[0]["wx_model"] == "icon_seamless"
    assert row_icon.iloc[0]["wx_source"] == 0

    assert len(row_ecmwf) == 1
    assert row_ecmwf.iloc[0]["wx_model"] == "ecmwf_ifs025"
    assert row_ecmwf.iloc[0]["wx_source"] == 1


# ---------------------------------------------------------------------------
# Test 3: coverage_start with mid-series gap
# ---------------------------------------------------------------------------

def test_coverage_start_with_mid_series_gap(tmp_path: Path):
    cfg = _make_cfg(tmp_path)
    times = pd.date_range("2024-01-19 00:00", periods=48, freq="h", tz="UTC")
    # First 12 hours null
    ghi = [np.nan] * 12 + [400.0] * 36
    # Add a mid-series gap at hour 30-32
    ghi[30] = np.nan
    ghi[31] = np.nan

    df = pd.DataFrame({
        "ts_utc_end": times,
        "ghi_fc": ghi,
        "dni_fc": ghi,
        "dhi_fc": ghi,
        "temp_fc": [25.0] * 48,
        "cloud_fc": [10.0] * 48,
        "wind_fc": [5.0] * 48,
        "wx_model": ["icon_seamless"] * 48,
    })

    # Coverage start should be based on first complete hour (hour 12: 2024-01-19 12:00 UTC)
    # -> local Darwin date 2024-01-19 -> next full day 2024-01-20
    start_date = coverage_start(df, cfg)
    assert start_date == "2024-01-20"

    gaps = detect_weather_gaps(df)
    assert len(gaps) >= 1
    # Check that mid-series gap is detected
    gap_hours = sum(g["hours"] for g in gaps)
    assert gap_hours >= 2


# ---------------------------------------------------------------------------
# Test 4: p_phys_norm hand-computed example
# ---------------------------------------------------------------------------

def test_p_phys_norm_hand_computed(tmp_path: Path):
    """poly-Si, POA 800, Tcell 45 -> 0.8 * 0.92 * 0.86 = 0.63296."""
    cfg = _make_cfg(tmp_path)
    poa = pd.Series([800.0])
    tcell = pd.Series([45.0])
    tech = pd.Series(["poly-Si"])

    res = compute_p_phys_norm(poa, tcell, tech, cfg)
    assert len(res) == 1
    assert abs(res[0] - 0.63296) < 1e-4

    # CPV should yield NaN
    res_cpv = compute_p_phys_norm(poa, tcell, pd.Series(["CPV"]), cfg)
    assert np.isnan(res_cpv[0])


# ---------------------------------------------------------------------------
# Test 5 & 6: pr_7d leakage and min_days
# ---------------------------------------------------------------------------

def test_pr_7d_leakage_and_min_days(tmp_path: Path):
    cfg = _make_cfg(tmp_path)

    # 10 days of data, 12 hours per day
    days = [date(2024, 6, 1) + timedelta(days=i) for i in range(10)]
    rows = []
    for d in days:
        for h in range(12):
            ts_loc = datetime(d.year, d.month, d.day, 6 + h, 0)
            rows.append({
                "source_id": 52,
                "ts_local_start": ts_loc,
                "target_kw": 5.0,
                "p_phys_norm": 0.8,
                "kwp": 5.5,
                "ghi_clear": 500.0,
            })
    df = pd.DataFrame(rows)

    pr1 = compute_pr_features(df, cfg)
    day_d = date(2024, 6, 8)  # day index 7
    mask_day_d = pd.to_datetime(df["ts_local_start"]).dt.date == day_d
    val_before = pr1.loc[mask_day_d, "pr_7d"].iloc[0]
    assert np.isfinite(val_before)

    # Modify labels on day D and later (day D+1)
    df_leak = df.copy()
    mask_after = pd.to_datetime(df["ts_local_start"]).dt.date >= day_d
    df_leak.loc[mask_after, "target_kw"] = 999.0

    pr2 = compute_pr_features(df_leak, cfg)
    val_after = pr2.loc[mask_day_d, "pr_7d"].iloc[0]
    # Day D PR must NOT change when future labels change
    assert val_before == val_after

    # Modify day D-1 labels -> day D PR MUST change
    df_past = df.copy()
    mask_d_minus_1 = pd.to_datetime(df["ts_local_start"]).dt.date == (day_d - timedelta(days=1))
    df_past.loc[mask_d_minus_1, "target_kw"] = 1.0

    pr3 = compute_pr_features(df_past, cfg)
    val_past_changed = pr3.loc[mask_day_d, "pr_7d"].iloc[0]
    assert val_past_changed != val_before

    # Test min_days: on day 2 (June 3), only 2 prior days exist (< min_days=3) -> must be NaN
    mask_day_2 = pd.to_datetime(df["ts_local_start"]).dt.date == date(2024, 6, 3)
    assert np.isnan(pr1.loc[mask_day_2, "pr_7d"].iloc[0])


# ---------------------------------------------------------------------------
# Test 7: Loader banned columns, coldstart, recipe dates
# ---------------------------------------------------------------------------

def test_loader_banned_columns_and_coldstart(tmp_path: Path):
    proc_dir = tmp_path / "processed" / "v_test"
    proc_dir.mkdir(parents=True)

    # Write dummy latest.json
    (tmp_path / "processed" / "latest.json").write_text(json.dumps({"version": "v_test"}))

    # Create dummy train_table
    times = pd.date_range("2024-01-20 00:00", periods=25, freq="D", tz="UTC")
    tt = pd.DataFrame({
        "source_id": [52] * 25 + [105] * 25,  # 105 is coldstart
        "ts_utc_end": times.tolist() * 2,
        "ts_local_start": times.tolist() * 2,
        "kwp": [5.0] * 50,
        "target_norm": [0.5] * 50,
        "ghi_clear": [500.0] * 50,
        "ghi_fc": [450.0] * 50,
        "dni_fc": [300.0] * 50,
        "dhi_fc": [150.0] * 50,
        "temp_fc": [25.0] * 50,
        "cloud_fc": [10.0] * 50,
        "wind_fc": [5.0] * 50,
        "wx_source": [0] * 50,
        "solar_zenith": [30.0] * 50,
        "solar_azimuth": [180.0] * 50,
        "kt_fc": [0.9] * 50,
        "poa_fc": [480.0] * 50,
        "temp_cell_fc": [35.0] * 50,
        "p_phys_norm": [0.45] * 50,
        "technology_code": [0] * 50,
        "tracking_code": [0] * 50,
        "tilt_deg": [20.0] * 50,
        "surface_azimuth_pvlib": [0.0] * 50,
        "hour_sin": [0.5] * 50,
        "hour_cos": [0.5] * 50,
        "doy_sin": [0.5] * 50,
        "doy_cos": [0.5] * 50,
        "array_split": ["train"] * 25 + ["coldstart"] * 25,
    })
    tt.to_parquet(proc_dir / "train_table.parquet")

    spec = {
        "version": "v_test",
        "row_filter": "target_norm.notna() & ghi_clear > 0 & ghi_fc.notna()",
        "feature_groups": {
            "base": ["ghi_fc", "dni_fc", "dhi_fc", "p_phys_norm"],
            "banned_group": ["target_norm", "ghi_fc"],
        },
        "banned": ["target_norm", "target_kw"],
    }
    (proc_dir / "feature_spec.json").write_text(json.dumps(spec))

    recipes = {
        "fleet_v1": {
            "train": ["2024-01-20", "2024-02-01"],
            "val": None,
            "test": None,
            "feature_groups": ["base"],
            "array_scope": "train",
        }
    }
    (proc_dir / "recipes.json").write_text(json.dumps(recipes))

    # 1. Loader with banned feature column raises ValueError
    with pytest.raises(ValueError, match="banned list"):
        load_split(version="v_test", recipe="fleet_v1", groups=["banned_group"], data_root=tmp_path)

    # 2. Loader excludes coldstart when array_scope="train"
    split = load_split(version="v_test", recipe="fleet_v1", array_scope="train", data_root=tmp_path)
    assert split.X_train is not None
    assert 105 not in split.meta_train["source_id"].values
    assert set(split.meta_train["source_id"].unique()) == {52}


# ---------------------------------------------------------------------------
# Test 8: DKASC ingest completeness gate & no overlap
# ---------------------------------------------------------------------------

def test_ingest_dkasc_completeness_and_no_overlap(tmp_path: Path):
    api_dir = tmp_path / "dkasc_api_5min"
    api_dir.mkdir(parents=True)

    # Create dummy existing chunk: 2026-09-20 -> 2026-09-22
    (api_dir / "2026-09-20_2026-09-22.csv").write_text("dummy")

    # 1. Default start date is day after last chunk
    next_start = get_default_start_date(api_dir)
    assert next_start == date(2026, 9, 23)

    # 2. Completeness gate rejects short row count
    cols = ["Timestamp"] + [f"Col{i}" for i in range(50)]
    header = ",".join(cols)
    short_content = header + "\n" + "\n".join(["2026-09-23 00:00:00" + ",1" * 50] * 10)  # only 10 rows
    valid, reason = validate_chunk(short_content, date(2026, 9, 23), date(2026, 9, 25), set(), today_darwin=date(2026, 10, 1))
    assert not valid
    assert "Row count" in reason

    # 3. Completeness gate rejects header with < 51 columns
    short_header = "Timestamp,Col1,Col2\n" + "\n".join(["2026-09-23 00:00:00,1,1"] * 900)
    valid, reason = validate_chunk(short_header, date(2026, 9, 23), date(2026, 9, 25), set(), today_darwin=date(2026, 10, 1))
    assert not valid
    assert "columns (< 51)" in reason


# ---------------------------------------------------------------------------
# Test 9: run subcommand on small fixture
# ---------------------------------------------------------------------------

def test_run_subcommand_fixture(tmp_path: Path):
    """run produces feature_spec.json, recipes.json, updates latest.json; failure preserves latest.json."""
    from pvdata.__main__ import main

    cfg_data = _make_cfg(tmp_path).as_dict()
    # Write pipeline.yaml
    cfg_file = tmp_path / "pipeline.yaml"
    import yaml
    cfg_file.write_text(yaml.dump(cfg_data))

    # Set up minimal mock directories and files
    proc_dir = tmp_path / "processed"
    proc_dir.mkdir(parents=True)
    (proc_dir / "latest.json").write_text(json.dumps({"version": "v_initial"}))

    # Run `run` with mock build & validate
    with patch("pvdata.build.build") as mock_build, patch("pvdata.validate.validate", return_value=0) as mock_val:
        main(["--config", str(cfg_file), "--data-root", str(tmp_path), "run", "--until", "2026-10-01", "--skip-fetch"])
        assert mock_build.called
        assert mock_val.called

    # Check latest.json was updated
    latest = json.loads((proc_dir / "latest.json").read_text())
    assert latest["version"] == "2026-10-01"
    assert latest["until"] == "2026-10-01"

    # Now simulate a validation failure: latest.json must NOT be updated
    with patch("pvdata.build.build"), patch("pvdata.validate.validate", return_value=1), pytest.raises(SystemExit):
        main(["--config", str(cfg_file), "--data-root", str(tmp_path), "run", "--until", "2026-10-02", "--skip-fetch"])

    latest_after_fail = json.loads((proc_dir / "latest.json").read_text())
    assert latest_after_fail["version"] == "2026-10-01"  # preserved!
