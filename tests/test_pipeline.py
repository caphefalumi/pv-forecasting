"""Tests for the PV data pipeline (no network, no real data).

Coverage:
- Header parsing (both naming styles, Reactive exclusion)
- Timezone conversion
- Hour binning and coverage math
- Seam: two sources in same hour → coverage 12/12
- Dedup: yearly wins, conflict counter
- Malformed line: synthetic CSV with merged line → skip + report
- QC rules: each rule on synthetic frame
- Azimuth conversion on N/E/S/W inputs
- Open-Meteo loader: synthetic JSON with nulls → coverage_start, wind conversion
- Build refuses to overwrite existing version
"""

import io
import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ROOT = Path(__file__).parent.parent


def make_cfg(**overrides):
    """Create a minimal PipelineConfig with temp data_root."""
    from pvdata.config import PipelineConfig
    data = {
        "data_root": "data",
        "site": {"latitude": -23.7624, "longitude": 133.8754, "altitude_m": 560, "tz": "Australia/Darwin"},
        "dkasc": {
            "yearly_files": ["Alice_Springs_2024.csv"],
            "api_dir": "dkasc_api_5min",
            "api_name_map": "dkasc_api_5min/source_names.csv",
            "weather_source_id": 101,
            "metadata_file": "arrays_metadata_template.csv",
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
        },
        "window": {"start": "auto", "end": "2026-02-11"},
        "qc": {
            "neg_clip_kw": -0.5,
            "max_ratio_kwp": 1.2,
            "stuck_min_records": 6,
            "min_coverage": 0.8333,
            "daytime_zenith_max": 85,
            "near_zero_kwh_per_kwp_day": 0.5,
        },
        "splits": {"time": None, "array": None},
    }
    data.update(overrides)
    return PipelineConfig(data)


# ---------------------------------------------------------------------------
# Test 1: Header parsing — both naming styles, Reactive exclusion
# ---------------------------------------------------------------------------

class TestHeaderParsing:
    def test_both_naming_styles_same_id(self):
        """Old style (239_DKA_Totals_BESS_Active_Power) and new (239_Active_Power)
        both map to source_id 239."""
        from pvdata.io_dkasc import _extract_source_id, _is_active_power

        old_col = "239_DKA_Totals_BESS_Active_Power"
        new_col = "239_Active_Power"

        assert _extract_source_id(old_col) == 239
        assert _extract_source_id(new_col) == 239

    def test_active_power_matched(self):
        from pvdata.io_dkasc import _is_active_power
        assert _is_active_power("52_DKA_M16_C_Phase_Active_Power")
        assert _is_active_power("239_Active_Power")

    def test_reactive_power_excluded(self):
        from pvdata.io_dkasc import _is_active_power
        assert not _is_active_power("52_DKA_M16_C_Phase_Reactive_Power")
        assert not _is_active_power("52_Reactive_Power")
        assert not _is_active_power("52_Apparent_Power")

    def test_no_id_returns_none(self):
        from pvdata.io_dkasc import _extract_source_id
        assert _extract_source_id("timestamp") is None
        assert _extract_source_id("") is None


# ---------------------------------------------------------------------------
# Test 2: Timezone conversion
# ---------------------------------------------------------------------------

class TestTimezone:
    def test_local_to_utc(self):
        """Local 2024-06-01 12:00 Alice Springs → UTC 2024-06-01 02:30."""
        local_ts = pd.Timestamp("2024-06-01 12:00:00")
        utc_ts = local_ts.tz_localize("Australia/Darwin").tz_convert("UTC")
        assert utc_ts == pd.Timestamp("2024-06-01 02:30:00", tz="UTC")

    def test_dst_not_applied(self):
        """Alice Springs (UTC+09:30) has no DST."""
        # Summer
        ts_summer = pd.Timestamp("2024-01-01 12:00:00").tz_localize("Australia/Darwin").tz_convert("UTC")
        # Winter
        ts_winter = pd.Timestamp("2024-07-01 12:00:00").tz_localize("Australia/Darwin").tz_convert("UTC")
        assert ts_summer.utcoffset() == ts_winter.utcoffset()


