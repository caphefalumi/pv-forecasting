"""Tests for jobs.common.otel_metrics and each job's Grafana metric emission.

No network: exports go to an in-memory reader or a capturing fake exporter.
"""

from __future__ import annotations

import contextlib
import math
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from opentelemetry.sdk.metrics.export import (
    InMemoryMetricReader,
    MetricExporter,
    MetricExportResult,
)

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jobs.common import otel_metrics as om  # noqa: E402
from jobs.common.job_results import (  # noqa: E402
    DRIFT_JOB,
    EVAL_JOB,
    INGEST_JOB,
    RETRAIN_JOB,
    mlflow_safe_name,
    record_drift_metrics,
    record_eval_metrics,
    record_ingest_metrics,
    record_retrain_metrics,
)

OTEL_ENV = (
    "OTEL_SDK_DISABLED",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_HEADERS",
    "GRAFANA_CLOUD_API_TOKEN",
    "GRAFANA_OTLP_INSTANCE_ID",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in OTEL_ENV:
        monkeypatch.delenv(k, raising=False)


def _collect(reader: InMemoryMetricReader) -> dict:
    """{metric_name: {"unit", "type", "points": [(value, attrs)], "resource": {...}}}"""
    out: dict = {}
    data = reader.get_metrics_data()
    for rm in data.resource_metrics:
        res = dict(rm.resource.attributes)
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                out[m.name] = {
                    "unit": m.unit,
                    "type": type(m.data).__name__,
                    "points": [(p.value, dict(p.attributes)) for p in m.data.data_points],
                    "resource": res,
                }
    return out


def _export(jm: om.JobMetrics) -> dict:
    reader = InMemoryMetricReader()
    jm.reader = reader
    assert jm.close() is True
    return _collect(reader)


class CapturingExporter(MetricExporter):
    """Stands in for OTLPMetricExporter; records constructor args and exported data."""

    instances: list["CapturingExporter"] = []

    def __init__(self, endpoint=None, headers=None, timeout=None, **_):
        super().__init__()
        self.endpoint, self.headers, self.timeout = endpoint, headers, timeout
        self.exported = []
        self.shut = False
        CapturingExporter.instances.append(self)

    def export(self, metrics_data, timeout_millis: float = 10_000, **kwargs):
        self.exported.append(metrics_data)
        return MetricExportResult.SUCCESS

    def force_flush(self, timeout_millis: float = 10_000) -> bool:
        return True

    def shutdown(self, timeout_millis: float = 30_000, **kwargs) -> None:
        self.shut = True


class FailingExporter(CapturingExporter):
    def export(self, metrics_data, timeout_millis: float = 10_000, **kwargs):
        raise ConnectionError("collector down")


@pytest.fixture
def otlp_env(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://otlp.example/otlp/")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "Authorization=Basic%20dG9rOnNlY3JldA==")
    CapturingExporter.instances = []
    import opentelemetry.exporter.otlp.proto.http.metric_exporter as mod
    monkeypatch.setattr(mod, "OTLPMetricExporter", CapturingExporter)
    return mod


def _exported_names(exporter: CapturingExporter) -> dict:
    out = {}
    for data in exporter.exported:
        for rm in data.resource_metrics:
            for sm in rm.scope_metrics:
                for m in sm.metrics:
                    out[m.name] = {
                        "points": [(p.value, dict(p.attributes)) for p in m.data.data_points],
                        "resource": dict(rm.resource.attributes),
                    }
    return out


# ── Config / headers ──────────────────────────────────────────────────────────

def test_no_headers_means_disabled(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://otlp.example/otlp")
    assert om.export_config() is None


def test_no_endpoint_means_disabled(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "Authorization=Basic abc")
    assert om.export_config() is None


def test_sdk_disabled_wins(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://otlp.example/otlp")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "Authorization=Basic abc")
    monkeypatch.setenv("OTEL_SDK_DISABLED", "TRUE")
    assert om.export_config() is None


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Authorization=Basic abc+/=", "Basic abc+/="),
        ("Authorization=Basic%20abc%2B%2F%3D", "Basic abc+/="),
        ("X-Other=1, Authorization=Basic%20abc+", "Basic abc+"),
    ],
)
def test_headers_url_decoded_like_backend(monkeypatch, raw, expected):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://otlp.example/otlp/")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", raw)
    endpoint, headers = om.export_config()
    assert endpoint == "https://otlp.example/otlp/v1/metrics"
    assert headers["Authorization"] == expected


def test_grafana_token_fallback(monkeypatch):
    import base64

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://otlp.example/otlp")
    monkeypatch.setenv("GRAFANA_OTLP_INSTANCE_ID", "123")
    monkeypatch.setenv("GRAFANA_CLOUD_API_TOKEN", "glc_x")
    _, headers = om.export_config()
    assert headers["Authorization"] == "Basic " + base64.b64encode(b"123:glc_x").decode()


# ── No-op behaviour ───────────────────────────────────────────────────────────

def test_close_without_config_is_silent_noop(monkeypatch):
    import opentelemetry.exporter.otlp.proto.http.metric_exporter as mod

    def boom(*a, **k):
        raise AssertionError("exporter must not be built without OTLP config")

    monkeypatch.setattr(mod, "OTLPMetricExporter", boom)
    jm = om.JobMetrics("solar-evaluate")
    jm.gauge("pv_eval_live_rmse", 0.1)
    assert jm.close() is False


def test_disabled_dry_run_never_exports(otlp_env):
    with om.job_metrics("solar-evaluate", enabled=False) as jm:
        jm.gauge("pv_eval_live_rmse", 0.1)
    assert CapturingExporter.instances == []


def test_none_nan_inf_are_skipped():
    jm = om.JobMetrics("solar-drift")
    jm.gauge("a", None)
    jm.gauge("b", float("nan"))
    jm.gauge("c", math.inf)
    jm.gauge("d", 1)
    assert set(jm.values()) == {"d"}


def test_same_attributes_last_write_wins():
    jm = om.JobMetrics("solar-drift")
    jm.gauge("g", 1, {"feature": "x"})
    jm.gauge("g", 2, {"feature": "x"})
    jm.gauge("g", 3, {"feature": "y"})
    assert sorted(v for v, _ in jm.values()["g"]) == [2, 3]


# ── Export shape ──────────────────────────────────────────────────────────────

def test_gauges_have_no_unit_and_service_attribute():
    jm = om.JobMetrics("solar-evaluate")
    jm.gauge("pv_eval_live_rmse", 0.12, {"champion_version": "3"}, "desc")
    jm.record_run(12.5, True)
    got = _export(jm)
    for name in ("pv_eval_live_rmse", "pv_batch_job_duration_seconds", "pv_batch_job_success"):
        assert got[name]["unit"] == ""
        assert got[name]["type"] == "Gauge"
        assert got[name]["resource"]["service.name"] == "solar-evaluate"
        assert got[name]["resource"]["service.instance.id"] == "solar-evaluate"
        for _, attrs in got[name]["points"]:
            assert attrs["service"] == "solar-evaluate"
            assert "job" not in attrs
    assert got["pv_eval_live_rmse"]["points"] == [(0.12, {"service": "solar-evaluate", "champion_version": "3"})]
    assert got["pv_batch_job_duration_seconds"]["points"][0] == (
        12.5, {"service": "solar-evaluate", "job_name": "solar-evaluate"},
    )
    assert got["pv_batch_job_success"]["points"][0][0] == 1


def test_record_run_exports_last_run_timestamp():
    jm = om.JobMetrics("solar-ingest")
    jm.record_run(3.0, False, finished_at=1_760_000_000.5)
    got = _export(jm)
    ts = got["pv_batch_job_last_run_timestamp_seconds"]
    assert ts["unit"] == "" and ts["type"] == "Gauge"
    assert ts["points"] == [(1_760_000_000.5, {"service": "solar-ingest", "job_name": "solar-ingest"})]


@pytest.mark.parametrize("fail", [False, True])
def test_job_metrics_sends_last_run_timestamp_at_exit(otlp_env, fail):
    before = time.time()
    with pytest.raises(RuntimeError) if fail else contextlib.nullcontext():
        with om.job_metrics("solar-retrain"):
            if fail:
                raise RuntimeError("boom")
    after = time.time()
    (exp,) = CapturingExporter.instances
    names = _exported_names(exp)
    ((value, attrs),) = names["pv_batch_job_last_run_timestamp_seconds"]["points"]
    assert before <= value <= after
    assert attrs == {"service": "solar-retrain", "job_name": "solar-retrain"}
    assert names["pv_batch_job_success"]["points"][0][0] == (0 if fail else 1)


def test_export_through_otlp_exporter_flushes_and_shuts_down(otlp_env):
    with om.job_metrics("solar-drift") as jm:
        jm.gauge("drift_score", 0.5, {"feature": "all"})
    (exp,) = CapturingExporter.instances
    assert exp.endpoint == "https://otlp.example/otlp/v1/metrics"
    assert exp.headers == {"Authorization": "Basic dG9rOnNlY3JldA=="}
    assert exp.shut is True
    names = _exported_names(exp)
    assert names["drift_score"]["points"] == [(0.5, {"service": "solar-drift", "feature": "all"})]
    assert names["pv_batch_job_success"]["points"][0] == (1, {"service": "solar-drift", "job_name": "solar-drift"})


# ── Failures never fail the job ───────────────────────────────────────────────

def test_export_failure_is_swallowed(otlp_env, monkeypatch):
    monkeypatch.setattr(otlp_env, "OTLPMetricExporter", FailingExporter)
    with om.job_metrics("solar-ingest") as jm:
        jm.gauge("pv_ingestion_rows", 10, {"stage": "usable"})
    # no exception raised


def test_exporter_constructor_failure_is_swallowed(otlp_env, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("bad config")

    monkeypatch.setattr(otlp_env, "OTLPMetricExporter", broken)
    jm = om.JobMetrics("solar-ingest")
    jm.gauge("x", 1)
    assert jm.close() is False


def test_reader_failure_is_swallowed():
    class BadReader(InMemoryMetricReader):
        def force_flush(self, timeout_millis: float = 10_000) -> bool:
            raise RuntimeError("boom")

    jm = om.JobMetrics("solar-ingest", reader=BadReader())
    jm.gauge("x", 1)
    assert jm.close() is False


def test_job_failure_records_success_zero_and_reraises(otlp_env):
    with pytest.raises(ValueError), om.job_metrics("solar-retrain"):
        raise ValueError("training blew up")
    names = _exported_names(CapturingExporter.instances[0])
    assert names["pv_batch_job_success"]["points"][0][0] == 0
    assert names["pv_batch_job_duration_seconds"]["points"][0][0] >= 0


@pytest.mark.parametrize("code, ok", [(0, 1), (2, 1), (1, 0)])
def test_system_exit_codes(otlp_env, code, ok):
    with pytest.raises(SystemExit), om.job_metrics("solar-evaluate"):
        sys.exit(code)
    names = _exported_names(CapturingExporter.instances[0])
    assert names["pv_batch_job_success"]["points"][0][0] == ok


# ── Per-job metric recording ──────────────────────────────────────────────────

def test_eval_metrics_with_champion_baseline():
    jm = om.JobMetrics(EVAL_JOB)
    record_eval_metrics(
        jm, {"live_rmse": 0.12, "live_mae": 0.08, "live_r2": 0.9},
        champion_version="4", champion_test_rmse=0.1, needs_retrain=True,
    )
    got = _export(jm)
    attrs = {"service": "solar-evaluate", "champion_version": "4"}
    assert got["pv_eval_live_rmse"]["points"] == [(0.12, attrs)]
    assert got["pv_eval_live_mae"]["points"] == [(0.08, attrs)]
    assert got["pv_eval_live_r2"]["points"] == [(0.9, attrs)]
    assert got["pv_eval_champion_test_rmse"]["points"] == [(0.1, attrs)]
    assert got["pv_eval_rmse_ratio"]["points"][0][0] == pytest.approx(1.2)
    assert got["pv_eval_needs_retrain"]["points"] == [(1, attrs)]


def test_eval_metrics_without_baseline_skip_ratio():
    jm = om.JobMetrics(EVAL_JOB)
    record_eval_metrics(jm, {"live_rmse": 0.2, "live_mae": 0.1, "live_r2": 0.5}, "1", None, True)
    vals = jm.values()
    assert "pv_eval_rmse_ratio" not in vals
    assert "pv_eval_champion_test_rmse" not in vals
    assert vals["pv_eval_needs_retrain"][0][0] == 1


def _drift_report():
    from jobs.drift_tools.detector import DriftReport, FeatureDriftResult

    def fr(name, ks, drifted):
        return FeatureDriftResult(name, ks, 0.01 if drifted else 0.5, drifted, 0, 0, 0, 1, 1)

    return DriftReport(
        feature_results=[fr("ghi_fc", 0.31, True), fr("temp_fc", 0.05, False)],
        n_features_tested=2, n_features_drifted=1, drift_score=0.5,
        target_psi=0.07, drift_detected=True, ks_triggered=True,
    )


def test_drift_metrics_by_feature_name():
    jm = om.JobMetrics(DRIFT_JOB)
    record_drift_metrics(jm, _drift_report())
    got = _export(jm)
    assert got["drift_score"]["points"] == [(0.5, {"service": "solar-drift", "feature": "all"})]
    ks = {a["feature"]: v for v, a in got["pv_drift_feature_ks_stat"]["points"]}
    assert ks == {"ghi_fc": 0.31, "temp_fc": 0.05}
    assert got["pv_drift_n_features_drifted"]["points"][0][0] == 1
    assert got["pv_drift_target_psi"]["points"] == [(0.07, {"service": "solar-drift", "feature": "target_norm"})]
    assert got["pv_drift_detected"]["points"][0][0] == 1


def test_drift_metrics_skip_missing_psi():
    rep = _drift_report()
    rep.target_psi = None
    jm = om.JobMetrics(DRIFT_JOB)
    record_drift_metrics(jm, rep)
    assert "pv_drift_target_psi" not in jm.values()


def _train_table():
    ts = pd.date_range("2026-09-30 12:00", periods=48, freq="h", tz="UTC")
    df = pd.DataFrame({
        "source_id": 1,
        "ts_utc_end": ts,
        "ts_local_start": (ts.tz_convert("Australia/Darwin") - pd.Timedelta(hours=1)).tz_localize(None),
        "target_kw": 1.0,
        "ghi_clear": np.r_[np.zeros(12), np.ones(36)],
        "ghi_fc": 1.0,
        "qc_flags": [""] * 40 + ["neg"] * 3 + ["over_kwp,stuck"] * 2 + ["day_zero"] * 3,
    })
    return df


def test_ingest_quality_counts_real_qc_flags_and_schema():
    from jobs.ingest_tools.quality import summarize_ingest

    df = _train_table()
    usable = df[df["ghi_clear"] > 0]
    q = summarize_ingest(df, usable, latest_ts=pd.Timestamp("2026-10-01 23:00", tz="UTC"),
                         now=pd.Timestamp("2026-10-02 14:00", tz="UTC"))
    assert q.flag_month == "2026-10"
    assert q.range_flags == {"missing": 0, "neg": 3, "over_kwp": 2, "stuck": 2, "day_zero": 3}
    assert q.schema_violations == 0
    assert q.rows_train_table == 48 and q.rows_usable == 36
    assert q.rows_new == 12
    assert q.data_lag_hours == pytest.approx(3.0)

    bad = pd.concat([df, df.iloc[:2]]).assign(station_ghi=1.0)
    qb = summarize_ingest(bad, bad)
    assert qb.duplicate_keys == 2 and qb.unauthorized_cols == 1 and qb.schema_violations == 3
    assert qb.rows_new is None


def test_ingest_metrics_exported():
    from jobs.ingest_tools.quality import summarize_ingest

    df = _train_table()
    jm = om.JobMetrics(INGEST_JOB)
    record_ingest_metrics(jm, summarize_ingest(df, df, now=pd.Timestamp("2026-10-02 14:00", tz="UTC")))
    got = _export(jm)
    flags = {a["flag_type"]: v for v, a in got["pv_ingestion_range_flags"]["points"]}
    assert flags["neg"] == 3 and flags["missing"] == 0
    assert got["pv_ingestion_schema_violations"]["points"][0][0] == 0
    stages = {a["stage"]: v for v, a in got["pv_ingestion_rows"]["points"]}
    assert stages == {"train_table": 48, "usable": 48}  # no "new" without a Supabase baseline
    assert got["pv_ingestion_data_lag_hours"]["points"][0][0] == pytest.approx(3.0)


@pytest.mark.parametrize("assigned, expected", [(True, 1), (False, 0)])
def test_retrain_metrics(assigned, expected):
    jm = om.JobMetrics(RETRAIN_JOB)
    record_retrain_metrics(jm, 0.09, assigned, model_version="7", champion_rmse=0.1)
    got = _export(jm)
    attrs = {"service": "solar-retrain", "model_version": "7"}
    assert got["pv_retrain_test_rmse"]["points"] == [(0.09, attrs)]
    assert got["pv_retrain_candidate_promoted"]["points"] == [(expected, attrs)]


def test_retrain_dry_run_has_no_promotion_metric():
    jm = om.JobMetrics(RETRAIN_JOB)
    record_retrain_metrics(jm, 0.09, None)
    assert "pv_retrain_candidate_promoted" not in jm.values()


def test_mlflow_safe_name():
    assert mlflow_safe_name("ghi_fc") == "ghi_fc"
    assert mlflow_safe_name("temp(°C)") == "temp__C_"


# ── Pipelines + entry points emit through the exporter ────────────────────────

def test_evaluate_entry_point_emits(otlp_env, monkeypatch):
    import evaluate
    import jobs.evaluate_pipeline.pipeline as ep
    import jobs.evaluate_tools.data_loader as dl
    import jobs.evaluate_tools.discord_notify as dn
    import jobs.evaluate_tools.inference as inf
    import jobs.evaluate_tools.model_loader as ml

    df = pd.DataFrame({"source_id": [1, 2], "ts_utc_end": ["2026-10-18T03:00Z", "2026-10-18T04:00Z"]})
    monkeypatch.setattr(dl, "load_recent_rows", lambda **k: df)
    monkeypatch.setattr(ml, "load_champion_checkpoint", lambda: ({}, "5", "run-x", 0.10))
    monkeypatch.setattr(ml, "build_model_from_checkpoint", lambda ckpt: object())
    y_true = np.array([0.1, 0.2, 0.3, 0.4])
    monkeypatch.setattr(inf, "run_inference", lambda c, m, d: (y_true + 0.05, y_true, 4))
    monkeypatch.setattr(dn, "send_eval_notification", lambda **k: None)
    monkeypatch.setattr(ep, "_load_env", lambda: None)

    # --dry-run never exports, so run the pipeline directly with metrics enabled.
    with om.job_metrics(EVAL_JOB) as jm:
        needs = ep.run_evaluate_pipeline(dry_run=True, metrics=jm)
    names = _exported_names(CapturingExporter.instances[0])
    assert names["pv_eval_live_rmse"]["points"][0][0] == pytest.approx(0.05)
    assert names["pv_eval_rmse_ratio"]["points"][0][0] == pytest.approx(0.5)
    assert names["pv_eval_needs_retrain"]["points"][0][0] == int(needs) == 0
    assert names["pv_batch_job_success"]["points"][0][1]["job_name"] == "solar-evaluate"

    # Entry point: --dry-run exits 0 and builds no exporter.
    CapturingExporter.instances = []
    with pytest.raises(SystemExit) as exc:
        evaluate.main(["--dry-run"])
    assert exc.value.code == 0
    assert CapturingExporter.instances == []


def test_drift_pipeline_emits_per_feature(otlp_env, monkeypatch):
    from datetime import date

    import jobs.drift_pipeline.pipeline as dp
    import jobs.drift_tools.data_loader as dl
    import jobs.drift_tools.discord_notify as dn

    rng = np.random.default_rng(0)
    feats = dl.DRIFT_FEATURES
    ref = pd.DataFrame({c: rng.normal(0, 1, 400) for c in feats})
    cur = pd.DataFrame({c: rng.normal(0, 1, 200) for c in feats})
    cur[feats[0]] += 3.0  # one clearly drifted feature
    d = date(2026, 10, 1)
    monkeypatch.setattr(dl, "load_windows", lambda **k: (cur, ref, d, d, d, d))
    monkeypatch.setattr(dn, "send_drift_notification", lambda **k: None)
    monkeypatch.setattr(dp, "_load_env", lambda: None)

    with om.job_metrics(DRIFT_JOB) as jm:
        dp.run_drift_pipeline(dry_run=True, metrics=jm)
    names = _exported_names(CapturingExporter.instances[0])
    ks = {a["feature"]: v for v, a in names["pv_drift_feature_ks_stat"]["points"]}
    assert feats[0] in ks and ks[feats[0]] > 0.5
    assert "target_norm" not in ks
    assert 0 < names["drift_score"]["points"][0][0] <= 1
    assert "pv_drift_target_psi" in names


def test_drift_mlflow_logs_named_ks(monkeypatch):
    """log_drift_run adds ks_stat_<feature> next to the step-indexed metrics."""
    import types

    logged: dict = {}

    class _Run:
        info = types.SimpleNamespace(run_id="r1")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    fake = types.SimpleNamespace(
        set_tracking_uri=lambda *a: None, set_experiment=lambda *a: None,
        start_run=lambda: _Run(), set_tags=lambda *a: None, log_params=lambda *a: None,
        log_metrics=lambda m: logged.update(m), log_metric=lambda *a, **k: None,
        log_artifact=lambda *a, **k: None,
    )
    monkeypatch.setitem(sys.modules, "mlflow", fake)
    import jobs.retrain_tools.mlflow_helpers as mh
    monkeypatch.setattr(mh, "_get_tracking_uri", lambda: "sqlite://")
    from datetime import date

    from jobs.drift_tools.mlflow_log import log_drift_run

    d = date(2026, 10, 1)
    log_drift_run(_drift_report(), d, d, d, d, 7, 30)
    assert logged["ks_stat_ghi_fc"] == 0.31 and logged["ks_stat_temp_fc"] == 0.05
    assert logged["drift_score"] == 0.5


def test_ingest_pipeline_emits(otlp_env, monkeypatch, tmp_path):
    import jobs.ingest_pipeline.pipeline as ip
    import jobs.ingest_tools.bootstrap as bs
    import jobs.ingest_tools.dvc_helpers as dvc
    import pvdata.dataset as ds

    monkeypatch.setattr(ip, "EXPORT_DIR", tmp_path)
    monkeypatch.setattr(ip, "_load_env", lambda: None)
    monkeypatch.setattr(dvc, "ensure_dvc_initialized", lambda *a, **k: None)
    monkeypatch.setattr(dvc, "dvc_add", lambda *a, **k: None)
    monkeypatch.setattr(bs, "ensure_raw_data", lambda *a, **k: None)
    monkeypatch.setattr(ds, "load_table", lambda version="latest": (_train_table(), {}))

    with om.job_metrics(INGEST_JOB) as jm:
        ip.run_ingest_pipeline(skip_fetch=True, dry_run=True, metrics=jm)
    names = _exported_names(CapturingExporter.instances[0])
    flags = {a["flag_type"]: v for v, a in names["pv_ingestion_range_flags"]["points"]}
    assert flags == {"missing": 0, "neg": 3, "over_kwp": 2, "stuck": 2, "day_zero": 3}
    assert names["pv_ingestion_files_uploaded"]["points"][0][0] == 0  # dry run uploads nothing
    assert names["pv_batch_job_success"]["points"][0][0] == 1


def test_retrain_entry_point_records_failure(otlp_env, monkeypatch):
    import jobs.retrain_pipeline.pipeline as rp
    import retrain

    def failing(dry_run=False, metrics=None):
        metrics.gauge("pv_retrain_test_rmse", 0.2)
        raise RuntimeError("no data")

    monkeypatch.setattr(rp, "run_retrain_pipeline", failing)
    monkeypatch.setattr(sys, "argv", ["retrain.py"])
    with pytest.raises(SystemExit) as exc:
        retrain.main([])
    assert exc.value.code == 1
    names = _exported_names(CapturingExporter.instances[0])
    assert names["pv_batch_job_success"]["points"][0] == (0, {"service": "solar-retrain", "job_name": "solar-retrain"})
    assert names["pv_retrain_test_rmse"]["points"][0][0] == 0.2
