"""Translate each job's results into the metrics the MLOps dashboard queries.

Only values the jobs actually compute are recorded; anything unavailable (e.g. no
champion baseline yet, dry-run promotion outcome) is skipped rather than faked.
"""

from __future__ import annotations

import logging
import re
from typing import Callable, Optional

from jobs.common.otel_metrics import JobMetrics

INGEST_JOB = "solar-ingest"
EVAL_JOB = "solar-evaluate"
DRIFT_JOB = "solar-drift"
RETRAIN_JOB = "solar-retrain"

logger = logging.getLogger(__name__)


def safe_record(fn: Callable, *args, **kwargs) -> None:
    """Call a record_* function; telemetry errors are logged, never raised."""
    try:
        fn(*args, **kwargs)
    except Exception as exc:
        logger.warning("Could not record job metrics (%s): %s", getattr(fn, "__name__", fn), exc)


def mlflow_safe_name(name: str) -> str:
    """MLflow metric keys allow [A-Za-z0-9_\\-. /]; replace anything else with '_'."""
    return re.sub(r"[^A-Za-z0-9_\-. /]", "_", name)


# ── Evaluate ──────────────────────────────────────────────────────────────────

def record_eval_metrics(
    jm: JobMetrics,
    live_metrics: dict,
    champion_version: Optional[str],
    champion_test_rmse: Optional[float],
    needs_retrain: bool,
) -> None:
    attrs = {"champion_version": str(champion_version) if champion_version is not None else None}
    live_rmse = live_metrics.get("live_rmse")
    jm.gauge("pv_eval_live_rmse", live_rmse, attrs, "Champion RMSE on the latest evaluation window")
    jm.gauge("pv_eval_live_mae", live_metrics.get("live_mae"), attrs, "Champion MAE on the latest evaluation window")
    jm.gauge("pv_eval_live_r2", live_metrics.get("live_r2"), attrs, "Champion R2 on the latest evaluation window")
    jm.gauge("pv_eval_champion_test_rmse", champion_test_rmse, attrs, "Champion test RMSE at training time")
    if champion_test_rmse is not None and champion_test_rmse > 0 and live_rmse is not None:
        jm.gauge(
            "pv_eval_rmse_ratio", float(live_rmse) / float(champion_test_rmse), attrs,
            "live_rmse / champion_test_rmse (retrain when > EVAL_DRIFT_THRESHOLD, default 1.15)",
        )
    jm.gauge("pv_eval_needs_retrain", 1 if needs_retrain else 0, attrs, "1 if the evaluation recommends retraining")


# ── Drift ─────────────────────────────────────────────────────────────────────

def record_drift_metrics(jm: JobMetrics, report) -> None:
    jm.gauge(
        "drift_score", report.drift_score, {"feature": "all"},
        "Share of features whose KS test drifted (drift when >= DRIFT_KS_THRESHOLD, default 0.40)",
    )
    for fr in report.feature_results:
        jm.gauge(
            "pv_drift_feature_ks_stat", fr.ks_stat, {"feature": fr.feature},
            "Two-sample KS statistic per feature, current vs reference window",
        )
    jm.gauge("pv_drift_n_features_drifted", report.n_features_drifted, None, "Number of features with KS p < alpha")
    jm.gauge("pv_drift_n_features_tested", report.n_features_tested, None, "Number of features KS-tested")
    jm.gauge(
        "pv_drift_target_psi", report.target_psi, {"feature": report.target_col},
        "PSI of the target (drift when > DRIFT_PSI_THRESHOLD, default 0.20)",
    )
    jm.gauge("pv_drift_detected", 1 if report.drift_detected else 0, None, "1 if data drift was detected")


# ── Ingest ────────────────────────────────────────────────────────────────────

def record_ingest_metrics(jm: JobMetrics, quality) -> None:
    jm.gauge(
        "pv_ingestion_schema_violations", quality.schema_violations, None,
        "Duplicate key rows + unauthorized columns in train_table (pvdata.validate hard checks 1-2)",
    )
    for flag_type, count in quality.range_flags.items():
        jm.gauge(
            "pv_ingestion_range_flags", count, {"flag_type": flag_type},
            "Hourly train_table rows carrying each 5-min QC flag in the latest month of data",
        )
    jm.gauge("pv_ingestion_rows", quality.rows_train_table, {"stage": "train_table"}, "Rows per ingest stage")
    jm.gauge("pv_ingestion_rows", quality.rows_usable, {"stage": "usable"}, "Rows per ingest stage")
    jm.gauge("pv_ingestion_rows", quality.rows_new, {"stage": "new"}, "Rows per ingest stage")
    jm.gauge(
        "pv_ingestion_data_lag_hours", quality.data_lag_hours, None,
        "Hours between now and the newest ts_utc_end in train_table",
    )


def record_ingest_uploads(jm: JobMetrics, n_files: int) -> None:
    jm.gauge("pv_ingestion_files_uploaded", n_files, None, "Monthly CSVs uploaded to Supabase this run")


# ── Retrain ───────────────────────────────────────────────────────────────────

def record_retrain_metrics(
    jm: JobMetrics,
    test_rmse: Optional[float],
    candidate_assigned: Optional[bool],
    model_version: Optional[str] = None,
    champion_rmse: Optional[float] = None,
) -> None:
    attrs = {"model_version": str(model_version) if model_version is not None else None}
    jm.gauge("pv_retrain_test_rmse", test_rmse, attrs, "Challenger RMSE on the test split")
    jm.gauge("pv_retrain_champion_test_rmse", champion_rmse, attrs, "Champion test RMSE it was compared against")
    if candidate_assigned is not None:
        jm.gauge(
            "pv_retrain_candidate_promoted", 1 if candidate_assigned else 0, attrs,
            "1 if the new version got the 'candidate' alias (champion promotion stays manual)",
        )