# ---------------------------------------------------------------------------
# Test 3: Hour binning — coverage math
# ---------------------------------------------------------------------------

class TestHourBinning:
    def _make_labels(self, n_records: int, sid: int = 52) -> pd.DataFrame:
        """Make n_records starting at 11:30 ACST (02:00 UTC) every 5 min."""
        # 11:30 ACST = 02:00 UTC; ceil to 03:00 UTC
        base_utc = pd.Timestamp("2024-06-01 02:00:00", tz="UTC")
        ts = [base_utc + pd.Timedelta(minutes=5 * i) for i in range(n_records)]
        return pd.DataFrame({
            "source_id": np.int16(sid),
            "ts_utc_start": ts,
            "power_kw": np.float32(2.5),
            "source_file": "test.csv",
            "qc_flag": "",
        })

    def test_binning_to_correct_hour(self):
        """Records at 11:30…12:25 ACST land in ts_utc_end = 03:00 UTC.

        Uses floor("h") + 1h (same logic as aggregate.py) so that
        ts_utc_start=02:00:00 maps to 03:00:00, not to itself.
        """
        labels = self._make_labels(12)  # 12 records: 02:00, 02:05, ..., 02:55 UTC
        labels["ts_utc_end"] = labels["ts_utc_start"].dt.floor("h") + pd.Timedelta(hours=1)
        assert (labels["ts_utc_end"] == pd.Timestamp("2024-06-01 03:00:00", tz="UTC")).all()

    def test_coverage_10_of_12(self):
        """9 valid records → coverage = 9/12, target = NaN (< 10/12 = 0.8333)."""
        from pvdata.aggregate import aggregate_labels
        from pvdata.config import PipelineConfig

        labels = self._make_labels(9)
        arrays_meta = pd.DataFrame({
            "source_id": [52],
            "kwp": [5.5],
            "excluded": [False],
            "valid_from_utc": [pd.NaT],
            "valid_to_utc": [pd.NaT],
        })
        cfg = make_cfg()
        result = aggregate_labels(labels, arrays_meta, cfg)
        assert len(result) == 1
        assert abs(result.iloc[0]["coverage"] - 9/12) < 1e-4
        assert pd.isna(result.iloc[0]["target_kw"])  # < 0.8333 threshold

    def test_coverage_10_of_12_passes(self):
        """10 valid records → coverage = 10/12 ≥ 0.8333 → target not NaN."""
        from pvdata.aggregate import aggregate_labels

        labels = self._make_labels(10)
        arrays_meta = pd.DataFrame({
            "source_id": [52],
            "kwp": [5.5],
            "excluded": [False],
            "valid_from_utc": [pd.NaT],
            "valid_to_utc": [pd.NaT],
        })
        cfg = make_cfg()
        result = aggregate_labels(labels, arrays_meta, cfg)
        assert not pd.isna(result.iloc[0]["target_kw"])
        assert abs(result.iloc[0]["coverage"] - 10/12) < 1e-4


# ---------------------------------------------------------------------------
# Test 4: Seam — two sources contributing half an hour each → 12/12
# ---------------------------------------------------------------------------

class TestSeam:
    def test_seam_coverage_full(self):
        """Yearly records 02:00–02:25 UTC + API records 02:30–02:55 UTC → 12/12."""
        from pvdata.aggregate import aggregate_labels

        base = pd.Timestamp("2024-06-01 02:00:00", tz="UTC")
        yearly_ts = [base + pd.Timedelta(minutes=5 * i) for i in range(6)]
        api_ts = [base + pd.Timedelta(minutes=30 + 5 * i) for i in range(6)]

        labels = pd.DataFrame({
            "source_id": np.int16(52),
            "ts_utc_start": yearly_ts + api_ts,
            "power_kw": np.float32(2.5),
            "source_file": ["yearly.csv"] * 6 + ["api.csv"] * 6,
            "qc_flag": "",
        })

        arrays_meta = pd.DataFrame({
            "source_id": [52],
            "kwp": [5.5],
            "excluded": [False],
            "valid_from_utc": [pd.NaT],
            "valid_to_utc": [pd.NaT],
        })
        cfg = make_cfg()
        result = aggregate_labels(labels, arrays_meta, cfg)
        assert len(result) == 1
        assert result.iloc[0]["n_valid"] == 12
        assert abs(result.iloc[0]["coverage"] - 1.0) < 1e-4


