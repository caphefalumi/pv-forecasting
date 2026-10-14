# PV Data & Feature Pipeline — Technical Documentation

| | |
|---|---|
| **Project** | COS40007 Applied Project, Solar PV field |
| **Task** | Day-ahead hourly forecasting of PV output for newly commissioned systems |
| **Package** | `pvdata` (Python 3.11+) |
| **Current snapshot** | `2026-10-01` (data 2024-01-20 → 2026-10-01, local time) |
| **Document date** | 2026-10-08 |

This document explains what the pipeline produces, where every file lives, how each feature is computed, how to load the data for training, and how the weekly automation works. `README.md` is the short version. This file is the reference.

---

## Contents

1. [Overview](#1-overview)
2. [Quick start](#2-quick-start)
3. [Paths and repository layout](#3-paths-and-repository-layout)
4. [Data sources](#4-data-sources)
5. [Arrays](#5-arrays)
6. [Weather source design](#6-weather-source-design)
7. [Processing stages](#7-processing-stages)
8. [Target and features](#8-target-and-features)
9. [Output files](#9-output-files)
10. [Splits, recipes and leakage rules](#10-splits-recipes-and-leakage-rules)
11. [Loading data for training](#11-loading-data-for-training)
12. [Command-line interface](#12-command-line-interface)
13. [Weekly automation](#13-weekly-automation)
14. [Configuration reference](#14-configuration-reference)
15. [Validation and tests](#15-validation-and-tests)
16. [Known issues and open decisions](#16-known-issues-and-open-decisions)
17. [Decision log](#17-decision-log)

---

## 1. Overview

The pipeline turns raw measurements and raw weather forecasts into a **training-ready table**: one row per array per hour, with day-ahead weather forecasts, solar geometry, physics-based features and the measured output as the label.

```
DKASC 5-min power (raw) ──► QC (5-min) ──► hourly aggregation ──► labels_hourly ──┐
Open-Meteo forecasts (raw, per model) ──► segment by date ──► weather_hourly ──────┼──► features ──► train_table
arrays_metadata.csv + array_overrides.csv ──► arrays ──────────────────────────────┘          │
                                                                                               ▼
                                      validate (hard checks) ──► feature_spec.json + recipes.json ──► latest.json
```

| Property | Value |
|---|---|
| Granularity | Hourly, UTC hour-ending bins |
| Forecast horizon | Day-ahead (Open-Meteo `_previous_day1` forecasts) |
| Label | `target_norm` = measured mean AC power ÷ rated kWp |
| Arrays in the data | 46 meters, **31 usable** (25 train + 6 cold-start), 15 excluded |
| Snapshot size | 1,088,544 rows × 40 columns, **360,322 usable rows** |
| Rebuild time | A few minutes on a laptop |
| Update cadence | Weekly (Sunday), full rebuild from raw |

---

## 2. Quick start

```bash
# setup (Windows PowerShell: .venv\Scripts\Activate.ps1)
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pytest -q                                   # 74 tests

# train-ready data (snapshot already built)
python -c "from pvdata import load_split; s = load_split('fleet_v1'); print(s.X_train.shape)"
```

```python
from pvdata import load_split

s = load_split("fleet_v1")                    # recipe name
model.fit(s.X_train, s.y_train, eval_set=[(s.X_val, s.y_val)])
pred_norm = model.predict(s.X_test)           # kW per kWp
pred_kw   = pred_norm * s.meta_test["kwp"]    # back to kW
```

---

## 3. Paths and repository layout

### 3.1 Root

| Location | Path |
|---|---|
| Laptop (Robertiz) | `C:\Swinburne University\COS40007 - Artificial Intelligence Engineering\PROJECT\code\` |
| Any clone | repository root, referred to as `code/` below |

### 3.2 Repository (committed to git)

```
code/
├── config/pipeline.yaml        all settings: paths, QC thresholds, weather segments, physics, recipes
├── pvdata/
│   ├── __init__.py             public API: load_split, load_table, Split
│   ├── __main__.py             CLI (python -m pvdata <command>)
│   ├── config.py               YAML loader and validation
│   ├── ingest_dkasc.py         DKASC API download (3-day chunks, completeness gate)
│   ├── io_dkasc.py             read yearly CSVs + API chunks, dedupe, to long UTC table
│   ├── io_openmeteo.py         Open-Meteo download and load, per-model segments, gap report
│   ├── metadata.py             array metadata, overrides, exclusion, validity windows
│   ├── qc.py                   5-minute quality flags
│   ├── aggregate.py            5-minute → hourly labels and station data
│   ├── features.py             solar position, clear-sky, POA, cell temperature
│   ├── physics.py              p_phys_norm (physics baseline)
│   ├── pr.py                   pr_7d, pr_30d (recent performance ratio)
│   ├── build.py                assembles a snapshot
│   ├── validate.py             hard and soft checks, writes qc_report.md
│   ├── dataset.py              loader for training (recipes, filters, banned columns)
│   └── sample.py               small CSV/Excel export for inspection
├── tests/                      pytest suite (74 tests)
├── scripts/run_weekly.sh       weekly job with S3 sync
├── Dockerfile, .dockerignore
├── requirements.txt
├── README.md
└── docs/data-pipeline.md       this file
```

### 3.3 Data folder (never committed, except three small metadata files)

| Path (under `code/data/`) | Stage | Content | In git |
|---|---|---|---|
| `Alice_Springs_2024.csv`, `_2025.csv`, `_2026.csv` | Raw | DKASC yearly files, 5-min, wide format. 2026 file ends 2026-02-11 | No |
| `dkasc_api_5min/<start>_<end>.csv` | Raw | DKASC API chunks from 2026-02-12, 3 days per file | No |
| `dkasc_api_5min/source_names.csv` | Metadata | API column name → `source_id` map | **Yes** |
| `open_meteo_raw/previous_runs_<model>_<start>_<end>.json` (+ `.meta.json`) | Raw | Open-Meteo responses, one set per weather model | No |
| `arrays_metadata.csv` | Metadata (hand-edited) | One row per meter: kWp, technology, tilt, azimuth, tracking… | **Yes** |
| `array_overrides.csv` | Metadata (hand-edited) | Array exclusions and validity windows | **Yes** |
| `interim/` | Cache | 5-min labels/station, per-model weather. Safe to delete, rebuilt automatically | No |
| `processed/<version>/` | **Output** | One snapshot (see §9) | No |
| `processed/latest.json` | Output | Pointer to the newest valid snapshot | No |

**Final training data:** `code/data/processed/2026-10-01/train_table.parquet` (or whatever `latest.json` points to).
**Hourly data before feature engineering:** `labels_hourly.parquet`, `weather_hourly.parquet`, `station_hourly.parquet` in the same folder.
**Raw data:** the yearly CSVs, `dkasc_api_5min/`, `open_meteo_raw/`.

### 3.4 Outside the repository (laptop only)

| Path (under `PROJECT\`) | Content |
|---|---|
| `_archive\2026-10-08\` | Obsolete files kept until manually deleted: old plan, old build `v1` (invalid weather input), old samples, legacy `best_match` weather files, unused DKASC station file |
| `references\dkasc\` | DKASC "Notes on the Data" and "Terms & Conditions" PDFs |

---

## 4. Data sources

### 4.1 DKASC (labels)

| Item | Detail |
|---|---|
| Provider | Desert Knowledge Australia Solar Centre, Alice Springs site |
| Yearly files | https://dkasolarcentre.com.au/download?location=alice-springs |
| API (gap after yearly files) | `https://solarcentre.spinifexvalley.com.au/power/export/average` (undocumented backend of the DKASC graph page). Params `start_date`, `end_date`, `interval=""` (5-min, max 3 days per request), `sources` = `{"<id>": [193], "101": [10021, 9000, 9304, 506]}` |
| Quantity | Active power (code 193), kW, AC, 5-minute mean |
| Weather station (source 101) | Rainfall, air temperature, GHI, wind speed. Used **only for QC and validation**, never as a feature |
| Timestamps | Local Alice Springs time, UTC+09:30, no daylight saving (`Australia/Darwin`). Period-beginning |
| Column names | `<source_id>_<meter>_<quantity>` in yearly files. Meter names change between years, so parsing always uses the leading `source_id` |
| Known raw defects | 2025 file has 2 malformed lines (9992, 10128), skipped. 2025 and 2026 files overlap on 2026-01-01, deduplicated |
| Latency | About 30 minutes |
| Licence | DKASC copyright. Do not commit or redistribute raw data. Cite according to the DKASC Terms |

### 4.2 Open-Meteo (inputs)

| Item | Detail |
|---|---|
| Training endpoint | `https://previous-runs-api.open-meteo.com/v1/forecast`, `timezone=GMT`, `models=<model>` |
| Variables | `shortwave_radiation`, `direct_normal_irradiance`, `diffuse_radiation`, `temperature_2m`, `cloud_cover`, `wind_speed_10m`, all with suffix `_previous_day1` (the forecast issued one day earlier) |
| Radiation convention | Value at hour H (UTC) = mean over [H−1, H) |
| Wind | Converted from km/h to m/s |
| Serving endpoint (live forecasts) | `https://api.open-meteo.com/v1/forecast` with the **same** `models=` as the model being served |
| Site coordinates | −23.7624, 133.8754, 560 m (approximate, not yet verified against the DKASC map) |
| Grid points actually served | ICON: (−23.75, 133.875). ECMWF 0.25°: (−23.75, 134.0) |

---

## 5. Arrays

DKASC Alice Springs has 46 array meters in the data. All 31 usable arrays are **fixed** mounts (all trackers are excluded).

### 5.1 Train arrays (25)

| source_id | Array | Technology | kWp | Tilt (°) | Azimuth (°) |
|---|---|---|---|---|---|
| 52 | 33 | poly-Si | 5.50 | 20 | 0 (N) |
| 55 | 29 | poly-Si | 6.00 | 20 | 0 |
| 58 | 36 | poly-Si | 5.20 | 20 | 0 |
| 61 | 23 | CdTe | 5.40 | 20 | 0 |
| 63 | 34 | poly-Si | 5.28 | 20 | 0 |
| 64 | 37 | poly-Si | 5.50 | 20 | 0 |
| 67 | 21 | poly-Si | 4.92 | 20 | 0 |
| 69 | 17 | HIT | 6.30 | 20 | 0 |
| 70 | 3 | poly-Si | 4.95 | 20 | 0 |
| 71 | 18 | mono-Si | 5.236 | 20 | 0 |
| 72 | 26 | poly-Si | 5.50 | 20 | 0 |
| 73 | 35 | poly-Si | 5.50 | 20 | 0 |
| 74 | 31 | mono-Si | 5.00 | 20 | 0 |
| 84 | 12 | mono-Si | 5.10 | 20 | 0 |
| 85 | 10 | mono-Si | 5.805 | 20 | 0 |
| 90 | 14 | poly-Si | 5.40 | 20 | 0 |
| 93 | 8 | a-Si | 6.00 | 20 | 0 |
| 98 | 19 | mono-Si | 5.04 | 20 | 0 |
| 100 | 16A | poly-Si | 1.98 | 20 | 0 |
| 103 | 16B | poly-Si | 1.98 | 2 (near flat) | 0 |
| 106 | 11 | poly-Si | 4.95 | 20 | 0 |
| 212 | 25 | poly-Si | 5.83 | 20 | 0 |
| 213 | 24 | poly-Si | 6.05 | 20 | 0 |
| 214 | 32 | poly-Si | 5.30 | 20 | 0 |
| 218 | 9A | CIGS | 5.20 | 20 | 0 (assumed, not stated on the DKASC site) |

### 5.2 Cold-start arrays (6), held out from training

| source_id | Array | Technology | kWp | Tilt / Azimuth | Why held out |
|---|---|---|---|---|---|
| 105 | 16C | poly-Si | 1.98 | 20 / 90 (E) | East-facing, unseen orientation |
| 81 | 16D | poly-Si | 1.98 | 20 / 270 (W) | West-facing, unseen orientation |
| 79 | 7 | CdTe | 6.96 | 20 / 0 | CdTe (array 23, also CdTe, stays in train) |
| 92 | 13 | mono-Si | 5.25 | 20 / 0 | Typical mono-Si |
| 56 | 30 | poly-Si | 5.61 | 20 / 0 | Typical poly-Si |
| 68 | 20 | poly-Si | 5.04 | 20 / 0 | Genuinely under-performing (~3.3 kWh/kWp/day). Fault case for monitoring demos |

Array 9A (CIGS) is **not** held out: it is the only CIGS array, so holding it out would test "unseen technology", not "new customer".

### 5.3 Excluded arrays (15)

| source_id | Array | Reason |
|---|---|---|
| 54, 57, 60, 66, 77, 99, 102 | various | Stopped or near-zero output (auto flag). 54→212, 57→213, 77→214, 99→218 are replacement meters |
| 59 | 38 | Stopped early (2024-02-05) |
| 78, 86, 87, 91, 95 | 2, 4, 1B, 1A, 6 | Dual-axis trackers. Yield below fixed arrays, performance unverified |
| 89 | 5 | Single-axis tracker. Same reason |
| 97 | 22 | CPV dual-axis tracker. Same reason, and no physics model for CPV |

Exclusions are applied through `array_overrides.csv` and automatic rules in `metadata.py`. Excluded arrays stay in `train_table` with `array_split = "excluded"` and `target_kw = NaN`. The loader never returns them.

### 5.4 Metadata conventions (`arrays_metadata.csv`)

| Rule | Detail |
|---|---|
| Columns | Read by name: `source_id, meter_name, source_type, kwh_2024_to_2026_02, first_output, last_output, auto_flag, page_url_guess, array_no, manufacturer, kwp, technology, tilt_deg, azimuth_deg, tracking, inverter, commissioned, notes`. Do not rename |
| `array_no` | String (`1A`, `9A`, `16D`). Read with `dtype={"array_no": str}` |
| `azimuth_deg` | DKASC convention 0 = North, same as pvlib (90 = E, 270 = W). Stored in the table as `surface_azimuth_pvlib` |
| `tracking` | `fixed`, `single_axis` or `dual_axis`. Blank allowed for stopped meters |
| `technology` | `poly-Si`, `mono-Si`, `CdTe`, `CIGS`, `HIT`, `a-Si`, `CPV` |
| Dates | ISO `YYYY-MM-DD`. Do not open and re-save with Excel |
| `first_output`, `last_output` | Reference only. Validity windows come only from `array_overrides.csv` |
| Source of truth for the id → array mapping | Hidden values of the checkboxes on https://dkasolarcentre.com.au/locations/alice-springs/graphs |

---

## 6. Weather source design

| Local date | Model | `wx_source` |
|---|---|---|
| 2024-01-20 → 2024-12-31 | `icon_seamless` (DWD ICON) | 0 |
| 2025-01-01 onward | `ecmwf_ifs025` (ECMWF IFS 0.25°) | 1 |

The boundary is applied by the local date of `ts_local_start` (hour starting 2024-12-31 23:30 local is ICON, 2025-01-01 00:30 is ECMWF).

**Why two sources.** Open-Meteo's default `best_match` silently switched model on 2025-10-01, so every model is now pinned explicitly. ECMWF 0.25° has no gaps and a lower bias against the DKASC station (−20 vs −39 W/m² on the clean period 2024-03-08 → 2025-04-30), but its history only starts 2024-03-06. ICON covers January 2024.

**Consequence for the champion–challenger demo (deliberate).** Champion v1 is trained on 2024 (ICON) and evaluated on 2025/2026 rows (ECMWF). It therefore meets a change of input source it never saw. The report frames this as a **weather-provider migration on 2025-01-01**: the champion meets input drift, the challenger trained on post-migration data replaces it. Two caveats must be stated in the report:

1. The champion is served with a different weather source from the one it was trained on.
2. In the challenger's training data `wx_source` is identical to "year ≥ 2025", so the model cannot separate a source effect from a time effect.

Full rationale: `handoff/PV_weather_source_decision.md` in the claude.ai Project.

---

## 7. Processing stages

### 7.1 Time conventions

| Field | Meaning |
|---|---|
| `ts_utc_end` | End of the hourly bin, UTC. Bin = [ts_utc_end − 1 h, ts_utc_end) |
| `ts_local_start` | Start of the same bin in local time (UTC+09:30). Local bins therefore start at hh:30 |
| Solar position | Computed at the bin midpoint (ts_utc_end − 30 min) |
| Recipe dates | Local date of `ts_local_start`, both ends inclusive |
| Window | `2024-01-20` (first full local day with complete ICON coverage) → `--until` |

### 7.2 Stages

| # | Stage | Module | What happens |
|---|---|---|---|
| 1 | Ingest DKASC | `ingest_dkasc.py` | Download 3-day chunks after the last complete chunk. A chunk is written only if its header is valid, all columns are known, rows ≥ 98% of 288 × days, and its end date is before today (local). Never overlaps an existing chunk |
| 2 | Ingest weather | `io_openmeteo.py` | Download each segment's model over its date range (+1 day buffer). Raw files are immutable, named by model |
| 3 | Read DKASC | `io_dkasc.py` | Yearly files + API chunks → long 5-min table in UTC. Bad lines skipped. Duplicates: yearly file beats API, newest API file beats older. Cached in `interim/` keyed by an input signature |
| 4 | Metadata | `metadata.py` | Load metadata and overrides, derive `metadata_complete`, pvlib azimuth, exclusions, validity windows |
| 5 | QC (5-min) | `qc.py` | Flags (see §7.3). Flags never delete records |
| 6 | Hourly aggregation | `aggregate.py` | Mean of valid 5-min values per UTC hour. `target_kw` is NaN if fewer than 10 of 12 records are valid, the array is excluded, or the hour is outside its validity window |
| 7 | Weather load | `io_openmeteo.py` | Load only each segment's model, assign hours to segments, never mix models within an hour. Gaps listed, not filled |
| 8 | Features | `features.py`, `physics.py`, `pr.py`, `build.py` | See §8 |
| 9 | Assemble | `build.py` | Join labels, weather and features into `train_table`, fill `array_split`, write snapshot files and manifest |
| 10 | Validate | `validate.py` | Hard and soft checks (§15). Any hard failure → `latest.json` is not updated |
| 11 | Publish | `__main__.py` (`run`) | Write `feature_spec.json`, `recipes.json`, update `latest.json` |

### 7.3 QC rules (5-minute)

| Flag | Rule | Effect |
|---|---|---|
| `missing` | Value is NaN | Invalid |
| (clip) | −0.5 kW ≤ value < 0 → set to 0 | No flag |
| `neg` | Value < −0.5 kW | Invalid |
| `over_kwp` | Value > 1.2 × kWp | Invalid |
| `stuck` | ≥ 6 consecutive identical non-zero daytime values (zenith < 85°) | Invalid |
| `day_zero` | Value **= 0** while station GHI > 300 W/m² and fleet median power/kWp > 0.3 | Invalid. See §16 (B1) for a proposed change |

---

## 8. Target and features

### 8.1 Target

| Column | Unit | Definition |
|---|---|---|
| `target_kw` | kW | Mean valid 5-min AC power in the hour. NaN if coverage < 10/12, array excluded, or outside validity window |
| `target_norm` | kW/kWp | `target_kw / kwp`. **The training label.** Range 0 → ~1.0 (max observed 0.97) |

Normalising by kWp lets one fleet model serve arrays of any size. Multiply predictions by `kwp` to get kW.

### 8.2 Feature reference

Expected importance is based on PV physics and has not been measured yet. Check it with the model's feature importance after training.

**Group `base` (on by default, 23 features)**

| Feature | Unit | Inputs | Computation | Role | Expected importance |
|---|---|---|---|---|---|
| `p_phys_norm` | kW/kWp | `poa_fc`, `temp_cell_fc`, technology | `max(0, poa_fc/1000 × [1 + γ(T_cell − 25)] × 0.86)`. γ per technology (§14.4). 0.86 = PVWatts default system losses | Physics baseline (Champion 0) and strongest single input. The model learns the residual | Very high |
| `poa_fc` | W/m² | GHI, DNI, DHI forecasts, sun position, tilt, azimuth | Transposition to the panel plane, Hay-Davies model (pvlib) | Irradiance actually reaching the panel. Power is close to proportional to it | Very high |
| `ghi_fc` | W/m² | Open-Meteo `shortwave_radiation_previous_day1` | As delivered | Main energy input | High |
| `kt_fc` | – | `ghi_fc`, `ghi_clear` | `ghi_fc / ghi_clear`, clipped to [0, 1.5], NaN when `ghi_clear` < 20 | How clear the forecast sky is, independent of sun angle | High |
| `solar_zenith` | deg | Time, site location | pvlib, at bin midpoint (apparent zenith) | Sun height: sunrise, sunset, noon peak | High |
| `solar_azimuth` | deg | Time, site location | pvlib, at bin midpoint | Sun direction. Matters for east/west arrays | High |
| `cloud_fc` | % | Open-Meteo `cloud_cover_previous_day1` | As delivered | Forecast uncertainty that mean GHI hides | Medium |
| `ghi_clear` | W/m² | Time, location, altitude | Ineichen clear-sky model (pvlib) | Theoretical maximum. Also used to select daytime rows | Medium |
| `temp_cell_fc` | °C | `poa_fc`, `temp_fc`, `wind_fc` | SAPM cell temperature, open-rack glass/polymer (pvlib) | Hotter cells are less efficient | Medium |
| `dni_fc` | W/m² | Open-Meteo `direct_normal_irradiance_previous_day1` | As delivered | Direct sun versus haze | Medium-low |
| `dhi_fc` | W/m² | Open-Meteo `diffuse_radiation_previous_day1` | As delivered | Diffuse share of light | Medium-low |
| `technology_code` | int | Metadata | poly-Si 0, mono-Si 1, CdTe 2, CIGS 3, HIT 4, a-Si 5, CPV 6, unknown −1 | Temperature and spectral response differ by technology | Medium-low |
| `hour_sin`, `hour_cos` | – | `ts_local_start` | Local mid-hour h (fractional), angle 2π·h/24 | Repeating daily effects physics misses (shading at low sun) | Medium-low |
| `doy_sin`, `doy_cos` | – | `ts_local_start` | Local day of year d, angle 2π·(d−1)/365.25 | Seasonal effects (dust, temperature) | Medium-low |
| `temp_fc` | °C | Open-Meteo `temperature_2m_previous_day1` | As delivered | Mostly captured by `temp_cell_fc` | Low |
| `wind_fc` | m/s | Open-Meteo `wind_speed_10m_previous_day1` | km/h ÷ 3.6 | Cooling, mostly captured by `temp_cell_fc` | Low |
| `tilt_deg` | deg | Metadata | As recorded | Mostly captured by `poa_fc` | Low |
| `surface_azimuth_pvlib` | deg | Metadata | DKASC azimuth, 0 = N | Mostly captured by `poa_fc` | Low |
| `kwp` | kWp | Metadata | As recorded | Label is already normalised. May capture inverter sizing effects | Low |
| `wx_source` | int | Weather segment | 0 = ICON, 1 = ECMWF | Lets the challenger know which source a row came from | Low |
| `tracking_code` | int | Metadata | fixed 0, single_axis 1, dual_axis 2, unknown −1 | **Constant (0) in all usable rows**, kept for schema stability | None |

**Group `pr` (built, off by default)**

| Feature | Unit | Computation | Role |
|---|---|---|---|
| `pr_7d` | – | For local day D: Σ actual kWh ÷ Σ physics-expected kWh over valid days D−7 … D−1. A valid day has ≥ 6 daytime hours with label and physics value. NaN if < 3 valid days. Clipped to [0, 1.5]. Same value for every hour of D | Per-array adaptation: dust, degradation, chronic under-performance (array 20) |
| `pr_30d` | – | Same over D−30 … D−1, NaN if < 10 valid days | Slower version of `pr_7d` |

Why off by default: because the ratio is computed against physics from the same weather forecasts, it also absorbs the weather-source bias within a week, which shrinks the champion's handicap in the A′ demo. Turn on with `groups=["base", "pr"]` for ablations.

### 8.3 Inputs used by the features

| Input | Source | Description |
|---|---|---|
| GHI forecast | Open-Meteo | Total solar radiation on a horizontal surface, hourly mean |
| DNI forecast | Open-Meteo | Direct radiation perpendicular to the sun's rays |
| DHI forecast | Open-Meteo | Diffuse radiation from the sky |
| Air temperature forecast | Open-Meteo | 2 m air temperature |
| Cloud cover forecast | Open-Meteo | Total cloud fraction |
| Wind speed forecast | Open-Meteo | 10 m wind speed |
| kWp, technology, tilt, azimuth, tracking | `arrays_metadata.csv` | Static array specification |
| Site latitude, longitude, altitude, time zone | `config/pipeline.yaml` | For sun position and clear-sky model |
| Measured power (past days only) | DKASC | Only for `pr_7d`, `pr_30d` |

### 8.4 Banned columns (never features)

`power_kw_mean`, `coverage`, `qc_flags`, `target_kw`, `target_norm`, `array_no`, `source_id`, `wx_model`, `is_daylight`, `time_split`, `array_split`, and every station column.

`source_id` is banned because the model would memorise individual arrays and the cold-start experiment would become meaningless. The loader raises an error if a banned column is requested.

---

## 9. Output files

### 9.1 Snapshot folder `data/processed/<version>/`

`<version>` is the cutoff date (`--until`) for runs made with `python -m pvdata run`.

| File | Rows (2026-10-01) | Content |
|---|---|---|
| `train_table.parquet` | 1,088,544 | **Main table**, 40 columns (§9.2) |
| `labels_hourly.parquet` | 1,109,566 | Hourly labels before feature engineering: `source_id, ts_utc_end, ts_local_start, power_kw_mean, n_valid, coverage, qc_flags, target_kw, target_norm` |
| `weather_hourly.parquet` | 24,144 | Hourly forecasts after segment assignment, with `wx_source`, `wx_model` |
| `station_hourly.parquet` | 24,121 | DKASC station observations (validation only) |
| `arrays.parquet` | 46 | Array metadata as used in the build |
| `feature_spec.json` | – | Target, feature groups, categorical maps, banned list, row filter, unit and description of every column |
| `recipes.json` | – | Copy of the recipes used (§10) |
| `manifest.json` | – | Window, input file hashes (SHA-256), weather segments and gaps, grid per model, row counts, git commit |
| `qc_report.md` | – | Load statistics, QC summary, validation results |

### 9.2 `train_table` columns

| Group | Columns |
|---|---|
| Keys | `source_id`, `ts_utc_end`, `ts_local_start` |
| Array metadata | `array_no`, `kwp`, `technology`, `tracking`, `tilt_deg`, `surface_azimuth_pvlib`, `technology_code`, `tracking_code` |
| Weather forecast | `ghi_fc`, `dni_fc`, `dhi_fc`, `temp_fc`, `cloud_fc`, `wind_fc`, `wx_source`, `wx_model` |
| Solar and physics | `solar_zenith`, `solar_azimuth`, `ghi_clear`, `kt_fc`, `poa_fc`, `temp_cell_fc`, `p_phys_norm`, `is_daylight` |
| Time | `hour_sin`, `hour_cos`, `doy_sin`, `doy_cos` |
| Recent performance | `pr_7d`, `pr_30d` |
| Label and QC | `power_kw_mean`, `coverage`, `qc_flags`, `target_kw`, `target_norm` |
| Split | `time_split` (always `unassigned`), `array_split` (`train` / `coldstart` / `excluded`) |

### 9.3 Usable rows

Row filter: `target_norm.notna() & ghi_clear > 0 & ghi_fc.notna()` (daytime rows with a valid label).

| Group | Arrays | Usable rows |
|---|---|---|
| Train arrays | 25 | 290,517 |
| Cold-start arrays | 6 | 69,805 |
| **Total** | **31** | **360,322** |
| of which ICON (`wx_source` 0) | | 125,612 |
| of which ECMWF (`wx_source` 1) | | 234,710 |

Use `ghi_clear > 0`, not `is_daylight`, to select daytime: `is_daylight` drops about 850 sunrise/sunset hours that still produce power.

### 9.4 `latest.json`

```json
{"version": "2026-10-01", "until": "2026-10-01", "build_time_utc": "...", "git_commit": "..."}
```

Updated only after a snapshot passes all hard checks.

---

## 10. Splits, recipes and leakage rules

### 10.1 Recipes (`config/pipeline.yaml` → `splits.time.recipes`)

| Recipe | Train | Validation | Test | Rows (train / val / test, train arrays) | Use |
|---|---|---|---|---|---|
| `fleet_v1` | 2024-01-20 → 2024-12-31 | 2025-01-01 → 2025-04-30 | 2025-05-01 → 2025-10-01 | 101,224 / 37,733 / 42,176 | Champion v1 |
| `fleet_v2` | 2024-01-20 → 2025-12-31 | 2026-01-01 → 2026-04-30 | 2026-05-01 → 2026-10-01 | 210,977 / 37,537 / 42,003 | Challenger v2 |
| `v1_vs_v2` | – | – | 2026-05-01 → 2026-10-01 | – / – / 42,003 | Head-to-head, both models on the same rows |
| `rolling` | all days before the test window | – | 14 local days ending at `as_of` | depends on `as_of` | Production loop: weekly retrain, evaluate the last 14 days |

All recipes use feature group `base` and the 25 train arrays by default.

Rules:
- Do not retrain v1 on train + validation. v1 must only see 2024.
- Report errors per month, not only one overall number.
- v2's test period covers the dry season only. State this as a limitation.

### 10.2 Cold-start

`array_scope="coldstart"` returns the 6 held-out arrays. Protocol (from the proposal): days 1–13 of a new array use the fleet model as is. From day 14 a per-array residual correction is added, kept only if it beats the fleet model on that array. Research question: how many days (7/14/30/60) the correction needs to win.

### 10.3 Forecast issue time and leakage

| Rule | Detail |
|---|---|
| Issue time | The forecast for local day D is issued at **22:00 local on D−1** |
| Weather inputs | `_previous_day1` forecasts, issued about one day ahead. Legal |
| Static and geometric inputs | Known in advance. Legal |
| Features from measured power | May use local days ≤ D−1 only (`pr_7d`, `pr_30d` comply, tested) |
| Station observations | Never features |
| Recipe selection | Weather source and all thresholds were fixed before any model result. Choose between alternatives on validation periods only, never on test |

---

## 11. Loading data for training

### 11.1 API

```python
load_split(recipe="fleet_v1", version="latest", groups=None,
           array_scope=None, as_of=None, data_root=None) -> Split

load_table(version="latest", data_root=None) -> (DataFrame, dict)   # full table + feature_spec
```

| Argument | Values | Default |
|---|---|---|
| `recipe` | `fleet_v1`, `fleet_v2`, `v1_vs_v2`, `rolling` | `fleet_v1` |
| `version` | `latest` or a folder name in `data/processed/` | `latest` |
| `groups` | list of feature groups, e.g. `["base"]`, `["base", "pr"]` | recipe's groups (`base`) |
| `array_scope` | `train`, `coldstart`, `all` | recipe's scope (`train`) |
| `as_of` | local date string, required for `rolling` | – |

`Split` fields: `X_train, y_train, meta_train, X_val, y_val, meta_val, X_test, y_test, meta_test, feature_names, target_name, metadata`. Parts a recipe does not define are `None`. `meta_*` holds `source_id, ts_utc_end, ts_local_start, kwp, p_phys_norm, wx_source` for each row, for un-normalising and per-array or per-month reporting.

### 11.2 Examples

```python
from pvdata import load_split, load_table

# champion and challenger
v1 = load_split("fleet_v1")
v2 = load_split("fleet_v2")

# head-to-head on identical rows
h2h = load_split("v1_vs_v2")

# physics baseline (Champion 0) needs no training
baseline_pred = h2h.meta_test["p_phys_norm"]

# cold-start arrays
cs = load_split("fleet_v1", array_scope="coldstart")

# with recent-performance features
v1_pr = load_split("fleet_v1", groups=["base", "pr"])

# production loop: train on everything before the last 14 days up to 2026-09-27
roll = load_split("rolling", as_of="2026-09-27")

# monthly error report
import numpy as np
err = (model.predict(v1.X_test) - v1.y_test) ** 2
monthly_rmse = np.sqrt(err.groupby(v1.meta_test["ts_local_start"].dt.to_period("M")).mean())

# full table for analysis
df, spec = load_table()
```

---

## 12. Command-line interface

Run from the repository root: `python -m pvdata <command>`. Global options: `--config <path>`, `--data-root <path>`, `-v`.

| Command | Arguments | Does |
|---|---|---|
| `run` | `[--until YYYY-MM-DD] [--skip-fetch] [--force] [--overwrite]` | Full pipeline: fetch DKASC, fetch weather, build `processed/<until>/`, validate, publish. Default `--until` = yesterday (Alice Springs) |
| `fetch-dkasc` | `[--start] [--end]` (local dates) | Download DKASC API chunks. Default start = day after the last complete chunk, default end = yesterday |
| `fetch-meteo` | `[--start] [--end]` | Download Open-Meteo for every configured segment |
| `build` | `--version <name> [--end] [--force] [--overwrite]` | Build a snapshot without fetching or publishing |
| `validate` | `--version <name>` | Run checks, append results to `qc_report.md`. Exit code ≠ 0 on hard failure |
| `sample` | `--version <name> --start --end` | Export a slice as CSV + `sample.xlsx` for inspection |

`--force` rebuilds the `interim/` caches. `--overwrite` allows rebuilding an existing version folder. The build is deterministic: the same raw inputs and the same `--until` give the same table.

---

## 13. Weekly automation

### 13.1 What runs

`scripts/run_weekly.sh`, inside the Docker image:

| Step | Action | On failure |
|---|---|---|
| 1 | `aws s3 sync $PV_S3_URI/raw/ data/` | Stop |
| 2 | `python -m pvdata run` (cutoff = yesterday, local) | Stop. Nothing is uploaded, `latest.json` unchanged |
| 3 | Upload new raw: `dkasc_api_5min/`, `open_meteo_raw/` → `$PV_S3_URI/raw/` (no `--delete`) | Stop |
| 4 | Upload `processed/<version>/` and `processed/latest.json` | Stop |

### 13.2 Schedule

| Time zone | Time |
|---|---|
| Alice Springs (ACST) | Sunday 01:00 |
| UTC | Saturday 15:30 → cron `30 15 * * 6` |
| Vietnam (ICT) | Saturday 22:30 |

The cutoff is Saturday (local), so each snapshot contains complete days only. A missed week is caught up automatically by the next run.

### 13.3 Docker

```bash
docker build -t pvdata .
docker run --rm -e PV_S3_URI=s3://<bucket>/<prefix> --entrypoint scripts/run_weekly.sh pvdata
# any single command:
docker run --rm -v "$PWD/data:/app/data" pvdata validate --version 2026-10-01
```

Credentials come from the runtime (IAM role or AWS CLI configuration), never from files. Suitable hosts: ECS scheduled task with EventBridge, EC2 cron, or a GitHub Actions scheduled workflow.

### 13.4 S3 layout

```
s3://<bucket>/<prefix>/
├── raw/                                   mirror of data/ inputs
│   ├── Alice_Springs_2024.csv, _2025.csv, _2026.csv
│   ├── arrays_metadata.csv, array_overrides.csv
│   ├── dkasc_api_5min/        (chunks + source_names.csv)
│   └── open_meteo_raw/
└── processed/
    ├── 2026-10-01/ ...                    one folder per weekly snapshot (~45 MB)
    └── latest.json
```

### 13.5 First-time setup (not done yet)

1. Create the bucket and an IAM role with read/write on the prefix.
2. Upload the current inputs: `aws s3 sync data/ s3://<bucket>/<prefix>/raw/ --exclude "interim/*" --exclude "processed/*" --exclude "samples/*"`.
3. Optionally upload the existing snapshot: `aws s3 sync data/processed/ s3://<bucket>/<prefix>/processed/`.
4. Build the image, schedule it with `PV_S3_URI` set.
5. Training jobs read `processed/latest.json`, download that folder, then call `load_split(..., data_root=<local copy>)`.

Metadata CSVs are read from `data/` at run time. After editing them in git, also upload them to `raw/`.

---

## 14. Configuration reference (`config/pipeline.yaml`)

### 14.1 Site and inputs

| Key | Value | Meaning |
|---|---|---|
| `data_root` | `data` | Root of all data paths |
| `site.latitude`, `site.longitude`, `site.altitude_m`, `site.tz` | −23.7624, 133.8754, 560, `Australia/Darwin` | Location for solar geometry |
| `dkasc.yearly_files` | 3 file names | Yearly DKASC files, exact names |
| `dkasc.api_dir`, `dkasc.api_name_map` | `dkasc_api_5min`, `.../source_names.csv` | API chunk folder and column map |
| `dkasc.weather_source_id` | 101 | Station meter id |
| `dkasc.metadata_file`, `dkasc.overrides_file` | `arrays_metadata.csv`, `array_overrides.csv` | Metadata |
| `open_meteo.endpoint`, `raw_dir`, `variables`, `lead_suffix` | see file | Weather download |
| `open_meteo.segments` | list of `{model, wx_source, from_local, to_local}` | Which model covers which local dates. Required, no implicit `best_match` |
| `window.start` | `auto` | First full local day with complete weather coverage |
| `window.end` | `2026-02-11` | Default end for `build` without `--end`. `run` always passes `--until` |

### 14.2 QC

| Key | Value | Meaning |
|---|---|---|
| `qc.neg_clip_kw` | −0.5 | Small negatives clipped to 0, larger ones invalid |
| `qc.max_ratio_kwp` | 1.2 | Above 1.2 × kWp is invalid |
| `qc.stuck_min_records` | 6 | Consecutive identical non-zero daytime values |
| `qc.min_coverage` | 0.8333 | ≥ 10 of 12 valid records per hour |
| `qc.daytime_zenith_max` | 85 | Daytime definition for QC |
| `qc.near_zero_kwh_per_kwp_day` | 0.5 | Array-level near-zero exclusion threshold |

### 14.3 Features and splits

| Key | Value |
|---|---|
| `features.pr_min_valid_hours_per_day` | 6 |
| `features.pr_windows` | `pr_7d`: 7 days, min 3. `pr_30d`: 30 days, min 10 |
| `splits.array.coldstart_source_ids` | 105, 81, 79, 92, 56, 68 |
| `splits.time.recipes` | §10.1 |

### 14.4 Physics

| Key | Value |
|---|---|
| `physics.system_efficiency` | 0.86 |
| `physics.t_ref_c` | 25 |
| `physics.gamma_per_c` | poly-Si −0.0040, mono-Si −0.0039, CdTe −0.0028, CIGS −0.0036, HIT −0.0026, a-Si −0.0020, CPV null (not modelled) |

Changing any value only requires a rebuild, no code change.

---

## 15. Validation and tests

### 15.1 Hard checks (any failure blocks publishing)

| Check | Rule |
|---|---|
| Unique key | (`source_id`, `ts_utc_end`) unique |
| No station columns | No observed-weather column in `train_table` |
| Lead | Only `_previous_day1` weather variables |
| Time alignment | Station GHI vs forecast GHI: shift 0 h is best among −2 … +2 h, and offset 0 is best in the half-hour table. r and RMSE reported only |
| Weather segments | Every row's `wx_source`/`wx_model` matches its segment, no other model present |
| Coverage | Window start and end inside the coverage of their segment's model |
| Feature spec | All listed features exist, none banned, all numeric |

Snapshot 2026-10-01: all hard checks pass. Alignment r = 0.941, RMSE 123 W/m² at shift 0.

### 15.2 Soft checks (reported only)

Weather gaps inside the window (none), per-array correlation of `p_phys_norm` with `target_norm` (flags r < 0.8: source 72 at 0.75), azimuth sanity (east array peaks at 11:00, west at 14:00, north-facing at 12:00), kWh/kWp/day range, rows per array per month, `target_norm` distribution, excluded and incomplete-metadata arrays.

### 15.3 Tests

`pytest -q` runs 74 tests: parsing and deduplication, QC rules, aggregation, metadata, regressions from earlier bugs, weather model pinning and segment boundaries, coverage with gaps, physics value, `pr` leakage and minimum-day rules, loader filters and banned columns, DKASC ingest gate (mocked HTTP), and the `run` command publishing logic.

### 15.4 Independent verification of snapshot 2026-10-01

Recomputed outside the pipeline on 2026-10-08: weather values match the raw JSON of each model exactly; no row has the wrong source; `p_phys_norm`, time features and `pr_7d/30d` match their formulas (300 random array-days for `pr`); labels match the previous build on all overlapping hours; 2026-09-26 → 2026-10-01 have 24 hours × 31 arrays per day.

---

## 16. Known issues and open decisions

| Id | Issue | Status / proposal |
|---|---|---|
| B1 | **Dead days not flagged.** On 215 array-days (2,060 hourly rows, 0.6% of usable rows) an array produced < 5% of its physics expectation while the fleet ran normally. Mostly source 72 (array 26, 88 days of intermittent outages), 212 (array 25, January to May 2024 after meter replacement), 74 (array 31, two outage periods). Cause: `day_zero` requires power exactly 0, while dead inverters report about 0.01 kW | **Proposed:** change `day_zero` to power < 2% of kWp under the same conditions. Applies to all splits and future weekly runs. Awaiting approval |
| B1b | Partial-output days (5–60% of expectation): array 11 (116 days), 26, 17, and array 20 (intended fault case) | Keep. Cannot be told apart from soiling or shading. `pr_7d` exists for this |
| A3 | `fetch-meteo` re-downloads the current year into a new file every week | Correct (newest file wins within a model), only ~300 KB/week. Optimise later |
| D2 | `stuck` flags about 90k tiny values (0.01–0.08 kW) at dawn and dusk | Proposed minimum of 5% kWp. Open |
| D4 | Site coordinates approximate | Verify on the DKASC map |
| D5 | Promotion rule for v1 vs v2 | Must be pre-registered before training. Draft: shadow ≥ 14 days, nRMSE ≥ 3% lower, Diebold–Mariano p < 0.05 or bootstrap CI excluding 0, no subgroup (technology, orientation) worse by > 5% |
| – | `tracking_code` is constant in usable rows | Kept for schema stability |
| – | S3 automation never run end to end | First-time setup in §13.5 |

---

## 17. Decision log

| Date | Decision |
|---|---|
| 2026-09-26 | Pipeline v1 built. Nine bugs fixed (sun position, POA vectorisation, validity windows, stuck check, memory) |
| 2026-10-04 | `best_match` found to switch model on 2025-10-01. Models pinned. Weather source chosen on the clean period only |
| 2026-10-04 | 6 cold-start arrays fixed. 9A (only CIGS) not held out |
| 2026-10-08 | One dataset: ICON ≤ 2024-12-31, ECMWF 0.25° from 2025-01-01, `wx_source`. Champion evaluated on ECMWF (deliberate) |
| 2026-10-08 | Data end 2026-10-01. Test periods end 2025-10-01 (v1) and 2026-10-01 (v2) |
| 2026-10-08 | Forecast issue time 22:00 local on D−1. `pr_7d/30d` built, off by default |
| 2026-10-08 | Physics baseline with PVWatts-style losses (0.86) and technology γ |
| 2026-10-08 | Alignment hard check: shift 0 must be best, no r threshold |
| 2026-10-08 | Weekly Sunday job. Raw append-only, processed rebuilt in full, one snapshot per run |
| 2026-10-08 | Splits defined as recipes and applied by the loader |

**Rejected (do not re-raise):** `best_match`; ECMWF 0.1° (`ecmwf_ifs`, history only from 2025-10 and gaps); choosing the weather source on test periods; two datasets or a two-source long table; incremental processed builds; daily ingest; holding out 9A; retraining v1 on train + validation; selecting daytime with `is_daylight`; editing raw files; wind as a second field.
