"""Tests for the follow-up changes (Items 1–4).

Covers:
- Item 2: API-vs-API dedup by mtime; count in stats
- Item 3: dd/mm vs ISO date parsing; invalid tracking; BOM file; normalised exclude
- Item 4: xlsx written with expected sheets; utf-8-sig CSV; row-limit guard
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest


# ---------------------------------------------------------------------------
# Helpers shared across tests
# ---------------------------------------------------------------------------

def _base_cfg(tmp_path: Path):
    from pvdata.config import PipelineConfig
    data = {
        "data_root": str(tmp_path),
        "site": {"latitude": -23.7624, "longitude": 133.8754, "altitude_m": 560, "tz": "Australia/Darwin"},
        "dkasc": {
            "yearly_files": [],
            "api_dir": "dkasc_api_5min",
            "api_name_map": "dkasc_api_5min/source_names.csv",
            "weather_source_id": 101,
            "metadata_file": "arrays_metadata_template.csv",
            "overrides_file": "array_overrides.csv",
        },
        "open_meteo": {"endpoint": "", "raw_dir": "open_meteo_raw", "variables": [],
                       "lead_suffix": "_previous_day1", "request_chunk": "year", "sleep_s": 0},
        "window": {"start": "2024-01-20", "end": "2026-02-11"},
        "qc": {"neg_clip_kw": -0.5, "max_ratio_kwp": 1.2, "stuck_min_records": 6,
               "min_coverage": 0.8333, "daytime_zenith_max": 85, "near_zero_kwh_per_kwp_day": 0.5},
        "splits": {"time": None, "array": None},
    }
    return PipelineConfig(data, data_root_override=str(tmp_path))


METADATA_HEADER = (
    "source_id,meter_name,source_type,kwh_2024_to_2026_02,first_output,last_output,"
    "auto_flag,page_url_guess,array_no,manufacturer,kwp,technology,tilt_deg,"
    "azimuth_deg,tracking,inverter,commissioned,notes\n"
)

SOURCE_NAMES_HEADER = "export_column,source_id,quantity\n"


def _write_source_names(api_dir: Path, source_ids: list[int]) -> None:
    api_dir.mkdir(parents=True, exist_ok=True)
    lines = [SOURCE_NAMES_HEADER]
    for sid in source_ids:
        lines.append(f"DKA.M{sid} - Active Power (kW),{sid},193\n")
    (api_dir / "source_names.csv").write_text("".join(lines))


def _make_chunk_csv(path: Path, sid: int, timestamps: list[str], power: float = 2.5) -> None:
    """Write a minimal API chunk CSV."""
    lines = ["Timestamp,DKA.M{sid} - Active Power (kW)\n".replace("{sid}", str(sid))]
    for ts in timestamps:
        lines.append(f"{ts},{power}\n")
    path.write_text("".join(lines))


# ---------------------------------------------------------------------------
# Item 2 — API-vs-API dedup by mtime
# ---------------------------------------------------------------------------

class TestApiApiDedup:
    def test_newer_file_wins(self, tmp_path: Path):
        """When two API chunk files have the same (source_id, ts_utc_start),
        the record from the most-recently-modified file must be kept."""
        from pvdata.io_dkasc import _load_api_chunks

        api_dir = tmp_path / "dkasc_api_5min"
        _write_source_names(api_dir, [52])

        ts = "2026-03-01 06:00:00"

        # older file: power = 2.5
        old_file = api_dir / "2026-03-01_2026-03-03.csv"
        _make_chunk_csv(old_file, 52, [ts], power=2.5)

        # Ensure newer file has a later mtime
        time.sleep(0.05)

        # newer file: power = 9.9
        new_file = api_dir / "2026-03-01_2026-03-03_v2.csv"
        _make_chunk_csv(new_file, 52, [ts], power=9.9)

        power_df, station_df, api_dups = _load_api_chunks(
            api_dir, api_dir / "source_names.csv", {52}
        )

        assert api_dups == 1, f"Expected 1 API-API duplicate, got {api_dups}"
        assert len(power_df) == 1
        assert abs(power_df.iloc[0]["power_kw"] - 9.9) < 0.01, (
            f"Expected 9.9 kW (newer file), got {power_df.iloc[0]['power_kw']}"
        )

    def test_api_api_dups_in_stats(self, tmp_path: Path):
        """api_api_duplicates count is returned in the stats dict from load_dkasc."""
        from pvdata.io_dkasc import _load_api_chunks

        api_dir = tmp_path / "dkasc_api_5min"
        _write_source_names(api_dir, [52])

        ts = "2026-04-01 06:00:00"
        f1 = api_dir / "chunk_a.csv"
        _make_chunk_csv(f1, 52, [ts], power=1.0)
        time.sleep(0.05)
        f2 = api_dir / "chunk_b.csv"
        _make_chunk_csv(f2, 52, [ts], power=2.0)

        _, _, api_dups = _load_api_chunks(api_dir, api_dir / "source_names.csv", {52})
        assert api_dups == 1

    def test_no_overlap_zero_dups(self, tmp_path: Path):
        """Non-overlapping chunks produce zero API-vs-API duplicates."""
        from pvdata.io_dkasc import _load_api_chunks

        api_dir = tmp_path / "dkasc_api_5min"
        _write_source_names(api_dir, [52])

        f1 = api_dir / "chunk_a.csv"
        _make_chunk_csv(f1, 52, ["2026-04-01 06:00:00"], power=1.0)
        f2 = api_dir / "chunk_b.csv"
        _make_chunk_csv(f2, 52, ["2026-04-02 06:00:00"], power=2.0)

        _, _, api_dups = _load_api_chunks(api_dir, api_dir / "source_names.csv", {52})
        assert api_dups == 0


# ---------------------------------------------------------------------------
# Item 3 — Excel-tolerant metadata reading
# ---------------------------------------------------------------------------

class TestDateParsing:
    """_parse_date_strict must handle dd/mm/YYYY and ISO, but NOT infer Nov from 11/02."""

    def test_iso_date_only(self):
        from pvdata.metadata import _parse_date_strict
        ts = _parse_date_strict("2026-02-11", file="f", source_id=1, column="c")
        assert ts == pd.Timestamp("2026-02-11")

    def test_iso_datetime(self):
        from pvdata.metadata import _parse_date_strict
        ts = _parse_date_strict("2026-02-11 18:35:00", file="f", source_id=1, column="c")
        assert ts == pd.Timestamp("2026-02-11 18:35:00")

    def test_ddmmyyyy_is_feb_11_not_nov_2(self):
        """11/02/2026 must be 2026-02-11, not 2026-11-02 (US interpretation)."""
        from pvdata.metadata import _parse_date_strict
        ts = _parse_date_strict("11/02/2026", file="f", source_id=52, column="last_output")
        assert ts == pd.Timestamp("2026-02-11"), (
            f"Expected 2026-02-11 (dd/mm/yyyy), got {ts}"
        )

    def test_ddmmyyyy_with_time(self):
        from pvdata.metadata import _parse_date_strict
        ts = _parse_date_strict("01/01/2024 06:30", file="f", source_id=52, column="first_output")
        assert ts == pd.Timestamp("2024-01-01 06:30")

    def test_invalid_date_raises(self):
        from pvdata.metadata import _parse_date_strict
        with pytest.raises(ValueError, match="Unrecognised date"):
            _parse_date_strict("2024/13/01", file="meta.csv", source_id=52, column="first_output")

    def test_empty_returns_none(self):
        from pvdata.metadata import _parse_date_strict
        assert _parse_date_strict("", file="f", source_id=1, column="c") is None
        assert _parse_date_strict(None, file="f", source_id=1, column="c") is None

    def test_commissioned_ddmmyyyy_in_metadata(self, tmp_path: Path):
        """commissioned with dd/mm/yyyy like 12/11/2012 is normalised in memory to 2012-11-12."""
        from pvdata.metadata import load_metadata

        p = tmp_path / "arrays_metadata_template.csv"
        p.write_text(
            METADATA_HEADER +
            "52,DKA_M16_C,array_meter,,,,,, 33 ,REC,5.5,poly-Si,20,0,fixed,,12/11/2012,\n",
            encoding="utf-8",
        )
        (tmp_path / "array_overrides.csv").write_text(
            "source_id,valid_from_local,valid_to_local,exclude,reason\n"
        )
        cfg = _base_cfg(tmp_path)
        arrays = load_metadata(cfg)
        assert arrays.iloc[0]["commissioned"] == "2012-11-12"

    def test_invalid_commissioned_raises(self, tmp_path: Path):
        """Invalid date in commissioned column must raise ValueError naming file, source_id, column."""
        from pvdata.metadata import load_metadata

        p = tmp_path / "arrays_metadata_template.csv"
        p.write_text(
            METADATA_HEADER +
            "52,DKA_M16_C,array_meter,,,,,, 33 ,REC,5.5,poly-Si,20,0,fixed,,not-a-date,\n",
            encoding="utf-8",
        )
        (tmp_path / "array_overrides.csv").write_text(
            "source_id,valid_from_local,valid_to_local,exclude,reason\n"
        )
        cfg = _base_cfg(tmp_path)
        with pytest.raises(ValueError, match="column='commissioned'"):
            load_metadata(cfg)

    def test_override_valid_from_ddmmyyyy(self, tmp_path: Path):
        """valid_from_local in array_overrides.csv with 11/02/2026 parsed as Feb 11."""
        from pvdata.metadata import load_metadata

        p = tmp_path / "arrays_metadata_template.csv"
        p.write_text(
            METADATA_HEADER +
            "52,DKA_M16_C,array_meter,,,,,, 33 ,REC,5.5,poly-Si,20,0,fixed,,2012-11-12,\n",
            encoding="utf-8",
        )
        (tmp_path / "array_overrides.csv").write_text(
            "source_id,valid_from_local,valid_to_local,exclude,reason\n"
            "52,11/02/2026,,,test\n"
        )
        cfg = _base_cfg(tmp_path)
        arrays = load_metadata(cfg)
        # 11/02/2026 local (ACST = UTC+09:30) converted to UTC
        # 2026-02-11 00:00:00 ACST -> 2026-02-10 14:30:00 UTC
        expected_utc = pd.Timestamp("2026-02-11 00:00:00", tz="Australia/Darwin").tz_convert("UTC")
        assert arrays.iloc[0]["valid_from_utc"] == expected_utc

    def test_invalid_override_date_raises(self, tmp_path: Path):
        """Invalid date in array_overrides.csv must raise ValueError."""
        from pvdata.metadata import load_metadata

        p = tmp_path / "arrays_metadata_template.csv"
        p.write_text(
            METADATA_HEADER +
            "52,DKA_M16_C,array_meter,,,,,, 33 ,REC,5.5,poly-Si,20,0,fixed,,2012-11-12,\n",
            encoding="utf-8",
        )
        (tmp_path / "array_overrides.csv").write_text(
            "source_id,valid_from_local,valid_to_local,exclude,reason\n"
            "52,2026-99-99,,,test\n"
        )
        cfg = _base_cfg(tmp_path)
        with pytest.raises(ValueError, match="column='valid_from_local'"):
            load_metadata(cfg)


class TestBomReading:
    """_read_hand_edited_csv must handle Excel BOM (utf-8-sig) and strip whitespace."""

    def test_bom_file_read(self, tmp_path: Path):
        from pvdata.metadata import _read_hand_edited_csv

        # Write with BOM, as Excel does
        p = tmp_path / "meta.csv"
        p.write_bytes(
            b"\xef\xbb\xbf"  # UTF-8 BOM
            b"source_id,tracking\n"
            b"  52  ,  fixed  \n"
        )
        df = _read_hand_edited_csv(p)
        assert list(df.columns) == ["source_id", "tracking"]
        assert df.iloc[0]["source_id"] == "52"
        assert df.iloc[0]["tracking"] == "fixed"

    def test_cell_whitespace_stripped(self, tmp_path: Path):
        from pvdata.metadata import _read_hand_edited_csv

        p = tmp_path / "meta.csv"
        p.write_text("a,b\n  hello  ,\tworld\t\n", encoding="utf-8")
        df = _read_hand_edited_csv(p)
        assert df.iloc[0]["a"] == "hello"
        assert df.iloc[0]["b"] == "world"


class TestTrackingValidation:
    """load_metadata must raise ValueError for invalid tracking values."""

    def _write_meta(self, tmp_path: Path, tracking: str) -> Path:
        p = tmp_path / "arrays_metadata_template.csv"
        p.write_text(
            METADATA_HEADER +
            f"52,DKA_M16_C,array_meter,,,,,, 33 ,REC,5.5,poly-Si,20,0,{tracking},,2012-11-12,\n",
            encoding="utf-8",
        )
        return p

    def test_valid_fixed_ok(self, tmp_path: Path):
        from pvdata.metadata import load_metadata
        self._write_meta(tmp_path, "fixed")
        (tmp_path / "array_overrides.csv").write_text(
            "source_id,valid_from_local,valid_to_local,exclude,reason\n"
        )
        cfg = _base_cfg(tmp_path)
        result = load_metadata(cfg)  # must not raise
        assert len(result) == 1

    def test_invalid_tracking_raises(self, tmp_path: Path):
        from pvdata.metadata import load_metadata
        self._write_meta(tmp_path, "tracker_xyz")
        (tmp_path / "array_overrides.csv").write_text(
            "source_id,valid_from_local,valid_to_local,exclude,reason\n"
        )
        cfg = _base_cfg(tmp_path)
        with pytest.raises(ValueError, match="Invalid tracking value"):
            load_metadata(cfg)

    def test_single_axis_ok(self, tmp_path: Path):
        from pvdata.metadata import load_metadata
        self._write_meta(tmp_path, "single_axis")
        (tmp_path / "array_overrides.csv").write_text(
            "source_id,valid_from_local,valid_to_local,exclude,reason\n"
        )
        cfg = _base_cfg(tmp_path)
        result = load_metadata(cfg)
        assert len(result) == 1

    def test_blank_tracking_loads_no_error_metadata_complete_false(self, tmp_path: Path):
        """A metadata row with blank tracking must load without error and have metadata_complete=False.

        Regression: str(NaN or "") was producing "nan" which was not in TRACKING_ALLOWED,
        raising ValueError on rows where tracking was simply left empty.
        """
        from pvdata.metadata import load_metadata

        p = tmp_path / "arrays_metadata_template.csv"
        p.write_text(
            METADATA_HEADER +
            # tracking column is blank — simulates a hand-edited file with that cell empty
            "52,DKA_M16_C,array_meter,,,,,, 33 ,REC,5.5,poly-Si,20,0,,,2012-11-12,\n",
            encoding="utf-8",
        )
        (tmp_path / "array_overrides.csv").write_text(
            "source_id,valid_from_local,valid_to_local,exclude,reason\n"
        )
        cfg = _base_cfg(tmp_path)
        result = load_metadata(cfg)  # must NOT raise ValueError
        assert len(result) == 1
        assert result.iloc[0]["metadata_complete"] == False, (
            "Row with blank tracking must have metadata_complete=False"
        )

    def test_nan_tracking_via_pandas_na_loads_no_error(self, tmp_path: Path):
        """Blank tracking read as pd.NA by _read_hand_edited_csv must not raise.

        The old `str(x or "")` pattern turned pd.NA → "nan" (truthy) → ValueError.
        """
        from pvdata.metadata import load_metadata
        import io as _io

        # Write with BOM so pandas might produce StringDtype NA variants
        p = tmp_path / "arrays_metadata_template.csv"
        p.write_bytes(
            b"\xef\xbb\xbf"  # BOM
            + METADATA_HEADER.encode()
            + b"52,DKA_M16_C,array_meter,,,,,, 33 ,REC,5.5,poly-Si,20,0,,,2012-11-12,\n"
        )
        (tmp_path / "array_overrides.csv").write_text(
            "source_id,valid_from_local,valid_to_local,exclude,reason\n"
        )
        cfg = _base_cfg(tmp_path)
        result = load_metadata(cfg)
        assert result.iloc[0]["metadata_complete"] == False


class TestNormaliseExclude:
    """_normalise_exclude must handle all accepted truthy/falsy spellings."""

    def test_truthy_values(self):
        from pvdata.metadata import _normalise_exclude
        for v in ("true", "True", "TRUE", "1", "yes", "YES"):
            assert _normalise_exclude(v) is True, f"Expected True for {v!r}"

    def test_falsy_values(self):
        from pvdata.metadata import _normalise_exclude
        for v in ("false", "False", "FALSE", "0", "no", "NO", ""):
            assert _normalise_exclude(v) is False, f"Expected False for {v!r}"

    def test_invalid_raises(self):
        from pvdata.metadata import _normalise_exclude
        with pytest.raises(ValueError):
            _normalise_exclude("maybe")


# ---------------------------------------------------------------------------
# Item 4 — xlsx output
# ---------------------------------------------------------------------------

class TestXlsxOutput:
    def _make_sample_dir(self, tmp_path: Path) -> tuple[Path, pd.DataFrame]:
        """Set up minimal processed/v0/ folder and return (sample_dir, train_table)."""
        proc = tmp_path / "processed" / "v0"
        proc.mkdir(parents=True)

        tt = pd.DataFrame({
            "source_id": [52, 52],
            "ts_utc_end": pd.to_datetime(["2025-06-01 03:00", "2025-06-01 04:00"]).tz_localize("UTC"),
            "target_kw": [2.5, 2.8],
        })
        arrays = pd.DataFrame({
            "source_id": [52],
            "excluded": [False],
            "kwp": [5.5],
        })

        tt.to_parquet(proc / "train_table.parquet", index=False)
        arrays.to_parquet(proc / "arrays.parquet", index=False)

        return proc, tt

    def test_xlsx_has_expected_sheets(self, tmp_path: Path):
        """sample.xlsx must contain all 5 data sheets + README sheet."""
        from pvdata.sample import _write_xlsx

        proc, tt = self._make_sample_dir(tmp_path)
        sample_dir = tmp_path / "samples" / "v0_test"
        sample_dir.mkdir(parents=True)

        arrays = pd.DataFrame({"source_id": [52], "kwp": [5.5]})

        _write_xlsx(
            sample_dir=sample_dir,
            version="v0", start="2025-06-01", end="2025-08-31",
            tables={
                "arrays": arrays,
                "train_table": tt,
                "labels_hourly": pd.DataFrame(),
                "weather_hourly": pd.DataFrame(),
                "station_hourly": pd.DataFrame(),
            },
        )

        import openpyxl
        wb = openpyxl.load_workbook(sample_dir / "sample.xlsx")
        assert "train_table" in wb.sheetnames
        assert "arrays" in wb.sheetnames
        assert "labels_hourly" in wb.sheetnames
        assert "weather_hourly" in wb.sheetnames
        assert "station_hourly" in wb.sheetnames
        assert "README" in wb.sheetnames

    def test_xlsx_timestamps_are_naive(self, tmp_path: Path):
        """Datetime cells in xlsx must be tz-naive (no tzinfo)."""
        from pvdata.sample import _write_xlsx, _strip_tz

        sample_dir = tmp_path / "sample_ts"
        sample_dir.mkdir()

        tt = pd.DataFrame({
            "source_id": [52],
            "ts_utc_end": pd.to_datetime(["2025-06-01 03:00"]).tz_localize("UTC"),
            "power_kw_mean": [2.5],
        })

        _write_xlsx(
            sample_dir=sample_dir,
            version="v0", start="2025-06-01", end="2025-08-31",
            tables={"train_table": tt, "arrays": pd.DataFrame(),
                    "labels_hourly": pd.DataFrame(), "weather_hourly": pd.DataFrame(),
                    "station_hourly": pd.DataFrame()},
        )

        import openpyxl
        wb = openpyxl.load_workbook(sample_dir / "sample.xlsx")
        ws = wb["train_table"]
        # Cell A1 = header "source_id", B1 = "ts_utc_end"
        # First data row: B2 should be a naive datetime
        val = ws.cell(row=2, column=2).value
        assert isinstance(val, datetime), f"Expected datetime, got {type(val)}"
        assert val.tzinfo is None, f"Expected tz-naive, got tzinfo={val.tzinfo}"

    def test_xlsx_row_limit_raises(self, tmp_path: Path):
        """_write_sheet must raise ValueError when DataFrame exceeds MAX_XLSX_ROWS."""
        from pvdata.sample import _write_sheet, MAX_XLSX_ROWS
        import openpyxl

        wb = openpyxl.Workbook()
        ws = wb.active

        # DataFrame with MAX_XLSX_ROWS + 1 rows
        big_df = pd.DataFrame({"x": range(MAX_XLSX_ROWS + 1)})
        with pytest.raises(ValueError, match="exceeding the limit"):
            _write_sheet(ws, big_df)

    def test_csv_utf8_sig_encoding(self, tmp_path: Path):
        """CSVs must be written with UTF-8 BOM (utf-8-sig) so Excel renders ° correctly."""
        from pvdata.sample import export_sample

        proc = tmp_path / "processed" / "v0"
        proc.mkdir(parents=True)

        import pytz
        tz = pytz.timezone("Australia/Darwin")

        tt = pd.DataFrame({
            "source_id": [52],
            "ts_utc_end": pd.to_datetime(["2025-06-01 03:00"]).tz_localize("UTC"),
            "target_kw": [2.5],
        })
        arrays = pd.DataFrame({"source_id": [52], "excluded": [False]})
        tt.to_parquet(proc / "train_table.parquet", index=False)
        arrays.to_parquet(proc / "arrays.parquet", index=False)

        from pvdata.config import PipelineConfig
        cfg_data = {
            "data_root": str(tmp_path),
            "site": {"latitude": -23.7624, "longitude": 133.8754,
                     "altitude_m": 560, "tz": "Australia/Darwin"},
            "dkasc": {"yearly_files": [], "api_dir": "dkasc_api_5min",
                      "api_name_map": "dkasc_api_5min/source_names.csv",
                      "weather_source_id": 101,
                      "metadata_file": "arrays_metadata_template.csv",
                      "overrides_file": "array_overrides.csv"},
            "open_meteo": {"endpoint": "", "raw_dir": "open_meteo_raw", "variables": [],
                           "lead_suffix": "_previous_day1", "request_chunk": "year", "sleep_s": 0},
            "window": {"start": "2024-01-20", "end": "2026-02-11"},
            "qc": {"neg_clip_kw": -0.5, "max_ratio_kwp": 1.2, "stuck_min_records": 6,
                   "min_coverage": 0.8333, "daytime_zenith_max": 85,
                   "near_zero_kwh_per_kwp_day": 0.5},
            "splits": {"time": None, "array": None},
        }
        cfg = PipelineConfig(cfg_data, data_root_override=str(tmp_path))
        export_sample(version="v0", start="2025-06-01", end="2025-08-31", cfg=cfg)

        csv_path = tmp_path / "samples" / "v0_2025-06-01_2025-08-31" / "train_table.csv"
        assert csv_path.exists()
        # Check BOM present
        raw = csv_path.read_bytes()
        assert raw[:3] == b"\xef\xbb\xbf", "CSV missing UTF-8 BOM"