# ---------------------------------------------------------------------------
# Test 5: Dedup — yearly wins, conflict counter
# ---------------------------------------------------------------------------

class TestDedup:
    def _make_synthetic_csv(self, tmp_path: Path, n_records: int = 5) -> Path:
        """Create a tiny synthetic yearly CSV."""
        header = "timestamp,52_DKA_M16_Active_Power,101_Global_Horizontal_Radiation\n"
        rows = []
        for i in range(n_records):
            ts = f"2024-01-01 {6 + i:02d}:00:00"
            rows.append(f'"{ts}",2.5,500\n')
        p = tmp_path / "yearly.csv"
        p.write_text(header + "".join(rows))
        return p

    def test_yearly_wins_on_dedup(self):
        """When yearly and API have the same (source_id, ts_utc_start), yearly value is kept."""
        import pandas as pd
        import numpy as np

        ts = pd.Timestamp("2024-01-01 06:00:00", tz="Australia/Darwin").tz_convert("UTC")

        yearly = pd.DataFrame({
            "source_id": np.int16(52),
            "ts_utc_start": [ts],
            "power_kw": np.float32(2.5),
            "source_file": "yearly.csv",
            "_origin": "yearly",
        })
        api = pd.DataFrame({
            "source_id": np.int16(52),
            "ts_utc_start": [ts],
            "power_kw": np.float32(3.0),  # different value
            "source_file": "api.csv",
            "_origin": "api",
        })
        combined = pd.concat([yearly, api], ignore_index=True)
        n_before = len(combined)
        combined = combined.drop_duplicates(subset=["source_id", "ts_utc_start"], keep="first")
        assert len(combined) == 1
        assert combined.iloc[0]["power_kw"] == pytest.approx(2.5)
        assert n_before - len(combined) == 1  # 1 duplicate dropped


# ---------------------------------------------------------------------------
# Test 6: Malformed line — skip + report
# ---------------------------------------------------------------------------

class TestMalformedLine:
    def test_malformed_line_skipped(self, tmp_path: Path):
        """A CSV with a merged (too many fields) line is read with that line skipped."""
        from pvdata.io_dkasc import _pre_scan_file

        # Normal header with 3 cols; row 2 has 5 cols (merged)
        content = (
            "timestamp,52_Active_Power,101_GHI\n"
            '"2024-01-01 06:00:00",2.5,500\n'
            '"2024-01-02 06:00:00",2.5,500,"extra1","extra2"\n'  # merged → 5 cols
            '"2024-01-03 06:00:00",2.5,500\n'
        )
        p = tmp_path / "test.csv"
        p.write_text(content)

        expected, bad_lines = _pre_scan_file(p)
        assert expected == 3
        assert len(bad_lines) == 1
        bad_lineno, bad_nfields, snippet = bad_lines[0]
        assert bad_lineno == 3  # line 3 (1-indexed, counting header)
        assert bad_nfields == 5

    def test_malformed_line_skipped_in_read_yearly_file(self, tmp_path: Path):
        """_read_yearly_file correctly skips the bad line identified by _pre_scan_file."""
        from pvdata.io_dkasc import _pre_scan_file, _read_yearly_file

        content = (
            "timestamp,52_Active_Power,101_Global_Horizontal_Radiation\n"
            '"2024-01-01 06:00:00",2.5,500\n'
            '"2024-01-02 06:00:00",2.5,500,"extra1","extra2"\n'
            '"2024-01-03 06:00:00",3.0,600\n'
        )
        p = tmp_path / "test.csv"
        p.write_text(content)

        _, bad_lines = _pre_scan_file(p)
        bad_line_numbers = {b[0] for b in bad_lines}
        assert bad_line_numbers == {3}

        power_df, station_df = _read_yearly_file(p, {52}, bad_line_numbers)
        assert len(power_df) == 2
        assert list(power_df["power_kw"]) == [pytest.approx(2.5), pytest.approx(3.0)]


# ---------------------------------------------------------------------------
# Test 7: QC rules on synthetic frame
# ---------------------------------------------------------------------------

