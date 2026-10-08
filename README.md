# PV Data & Feature Pipeline

## 1. Purpose

The PV Feature Pipeline prepares analysis-ready, leakage-free datasets for day-ahead hourly solar photovoltaic (PV) power forecasting. Ground-truth target labels are sourced from 46 commercial and research PV arrays at the Desert Knowledge Australia Solar Centre (DKASC) in Alice Springs. Input weather features are derived from numerical weather prediction (NWP) forecasts provided by Open-Meteo Previous Runs, aligned with solar geometry, clear-sky irradiance models, and physical PV cell performance baselines.

---

## 2. Repository Layout

```text
├── config/
│   └── pipeline.yaml           # Pipeline configuration (paths, QC, physics, splits, recipes)
├── pvdata/
│   ├── __init__.py             # Public API exports (load_split, load_table, Split)
│   ├── __main__.py             # CLI entrypoint implementing pipeline subcommands
│   ├── aggregate.py            # Aggregation from 5-min intervals to UTC hour-ending
│   ├── build.py                # Snapshot build orchestrator producing 40-column table
│   ├── config.py               # Typed configuration dataclasses and YAML parser
│   ├── dataset.py              # Dataset loader providing leakage-free train/val/test splits
│   ├── features.py             # Solar position, Ineichen clear-sky, Hay-Davies POA, SAPM cell temp
│   ├── ingest_dkasc.py         # Incremental DKASC API 5-min data ingestion with completeness gate
│   ├── io_dkasc.py             # Raw DKASC yearly CSV reader and deduplication engine
│   ├── io_openmeteo.py         # Open-Meteo forecast fetcher and segmented cache manager
│   ├── metadata.py             # Array metadata loader, Excel normalization, and overrides
│   ├── physics.py              # Physical baseline PV power calculation (p_phys_norm)
│   ├── pr.py                   # Rolling Performance Ratio (pr_7d, pr_30d) calculation
│   ├── qc.py                   # Quality control flagging on raw 5-minute power records
│   ├── sample.py               # Human-readable sample exporter (CSV / Excel format)
│   └── validate.py             # Hard and soft validation checks on built snapshots
├── tests/                      # Automated test suite (regressions, features, pipeline integration)
├── scripts/
│   └── run_weekly.sh           # Weekly production automation script with S3 synchronization
├── Dockerfile                  # Container definition for pipeline execution
├── .dockerignore               # Docker ignore rules (excludes data, git, virtualenv, caches)
├── .gitignore                  # Git ignore rules protecting measurement data
├── requirements.txt            # Python package dependencies
└── README.md                   # Team documentation
```

---

## 3. Setup

### Virtual Environment Setup

**Windows PowerShell:**
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

**Linux / macOS:**
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Running Tests

Verify your environment by running the test suite:
```bash
pytest -q
```

---

## 4. Data Layout

```text
data/
├── Alice_Springs_2024.csv             # Raw yearly DKASC measurements (never committed)
├── Alice_Springs_2025.csv             # Raw yearly DKASC measurements (never committed)
├── Alice_Springs_2026.csv             # Raw yearly DKASC measurements (never committed)
├── arrays_metadata.csv                # Array metadata and specifications (COMMITTED)
├── array_overrides.csv                # Curated array metadata overrides (COMMITTED)
├── dkasc_api_5min/
│   ├── source_names.csv               # DKASC column mapping dictionary (COMMITTED)
│   └── *.csv                          # Incremental 3-day 5-min API chunks (never committed)
├── open_meteo_raw/                    # Raw Open-Meteo JSON responses (never committed)
├── interim/                           # Intermediate processed Parquet caches (never committed)
├── processed/
│   ├── <version>/                     # Self-contained immutable snapshot (never committed)
│   │   ├── train_table.parquet        # Main 40-column hourly training dataset
│   │   ├── arrays.parquet             # Metadata for all 46 DKASC array meters
│   │   ├── labels_hourly.parquet      # Hourly aggregated ground-truth PV power
│   │   ├── weather_hourly.parquet     # Pinned day-ahead weather forecasts
│   │   ├── station_hourly.parquet     # Observed on-site weather station measurements
│   │   ├── feature_spec.json          # Target definition, banned columns, feature dictionary
│   │   ├── recipes.json               # Registered evaluation recipes
│   │   ├── manifest.json              # Hashes (SHA-256), git commit, and grid metadata
│   │   └── qc_report.md               # Quality control summary and validation results
│   └── latest.json                    # Pointer to current active version (never committed)
```

**Commitment Rules:**
- **Committed to Git:** Metadata CSVs (`arrays_metadata.csv`, `array_overrides.csv`) and the API column dictionary (`dkasc_api_5min/source_names.csv`).
- **Never Committed:** All raw DKASC measurement data, API chunk CSVs, weather caches, interim parquets, processed snapshots, and copyright documentation PDFs.

---

## 5. Weather Source Segments

To provide long-term continuity without mixing numerical models within the same timeframe, the pipeline segments historical day-ahead weather forecasts into two pinned sources:
- **`icon_seamless`** (`wx_source = 0`): Used for historical dates up to `2024-12-31`.
- **`ecmwf_ifs025`** (`wx_source = 1`): Used from `2025-01-01` onward.

