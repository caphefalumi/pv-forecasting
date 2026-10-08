"""Export a human-readable sample of the training data for teammates.

Sample export generation:
- Filter train_table (non-excluded arrays) + matching rows of other tables.
- Write CSVs with encoding="utf-8-sig" (Excel-safe for °, ²).
- Write one sample.xlsx (openpyxl) with sheets:
    arrays, train_table, labels_hourly, weather_hourly, station_hourly, README
  Timestamps are timezone-naive datetime cells (UTC or local per column).
  Header row frozen, autofilter on. Fails loudly if any sheet > 1,000,000 rows.
- Write README.md with full data dictionary, time convention, QC codes, attribution.
- Parquet stays the canonical format in data/processed/<version>/; not touched here.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

from .config import PipelineConfig

logger = logging.getLogger(__name__)

MAX_XLSX_ROWS = 1_000_000

DATA_DICTIONARY = """
## Data Dictionary

| Column | Unit | Meaning | Source |
|--------|------|---------|--------|
| source_id | — | Unique DKASC array identifier | DKASC |
| ts_utc_end | UTC | Hour-ending timestamp (e.g. 03:00 UTC = hour 02:00–03:00 UTC) | Derived |
| ts_local_start | ACST (UTC+09:30) | Local start of the hour interval | Derived |
| array_no | — | Array number in DKASC documentation | DKASC metadata |
| kwp | kWp | Rated peak power of the array | DKASC metadata |
| technology | — | PV cell technology (e.g. poly-Si, mono-Si) | DKASC metadata |
| tracking | — | Mounting type: fixed, single_axis, or dual_axis | DKASC metadata |
| tilt_deg | degrees | Panel tilt from horizontal (fixed arrays only) | DKASC metadata |
| surface_azimuth_pvlib | degrees | Panel azimuth in pvlib convention (0=N, 90=E, 180=S, 270=W) | Derived from DKASC |
| ghi_fc | W/m² | Forecast global horizontal irradiance (day-ahead) | Open-Meteo |
| dni_fc | W/m² | Forecast direct normal irradiance (day-ahead) | Open-Meteo |
| dhi_fc | W/m² | Forecast diffuse horizontal irradiance (day-ahead) | Open-Meteo |
| temp_fc | °C | Forecast 2m air temperature (day-ahead) | Open-Meteo |
| cloud_fc | % | Forecast cloud cover (day-ahead) | Open-Meteo |
| wind_fc | m/s | Forecast 10m wind speed (day-ahead, converted from km/h) | Open-Meteo |
| solar_zenith | degrees | Solar zenith angle at hour midpoint | pvlib |
| solar_azimuth | degrees | Solar azimuth angle at hour midpoint | pvlib |
| ghi_clear | W/m² | Clear-sky GHI (Ineichen model) at hour midpoint | pvlib |
| kt_fc | — | Clearness index: ghi_fc / ghi_clear (clipped [0, 1.5]) | Derived |
| poa_fc | W/m² | Forecast plane-of-array irradiance (haydavies model) | pvlib + Open-Meteo |
| temp_cell_fc | °C | Forecast cell temperature (SAPM open_rack_glass_polymer) | pvlib |
| is_daylight | bool | True when solar zenith < 90° | pvlib |
| power_kw_mean | kW | Mean active power over valid 5-min records in the hour | DKASC |
| coverage | — | Fraction of valid 5-min records (0–1); 1.0 = 12/12 records valid | Derived |
| qc_flags | — | Comma-joined QC flag codes (empty = OK); see QC Codes below | Derived |
| target_kw | kW | Training target (NaN if coverage < 10/12, excluded, or outside validity) | Derived |
| target_norm | — | target_kw / kwp (NaN if kwp unknown) | Derived |
| time_split | — | Time-based split label (unassigned until team confirms) | Derived |
| array_split | — | Array-based split label (unassigned until team confirms) | Derived |
"""

QC_CODES = """
## QC Flag Codes