class TestQCRules:
    def _make_qc_input(self, power_values, qc_flags=None):
        """Make a minimal labels DataFrame for QC testing."""
        n = len(power_values)
        ts = [pd.Timestamp("2024-06-01 02:00:00", tz="UTC") + pd.Timedelta(minutes=5 * i)
              for i in range(n)]
        df = pd.DataFrame({
            "source_id": np.int16(52),
            "ts_utc_start": ts,
            "power_kw": np.float32(power_values),
            "source_file": "test.csv",
            "qc_flag": qc_flags or [""] * n,
        })
        return df

    def _arrays_meta(self, kwp=5.5):
        return pd.DataFrame({"source_id": [52], "kwp": [float(kwp)]})

    def test_missing_flag(self):
        from pvdata.qc import apply_qc
        labels = self._make_qc_input([float("nan"), 2.5])
        result = apply_qc(labels, self._arrays_meta(), pd.DataFrame(), pd.DataFrame(), make_cfg())
        assert "missing" in result.iloc[0]["qc_flag"]
        assert result.iloc[1]["qc_flag"] == ""

    def test_neg_flag(self):
        from pvdata.qc import apply_qc
        labels = self._make_qc_input([-1.0, 2.5])
        result = apply_qc(labels, self._arrays_meta(), pd.DataFrame(), pd.DataFrame(), make_cfg())
        assert "neg" in result.iloc[0]["qc_flag"]

    def test_clip_no_flag(self):
        from pvdata.qc import apply_qc
        labels = self._make_qc_input([-0.1, 2.5])  # -0.1 in clip range [-0.5, 0)
        result = apply_qc(labels, self._arrays_meta(), pd.DataFrame(), pd.DataFrame(), make_cfg())
        assert result.iloc[0]["qc_flag"] == ""  # no flag
        assert result.iloc[0]["power_kw"] == pytest.approx(0.0)  # clipped

    def test_over_kwp_flag(self):
        from pvdata.qc import apply_qc
        labels = self._make_qc_input([7.0, 2.5])  # 7.0 > 1.2 * 5.5 = 6.6
        result = apply_qc(labels, self._arrays_meta(kwp=5.5), pd.DataFrame(), pd.DataFrame(), make_cfg())
        assert "over_kwp" in result.iloc[0]["qc_flag"]
        assert result.iloc[1]["qc_flag"] == ""

    def test_stuck_flag(self):
        from pvdata.qc import apply_qc
        # 6 consecutive identical non-zero values during daytime (zenith < 85)
        power = [2.5] * 6 + [3.0]
        labels = self._make_qc_input(power)
        # Solar position: all daytime (zenith < 85)
        solar_pos = pd.DataFrame({
            "ts_utc_start": labels["ts_utc_start"],
            "zenith": [30.0] * 7,
        })
        result = apply_qc(labels, self._arrays_meta(), pd.DataFrame(), solar_pos, make_cfg())
        for i in range(6):
            assert "stuck" in result.iloc[i]["qc_flag"], f"Row {i} not stuck"
        assert "stuck" not in result.iloc[6]["qc_flag"]

    def test_day_zero_flag(self):
        from pvdata.qc import apply_qc
        # Array power = 0, GHI > 300, fleet median > 0.3
        labels = pd.DataFrame({
            "source_id": np.int16([52, 55]),  # two arrays
            "ts_utc_start": [pd.Timestamp("2024-06-01 03:00:00", tz="UTC")] * 2,
            "power_kw": np.float32([0.0, 3.0]),  # 52 = zero, 55 = active
            "source_file": "test.csv",
            "qc_flag": ["", ""],
        })
        arrays_meta = pd.DataFrame({
            "source_id": [52, 55],
            "kwp": [5.5, 6.0],
        })
        station = pd.DataFrame({
            "ts_utc_start": [pd.Timestamp("2024-06-01 03:00:00", tz="UTC")],
            "ghi_obs": [500.0],
        })
        result = apply_qc(labels, arrays_meta, station, pd.DataFrame(), make_cfg())
        # Array 52 (power=0, GHI>300, fleet active) → day_zero
        arr52 = result[result["source_id"] == 52]
        assert "day_zero" in arr52.iloc[0]["qc_flag"]