The model boundary transitions cleanly at local midnight with a 1-day fetch buffer to prevent edge artifacts. The integer indicator `wx_source` is included in the feature set so downstream machine learning models can account for baseline distribution shifts between weather models.

---

## 6. Commands

Execute the pipeline using `python -m pvdata <subcommand>`:

| Command | Arguments | Description |
|---|---|---|
| `run` | `[--until YYYY-MM-DD]`<br>`[--skip-fetch]`<br>`[--force]`<br>`[--overwrite]` | Runs full pipeline end-to-end: fetches DKASC and NWP, builds snapshot, validates, and updates `latest.json`. Default `--until` is yesterday (local Darwin). |
| `fetch-dkasc` | `[--start YYYY-MM-DD]`<br>`[--end YYYY-MM-DD]` | Fetches incremental 5-min DKASC data from public API into `data/dkasc_api_5min/` in 3-day chunks. |
| `fetch-meteo` | `[--start YYYY-MM-DD]`<br>`[--end YYYY-MM-DD]` | Fetches day-ahead weather predictions from Open-Meteo Previous Runs into `data/open_meteo_raw/`. |
| `build` | `--version <version>`<br>`[--end YYYY-MM-DD]`<br>`[--force]`<br>`[--overwrite]` | Builds a versioned training table snapshot under `data/processed/<version>/`. |
| `validate` | `--version <version>` | Runs hard integrity checks and soft sanity checks on a processed snapshot. |
| `sample` | `--version <version>`<br>`--start YYYY-MM-DD`<br>`--end YYYY-MM-DD` | Exports a human-readable slice of data into CSV and Excel (`sample.xlsx`) for inspection. |

---

## 7. Weekly Automation

Production snapshots are generated automatically using [scripts/run_weekly.sh](file:///c:/Swinburne%20University/COS40007%20-%20Artificial%20Intelligence%20Engineering/PROJECT/code/scripts/run_weekly.sh).

### Cloud Storage (S3) Layout
- **Raw Data:** `${PV_S3_URI}/raw/` (syncs `dkasc_api_5min/` and `open_meteo_raw/`)
- **Snapshots:** `${PV_S3_URI}/processed/<version>/` and `${PV_S3_URI}/processed/latest.json`

### Schedule
Runs weekly on **Sunday at 01:00 ACST (Alice Springs)** = **Saturday at 15:30 UTC**.

### Docker Execution
The pipeline image can be run in cloud automation environments without hardcoded AWS credentials:
```bash
docker run --entrypoint scripts/run_weekly.sh -e PV_S3_URI=s3://my-bucket/pv-forecast <image>
```

---

## 8. Using the Data

### Loading Splits in Python

Teammates can load leakage-free train, validation, and test sets with clean separation of cold-start arrays in 5 lines:

```python
from pvdata import load_split

# Load pre-registered train/val/test split
split = load_split("fleet_v1")

X_train, y_train = split.X_train, split.y_train
X_val, y_val     = split.X_val, split.y_val
X_test, y_test   = split.X_test, split.y_test
```

### Registered Recipes

| Recipe | Train Period | Val Period | Test Period | Target Array Scope |
|---|---|---|---|---|
| `fleet_v1` | 2024-01-20 → 2024-12-31 | 2025-01-01 → 2025-04-30 | 2025-05-01 → 2025-10-01 | Training fleet (cold-start held out) |
| `fleet_v2` | 2024-01-20 → 2025-12-31 | 2026-01-01 → 2026-04-30 | 2026-05-01 → 2026-10-01 | Training fleet (cold-start held out) |
| `v1_vs_v2` | — | — | 2026-05-01 → 2026-10-01 | Common benchmark test window |
| `rolling` | Local dates `< test_start` | — | Past `test_days` up to `as_of` | Dynamic evaluation window |

### Converting Normalized Predictions to kW

The target variable `target_norm` represents capacity-normalized active power ($0 \le y \le 1$ under standard conditions):
$$\text{Power (kW)} = \text{target\_norm} \times \text{kwp}$$

Each split metadata table (`split.meta_train`, `split.meta_test`) provides array capacities (`kwp`) and identifiers (`source_id`).

### Feature Groups
- **`base` (default):** 23 core features including NWP irradiance forecasts (`ghi_fc`, `dni_fc`, `dhi_fc`), temperature, wind, cloud cover, sun angles (`solar_zenith`, `solar_azimuth`), Ineichen clear-sky GHI, Hay-Davies plane-of-array irradiance (`poa_fc`), SAPM cell temperature, physical baseline `p_phys_norm`, capacity `kwp`, encoded categorical codes (`technology_code`, `tracking_code`), and cyclical time encodings (`hour_sin`, `hour_cos`, `doy_sin`, `doy_cos`).
- **`pr` (optional):** Rolling performance ratio indicators (`pr_7d`, `pr_30d`) reflecting recent soiling or degradation over days $[D-W, D-1]$. Enable via `load_split("fleet_v1", groups=["base", "pr"])`.

For the complete schema, units, and column descriptions, refer to `feature_spec.json` within the snapshot directory.

---

## 9. Data Licence Note

DKASC Alice Springs data is published by Desert Knowledge Australia under copyright. All measurement CSVs, API extracts, and derived power datasets are for internal educational and research use within this project. Do not commit or publicly redistribute raw measurement files.