| Code | Meaning |
|------|---------|
| missing | Value is NaN (5-min record missing) |
| neg | Value < -0.5 kW (invalid; below inverter standby range) |
| over_kwp | Value > 1.2 × kwp (physically implausible) |
| stuck | ≥ 6 consecutive identical non-zero values during daytime (sensor stuck) |
| day_zero | Power == 0 while GHI > 300 W/m² and fleet median > 0.3 (array-specific outage) |

Notes:
- Values in [-0.5, 0) kW are clipped to 0 (inverter standby) — no flag added.
- Records with any invalidating flag (missing, neg, over_kwp, stuck, day_zero) do not contribute to power_kw_mean or coverage.
"""

ATTRIBUTION = """
## Data Attribution

### DKASC Data
This sample uses data from the **DKA Solar Centre** (Alice Springs, NT, Australia).
Copyright © Desert Knowledge Australia. Used under the terms of the DKA Solar Centre data access agreement.
**Do not redistribute the raw DKASC data.**

### Open-Meteo Data
Weather forecast data from **Open-Meteo** (https://open-meteo.com/).
© Open-Meteo contributors, licensed under CC BY 4.0.
API used: Previous Runs API (`https://previous-runs-api.open-meteo.com/v1/forecast`),
`_previous_day1` suffix = forecast issued ~24 h before the valid hour.
"""

TIME_CONVENTION = """
## Time Convention

- All timestamps are in **UTC**.
- `ts_utc_end` is **hour-ending**: a record with `ts_utc_end = 2025-06-01 03:00 UTC`
  covers the interval **02:00–03:00 UTC** (i.e., 11:30–12:30 ACST).
- `ts_local_start` = `ts_utc_end - 1h` converted to **Australia/Darwin (ACST = UTC+09:30)**.
- Open-Meteo `_previous_day1` radiation variables are also **preceding-hour means**,
  so `ts_utc_end = 03:00 UTC` aligns with `ghi_fc` representing the 02:00–03:00 UTC interval.
- In sample.xlsx timestamps are timezone-naive datetime cells:
  columns named ts_utc_* contain naive UTC values; ts_local_* contain naive local (ACST) values.
  Number format in xlsx: yyyy-mm-dd hh:mm.
"""

# Sheets to export: (sheet_name, parquet_filename_or_None, df_kwarg_or_None)
# None means derive from already-loaded data.
XLSX_SHEETS = ["arrays", "train_table", "labels_hourly", "weather_hourly", "station_hourly"]


def _strip_tz(df: pd.DataFrame) -> pd.DataFrame:
    """Return a copy of df with all tz-aware datetime columns made tz-naive.

    Columns starting with ts_utc_* → strip tz (values are already UTC).
    Columns starting with ts_local_* → strip tz (values are already local).
    Other datetime columns → strip tz.
    """
    df = df.copy()
    for col in df.columns:
        if pd.api.types.is_datetime64_any_dtype(df[col]):
            try:
                df[col] = df[col].dt.tz_localize(None)
            except TypeError:
                df[col] = df[col].dt.tz_convert(None)
    return df


def _write_sheet(ws, df: pd.DataFrame) -> None:
    """Write DataFrame to an openpyxl worksheet with frozen header + autofilter.

    Timestamps are written as native datetime cells with number format yyyy-mm-dd hh:mm.
    """
    from openpyxl.utils import get_column_letter

    if len(df) > MAX_XLSX_ROWS:
        raise ValueError(
            f"Sheet would have {len(df):,} rows, exceeding the limit of {MAX_XLSX_ROWS:,}. "
            "Reduce the sample window or split into multiple sheets."
        )

    # Write header
    for col_idx, col_name in enumerate(df.columns, start=1):
        ws.cell(row=1, column=col_idx, value=col_name)

    # Identify datetime columns
    dt_cols = {
        col: col_idx + 1
        for col_idx, col in enumerate(df.columns)
        if pd.api.types.is_datetime64_any_dtype(df[col])
    }

    # Write data rows
    for row_idx, row in enumerate(df.itertuples(index=False), start=2):
        for col_idx, value in enumerate(row, start=1):
            cell = ws.cell(row=row_idx, column=col_idx)
            if pd.isna(value) if not isinstance(value, str) else False:
                cell.value = None
            else:
                cell.value = value
            # Apply datetime format
            if col_idx in dt_cols.values() and isinstance(value, datetime):
                cell.number_format = "yyyy-mm-dd hh:mm"

    # Freeze header row
    ws.freeze_panes = ws["A2"]

    # Autofilter on header row
    if df.shape[1] > 0:
        last_col = get_column_letter(df.shape[1])
        ws.auto_filter.ref = f"A1:{last_col}1"


def _write_readme_sheet(ws, readme_text: str) -> None:
    """Write the README text into a sheet, one line per row."""
    ws.freeze_panes = ws["A2"]
    for row_idx, line in enumerate(readme_text.splitlines(), start=1):
        ws.cell(row=row_idx, column=1, value=line)


def _load_table(out_dir: Path, name: str) -> Optional[pd.DataFrame]:
    """Load a parquet table from the processed/ version folder, or None if missing."""
    path = out_dir / f"{name}.parquet"
    if path.exists():
        return pd.read_parquet(path)
    return None


def export_sample(
    version: str,
    start: str,
    end: str,
    cfg: PipelineConfig,
) -> None:
    """Export CSV + xlsx sample for teammates.

    Args:
        version: Build version (e.g. "v0").
        start: Sample start date (local YYYY-MM-DD, inclusive).
        end: Sample end date (local YYYY-MM-DD, inclusive).
        cfg: Pipeline config.
    """
    import pytz
    tz = pytz.timezone(cfg.tz)

    out_dir = cfg.path("processed") / version
    if not out_dir.exists():
        raise FileNotFoundError(f"Version folder not found: {out_dir}. Run build first.")

    sample_dir = cfg.path("samples") / f"{version}_{start}_{end}"
    sample_dir.mkdir(parents=True, exist_ok=True)

    # Load tables
    tt = pd.read_parquet(out_dir / "train_table.parquet")
    _arr = _load_table(out_dir, "arrays")
    arrays_meta = _arr if _arr is not None else pd.DataFrame()
    _lh = _load_table(out_dir, "labels_hourly")
    labels_hourly = _lh if _lh is not None else pd.DataFrame()
    _wh = _load_table(out_dir, "weather_hourly")
    weather_hourly = _wh if _wh is not None else pd.DataFrame()
    _sh = _load_table(out_dir, "station_hourly")
    station_hourly = _sh if _sh is not None else pd.DataFrame()

    # Filter to non-excluded arrays
    if not arrays_meta.empty and "excluded" in arrays_meta.columns:
        ok_ids = set(arrays_meta[~arrays_meta["excluded"].fillna(False)]["source_id"].astype(int))
        tt = tt[tt["source_id"].isin(ok_ids)]

    # Filter by date window
    start_utc = pd.Timestamp(start, tz=tz).tz_convert("UTC")
    end_utc = (pd.Timestamp(end, tz=tz) + pd.Timedelta(hours=23, minutes=59, seconds=59)).tz_convert("UTC")

    def _filter_ts(df: pd.DataFrame, ts_col: str) -> pd.DataFrame:
        if df.empty or ts_col not in df.columns:
            return df
        ts = pd.to_datetime(df[ts_col], utc=True)
        return df[(ts >= start_utc) & (ts <= end_utc)]

    tt = _filter_ts(tt, "ts_utc_end")
    labels_windowed = _filter_ts(labels_hourly, "ts_utc_end")
    weather_windowed = _filter_ts(weather_hourly, "ts_utc_end")
    station_windowed = _filter_ts(station_hourly, "ts_utc_end")

    logger.info("Sample: %d rows, %d arrays", len(tt), tt["source_id"].nunique())

    # ---- Write CSVs (utf-8-sig for Excel compatibility) ----
    tt_csv = sample_dir / "train_table.csv"
    tt.to_csv(tt_csv, index=False, encoding="utf-8-sig")
    logger.info("Wrote %s (%.1f MB)", tt_csv.name, tt_csv.stat().st_size / 1e6)

    if not arrays_meta.empty:
        arr_csv = sample_dir / "arrays.csv"
        arrays_meta.to_csv(arr_csv, index=False, encoding="utf-8-sig")
        logger.info("Wrote %s", arr_csv.name)

    # ---- Write xlsx ----
    _write_xlsx(
        sample_dir=sample_dir,
        version=version, start=start, end=end,
        tables={
            "arrays": arrays_meta,
            "train_table": tt,
            "labels_hourly": labels_windowed,
            "weather_hourly": weather_windowed,
            "station_hourly": station_windowed,
        },
    )

    # ---- Write README ----
    readme_text = _build_readme(version, start, end, tt, arrays_meta)
    readme_path = sample_dir / "README.md"
    with open(readme_path, "w", encoding="utf-8") as fh:
        fh.write(readme_text)
    logger.info("Wrote README.md")

    print(f"\nSample written to: {sample_dir}")
    print(f"  train_table.csv: {len(tt):,} rows, {tt['source_id'].nunique()} arrays")
    print(f"  Files: {sorted(f.name for f in sample_dir.iterdir())}")


def _write_xlsx(
    sample_dir: Path,
    version: str,
    start: str,
    end: str,
    tables: dict[str, pd.DataFrame],
) -> None:
    """Write sample.xlsx with one sheet per table plus a README sheet."""
    try:
        import openpyxl
    except ImportError as e:
        raise ImportError(
            "openpyxl is required for xlsx export. Install it with: pip install openpyxl"
        ) from e

    wb = openpyxl.Workbook()
    wb.remove(wb.active)  # remove default empty sheet

    sheet_order = ["arrays", "train_table", "labels_hourly", "weather_hourly", "station_hourly"]

    for sheet_name in sheet_order:
        df = tables.get(sheet_name)
        ws = wb.create_sheet(title=sheet_name)
        if df is None or df.empty:
            ws.cell(row=1, column=1, value="(no data)")
            continue
        df_naive = _strip_tz(df)
        _write_sheet(ws, df_naive)
        logger.info("xlsx sheet '%s': %d rows × %d cols", sheet_name, len(df), len(df.columns))

    # README sheet
    readme_ws = wb.create_sheet(title="README")
    _tt_table = tables.get("train_table")
    _arr_table = tables.get("arrays")
    readme_text = _build_readme(
        version, start, end,
        _tt_table if _tt_table is not None else pd.DataFrame(),
        _arr_table if _arr_table is not None else pd.DataFrame(),
    )
    _write_readme_sheet(readme_ws, readme_text)

    xlsx_path = sample_dir / "sample.xlsx"
    wb.save(xlsx_path)
    logger.info("Wrote %s (%.1f MB)", xlsx_path.name, xlsx_path.stat().st_size / 1e6)


def _build_readme(
    version: str,
    start: str,
    end: str,
    tt: pd.DataFrame,
    arrays_meta: pd.DataFrame,
) -> str:
    n_arrays = tt["source_id"].nunique() if not tt.empty and "source_id" in tt.columns else 0
    return f"""# PV Training Data Sample — {version} ({start} → {end})

**Generated:** {datetime.now(timezone.utc).isoformat()}
**Version:** {version}
**Sample window:** {start} → {end} (local Alice Springs time, ACST = UTC+09:30)

## Files

| File | Description |
|------|-------------|
| `train_table.csv` | Main training table: one row per (array, UTC hour) |
| `arrays.csv` | Array metadata (static properties) |
| `sample.xlsx` | All tables + README in one Excel workbook |
| `README.md` | This file |

## Row Counts

- train_table: {len(tt):,} rows
- Arrays in sample: {n_arrays}

{TIME_CONVENTION}
{DATA_DICTIONARY}
{QC_CODES}
{ATTRIBUTION}
"""