# ---------------------------------------------------------------------------
# Test 8: Azimuth conversion
# ---------------------------------------------------------------------------

class TestAzimuthConversion:
    def test_north(self):
        from pvdata.metadata import dkasc_to_pvlib_azimuth
        assert dkasc_to_pvlib_azimuth(0) == pytest.approx(0.0)

    def test_east(self):
        from pvdata.metadata import dkasc_to_pvlib_azimuth
        assert dkasc_to_pvlib_azimuth(90) == pytest.approx(90.0)

    def test_south(self):
        from pvdata.metadata import dkasc_to_pvlib_azimuth
        assert dkasc_to_pvlib_azimuth(180) == pytest.approx(180.0)

    def test_west(self):
        from pvdata.metadata import dkasc_to_pvlib_azimuth
        assert dkasc_to_pvlib_azimuth(270) == pytest.approx(270.0)

    def test_nan(self):
        from pvdata.metadata import dkasc_to_pvlib_azimuth
        assert np.isnan(dkasc_to_pvlib_azimuth(float("nan")))

    def test_normalization(self):
        from pvdata.metadata import dkasc_to_pvlib_azimuth
        # 360 → 0
        assert dkasc_to_pvlib_azimuth(360) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Test 9: Open-Meteo loader synthetic JSON
# ---------------------------------------------------------------------------

class TestOpenMeteoLoader:
    def _make_synthetic_json(self, tmp_path: Path, n_null_hours: int = 10) -> Path:
        """Create a synthetic previous_runs_*.json with n_null_hours at start."""
        hours = pd.date_range("2024-01-19 00:00", periods=48, freq="h", tz="UTC")
        times = [t.strftime("%Y-%m-%dT%H:%M") for t in hours]
        # First n_null_hours are null, rest are valid
        ghi = [None] * n_null_hours + [400.0] * (48 - n_null_hours)
        data = {
            "latitude": -23.796,
            "longitude": 133.858,
            "elevation": 558,
            "hourly": {
                "time": times,
                "shortwave_radiation_previous_day1": ghi,
                "direct_normal_irradiance_previous_day1": ghi,
                "diffuse_radiation_previous_day1": ghi,
                "temperature_2m_previous_day1": [20.0] * n_null_hours + [25.0] * (48 - n_null_hours),
                "cloud_cover_previous_day1": [None] * n_null_hours + [50.0] * (48 - n_null_hours),
                "wind_speed_10m_previous_day1": [None] * n_null_hours + [36.0] * (48 - n_null_hours),
            },
        }
        fpath = tmp_path / "previous_runs_2024-01-19_2024-01-20.json"
        fpath.write_text(json.dumps(data))
        return fpath

    def test_wind_conversion(self, tmp_path: Path):
        """wind_speed_10m in km/h → m/s (36 km/h → 10 m/s)."""
        from pvdata.io_openmeteo import _parse_one_json

        fpath = self._make_synthetic_json(tmp_path, n_null_hours=0)
        # Override with all-valid data and wind=36 km/h
        data = json.loads(fpath.read_text())
        data["hourly"]["wind_speed_10m_previous_day1"] = [36.0] * 48
        fpath.write_text(json.dumps(data))

        df, gi = _parse_one_json(fpath)
        assert df is not None
        # 36 km/h / 3.6 = 10 m/s
        valid_wind = df["wind_fc"].dropna()
        assert (abs(valid_wind - 10.0) < 0.01).all()

    def test_coverage_start(self, tmp_path: Path):
        """coverage_start finds first full local day after continuous non-null coverage."""
        from pvdata.io_openmeteo import coverage_start as cvg_start, _parse_one_json

        fpath = self._make_synthetic_json(tmp_path, n_null_hours=12)
        df, _ = _parse_one_json(fpath)

        cfg = make_cfg()
        result = cvg_start(df, cfg)
        # First non-null hour (index 12) = 2024-01-19 12:00 UTC
        # Local date = 2024-01-19 (ACST = UTC+09:30), next full local day = 2024-01-20
        assert result == "2024-01-20"


