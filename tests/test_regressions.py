"""Regression tests for bugs found when running the real v1 build (2026-09-26)."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from tests.test_followup import METADATA_HEADER, _base_cfg


def _write_meta(tmp_path: Path, rows: str) -> None:
    (tmp_path / "arrays_metadata_template.csv").write_text(METADATA_HEADER + rows, encoding="utf-8")
    (tmp_path / "array_overrides.csv").write_text("source_id,valid_from_local,valid_to_local,exclude,reason\n")


def test_blank_tracking_is_allowed(tmp_path):
    from pvdata.metadata import load_metadata
    _write_meta(tmp_path, "54,DKA_M15_C,array_meter,,,,near_zero_output,,,,,,,,,,,\n")
    arrays = load_metadata(_base_cfg(tmp_path))
    assert not bool(arrays.iloc[0]["metadata_complete"])


def test_first_last_output_do_not_truncate_validity(tmp_path):
    """first_output/last_output are informational; they must not cut data after 2026-02-11."""
    from pvdata.metadata import load_metadata
    _write_meta(tmp_path, "52,DKA_M16_C,array_meter,1,01/01/2024 6:30,11/02/2026 18:35,,,33,REC,5.5,"
                          "poly-Si,20,0,fixed,,12/11/2012,\n")
    arrays = load_metadata(_base_cfg(tmp_path))
    assert pd.isna(arrays.iloc[0]["valid_to_utc"])
    assert pd.isna(arrays.iloc[0]["valid_from_utc"])


def test_solar_position_accepts_series(tmp_path):
    from pvdata.features import compute_solar_position
    ts = pd.Series(pd.date_range("2025-06-01 00:00", periods=24, freq="h", tz="UTC"))
    sp = compute_solar_position(ts, _base_cfg(tmp_path))
    assert len(sp) == 24 and sp["ghi_clear"].max() > 500
    # local solar noon ~ 03:00 UTC -> hour-ending 04:00 has the smallest zenith
    assert sp.loc[sp["solar_zenith"].idxmin(), "ts_utc_end"].hour in (3, 4)


def test_array_features_long_format(tmp_path):
    from pvdata.features import compute_array_features, compute_solar_position
    cfg = _base_cfg(tmp_path)
    ts = pd.Series(pd.date_range("2025-06-01 00:00", periods=24, freq="h", tz="UTC"))
    hourly = compute_solar_position(ts, cfg)
    hourly = hourly.assign(ghi_fc=hourly["ghi_clear"], dni_fc=600.0, dhi_fc=80.0, temp_fc=20.0, wind_fc=2.0)
    hourly.loc[~hourly["is_daylight"], ["ghi_fc", "dni_fc", "dhi_fc"]] = 0.0
    meta = pd.DataFrame({
        "source_id": [1, 2, 3], "metadata_complete": [True, True, False],
        "tracking": ["fixed", "dual_axis", "fixed"], "tilt_deg": [20.0, np.nan, 20.0],
        "surface_azimuth_pvlib": [0.0, np.nan, 0.0],
    })
    out = compute_array_features(hourly, meta, cfg)
    assert set(out["source_id"]) == {1, 2}
    assert len(out) == 48
    noon = out[out["ts_utc_end"] == hourly.loc[hourly["solar_zenith"].idxmin(), "ts_utc_end"]]
    # dual-axis tracker sees at least as much as the fixed array at noon
    assert noon.set_index("source_id").loc[2, "poa_fc"] >= noon.set_index("source_id").loc[1, "poa_fc"] - 1


def _qc_inputs(power, zen=30.0, ghi=800.0):
    ts = pd.date_range("2025-06-01 02:00", periods=len(power), freq="5min", tz="UTC")
    labels = pd.DataFrame({"source_id": np.int16(1), "ts_utc_start": ts,
                           "power_kw": np.array(power, dtype="float32"), "source_file": "x"})
    solar = pd.DataFrame({"ts_utc_start": ts, "solar_zenith": zen})
    station = pd.DataFrame({"ts_utc_start": ts, "ghi_obs": ghi})
    return labels, solar, station


def test_stuck_flag_runs(tmp_path):
    from pvdata.qc import apply_qc
    cfg = _base_cfg(tmp_path)
    labels, solar, _ = _qc_inputs([1.0, 2.0, 2.0, 2.0, 2.0, 2.0, 2.0, 3.0, 3.0])
    meta = pd.DataFrame({"source_id": [1], "kwp": [5.0]})
    out = apply_qc(labels, meta, pd.DataFrame(), solar, cfg)
    flags = out["qc_flag"].astype(str).tolist()
    assert flags[1:7] == ["stuck"] * 6 and flags[0] == "" and flags[7] == ""


def test_stuck_not_flagged_at_night(tmp_path):
    from pvdata.qc import apply_qc
    cfg = _base_cfg(tmp_path)
    labels, solar, _ = _qc_inputs([2.0] * 8, zen=95.0)
    out = apply_qc(labels, pd.DataFrame({"source_id": [1], "kwp": [5.0]}), pd.DataFrame(), solar, cfg)
    assert (out["qc_flag"].astype(str) == "").all()


def test_day_zero_flag(tmp_path):
    from pvdata.qc import apply_qc
    cfg = _base_cfg(tmp_path)
    ts = pd.date_range("2025-06-01 02:00", periods=3, freq="5min", tz="UTC")
    labels = pd.DataFrame({
        "source_id": np.repeat(np.int16([1, 2, 3]), 3), "ts_utc_start": np.tile(ts, 3),
        "power_kw": np.array([0, 0, 0, 3, 3.1, 3.2, 3, 3.3, 3.1], dtype="float32"), "source_file": "x"})
    station = pd.DataFrame({"ts_utc_start": ts, "ghi_obs": 800.0})
    meta = pd.DataFrame({"source_id": [1, 2, 3], "kwp": [5.0, 5.0, 5.0]})
    out = apply_qc(labels, meta, station, pd.DataFrame(), cfg)
    f = out.set_index("source_id")["qc_flag"].astype(str)
    assert (f.loc[1] == "day_zero").all() and (f.loc[2] == "").all()


def test_aggregate_accepts_categorical_flags(tmp_path):
    from pvdata.aggregate import aggregate_labels
    cfg = _base_cfg(tmp_path)
    ts = pd.date_range("2025-06-01 02:00", periods=12, freq="5min", tz="UTC")
    labels = pd.DataFrame({"source_id": np.int16(1), "ts_utc_start": ts,
                           "power_kw": np.float32(2.0),
                           "qc_flag": pd.Categorical([""] * 10 + ["stuck", "missing,stuck"])})
    meta = pd.DataFrame({"source_id": [1], "kwp": [5.0], "excluded": [False],
                         "valid_from_utc": [pd.NaT], "valid_to_utc": [pd.NaT]})
    h = aggregate_labels(labels, meta, cfg)
    assert h.iloc[0]["n_valid"] == 10 and h.iloc[0]["qc_flags"] == "missing,stuck"
    assert h.iloc[0]["target_kw"] == 2.0


def test_api_chunks_missing_dir_returns_three(tmp_path):
    from pvdata.io_dkasc import _load_api_chunks
    res = _load_api_chunks(tmp_path / "nope", tmp_path / "nope.csv", {52})
    assert len(res) == 3


def test_lstm_model_gradients_flow_during_training():
    """Verify gradients are not killed by clamp during training."""
    import torch
    import torch.nn as nn
    from jobs.retrain_tools.lstm_model import _make_module

    torch.manual_seed(42)
    model = _make_module(input_size=10, hidden_size=32, num_layers=1, dropout=0.0)
    model.train()

    x = torch.randn(16, 6, 10)
    y = torch.ones(16) * 0.5
    pred = model(x)
    loss = nn.MSELoss()(pred, y)
    loss.backward()

    # Gradients must be non-zero for head parameters
    for p in model.parameters():
        assert p.grad is not None
        assert p.grad.abs().sum().item() > 0.0

    # In eval mode, negative outputs must be clamped to zero
    model.eval()
    with torch.no_grad():
        eval_pred = model(x)
        assert (eval_pred >= 0.0).all().item()


def test_tracking_uri_uses_psycopg2_for_supabase_pooler(monkeypatch):
    """Ensure MLflow tracking URI uses postgresql+psycopg2 to avoid prepared statement issues."""
    from jobs.retrain_tools.mlflow_helpers import _get_tracking_uri

    monkeypatch.setenv(
        "MLFLOW_TRACKING_URI",
        "postgresql://postgres.xxx:pass@aws-0.pooler.supabase.com:5432/postgres",
    )
    uri = _get_tracking_uri()
    assert "pooler.supabase.com:6543" in uri
    assert uri.startswith("postgresql+psycopg2://")