# ---------------------------------------------------------------------------
# Test 10: Build refuses to overwrite
# ---------------------------------------------------------------------------

class TestBuildRefusesOverwrite:
    def test_refuses_overwrite(self, tmp_path: Path):
        """Build raises FileExistsError when version folder exists and overwrite=False."""
        from pvdata.build import build
        from pvdata.config import PipelineConfig

        # Create a dummy version folder
        version = "v0"
        processed = tmp_path / "processed" / version
        processed.mkdir(parents=True)

        cfg_data = {
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
            "open_meteo": {
                "endpoint": "", "raw_dir": "open_meteo_raw", "variables": [],
                "lead_suffix": "_previous_day1", "request_chunk": "year", "sleep_s": 0,
            },
            "window": {"start": "2024-01-20", "end": "2026-02-11"},
            "qc": {
                "neg_clip_kw": -0.5, "max_ratio_kwp": 1.2, "stuck_min_records": 6,
                "min_coverage": 0.8333, "daytime_zenith_max": 85, "near_zero_kwh_per_kwp_day": 0.5,
            },
            "splits": {"time": None, "array": None},
        }
        cfg = PipelineConfig(cfg_data, data_root_override=str(tmp_path))

        with pytest.raises(FileExistsError, match="already exists"):
            build(version=version, cfg=cfg, overwrite=False)


# ---------------------------------------------------------------------------
# Test 11: array_no stays as string (regression — alphanumeric values like "1A")
# ---------------------------------------------------------------------------

class TestArrayNoPreservedAsString:
    """Regression: array_no values like '1A', '16D', '9A' must NOT become NaN."""

    def _make_metadata_csv(self, tmp_path: Path) -> Path:
        header = (
            "source_id,meter_name,source_type,kwh_2024_to_2026_02,first_output,last_output,"
            "auto_flag,page_url_guess,array_no,manufacturer,kwp,technology,tilt_deg,"
            "azimuth_deg,tracking,inverter,commissioned,notes\n"
        )
        rows = [
            '52,DKA_M16_C,array_meter,,,,,, 1A ,REC,5.5,poly-Si,20,0,fixed,,2012-11-12,\n',
            '55,DKA_M20_B,array_meter,,,,,, 16D ,CSUN,6.0,poly-Si,20,0,fixed,,2013-08-14,\n',
            '56,DKA_M20_A,array_meter,,,,,,9A,Q CELLS,5.61,poly-Si,20,0,fixed,,2013-12-01,\n',
            # Purely numeric array_no should also still work as string
            '58,DKA_M17_C,array_meter,,,,,, 36 ,Nova,5.2,poly-Si,20,0,fixed,,2016-06-09,\n',
        ]
        p = tmp_path / "arrays_metadata_template.csv"
        p.write_text(header + "".join(rows), encoding="utf-8")
        return p

    def test_array_no_preserved(self, tmp_path: Path):
        """array_no with alphanumeric values must remain as strings, not NaN."""
        import yaml
        from pvdata.config import PipelineConfig
        from pvdata.metadata import load_metadata

        # Write overrides stub
        overrides = tmp_path / "array_overrides.csv"
        overrides.write_text("source_id,valid_from_local,valid_to_local,exclude,reason\n")

        self._make_metadata_csv(tmp_path)

        cfg_data = {
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
        cfg = PipelineConfig(cfg_data, data_root_override=str(tmp_path))
        result = load_metadata(cfg)

        # All array_no values must be non-null strings
        assert result["array_no"].notna().all(), (
            f"array_no has NaN: {result[['source_id','array_no']].to_dict('records')}"
        )
        # Alphanumeric values preserved exactly (stripped)
        array_nos = result.set_index("source_id")["array_no"]
        assert array_nos[52] == "1A",  f"Expected '1A', got {array_nos[52]!r}"
        assert array_nos[55] == "16D", f"Expected '16D', got {array_nos[55]!r}"
        assert array_nos[56] == "9A",  f"Expected '9A', got {array_nos[56]!r}"
        # Purely numeric stays as string (not int/float)
        assert array_nos[58] == "36",  f"Expected '36', got {array_nos[58]!r}"
        assert isinstance(array_nos[58], str), "Numeric array_no should still be a string"
