"""
mlflow_log.py — Log champion evaluation results to MLflow.

Uses a dedicated experiment ("pv-lstm-fleet-eval") to keep evaluation runs
separate from retrain runs logged under "pv-lstm-fleet".

Each evaluation run records:
  Tags:
    - champion_version   : model version string, e.g. "3"
    - champion_run_id    : MLflow run ID of the champion training run
    - eval.needs_retrain : "true" / "false"
    - eval.reason        : human-readable drift decision reason
    - eval.ts_start      : earliest ts_utc_end in the evaluation window
    - eval.ts_end        : latest ts_utc_end in the evaluation window

  Params:
    - n_eval_rows    : number of filtered rows in the evaluation window
    - n_eval_windows : number of sliding windows built from those rows
    - n_recent_months: how many monthly CSVs were loaded

  Metrics:
    - live_rmse
    - live_mae
    - live_r2
    - champion_test_rmse (if available)
    - drift_ratio        (live_rmse / champion_test_rmse, if available)
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

EVAL_EXPERIMENT_NAME = "pv-lstm-fleet-eval"


def log_eval_run(
    live_metrics:          dict,
    champion_version:      str,
    champion_run_id:       str,
    champion_test_rmse:    Optional[float],
    needs_retrain:         bool,
    reason:                str,
    df_eval:               pd.DataFrame,
    n_windows:             int,
    n_recent_months:       int,
) -> str:
    """Log one evaluation run to MLflow and return the new run_id.

    Args:
        live_metrics:       Dict with keys "live_rmse", "live_mae", "live_r2".
        champion_version:   MLflow model version string of the champion.
        champion_run_id:    MLflow run_id of the champion's training run.
        champion_test_rmse: RMSE the champion achieved during training (or None).
        needs_retrain:      True if drift threshold exceeded.
        reason:             Human-readable drift decision string.
        df_eval:            Evaluation DataFrame (for timestamp metadata).
        n_windows:          Number of windows evaluated.
        n_recent_months:    Number of monthly CSVs downloaded.

    Returns:
        run_id (str): MLflow run ID of the newly logged evaluation run.
    """
    try:
        import mlflow
    except ImportError as exc:
        raise ImportError("mlflow is required: pip install mlflow") from exc

    from jobs.retrain_tools.mlflow_helpers import _get_tracking_uri

    tracking_uri = _get_tracking_uri()
    mlflow.set_tracking_uri(tracking_uri)
    mlflow.set_experiment(EVAL_EXPERIMENT_NAME)

    # Resolve evaluation window timestamps
    ts_start = ts_end = "unknown"
    if "ts_utc_end" in df_eval.columns:
        ts_series = pd.to_datetime(df_eval["ts_utc_end"])
        ts_start  = str(ts_series.min())
        ts_end    = str(ts_series.max())

    live_rmse = live_metrics.get("live_rmse", float("nan"))

    with mlflow.start_run() as run:
        run_id = run.info.run_id

        # ── Tags ──────────────────────────────────────────────────────────────
        mlflow.set_tags({
            "champion_version":   champion_version,
            "champion_run_id":    champion_run_id,
            "eval.needs_retrain": str(needs_retrain).lower(),
            "eval.reason":        reason[:500],    # MLflow tag value limit
            "eval.ts_start":      ts_start,
            "eval.ts_end":        ts_end,
            "model.type":         "LSTM",
        })

        # ── Params ────────────────────────────────────────────────────────────
        mlflow.log_params({
            "n_eval_rows":     len(df_eval),
            "n_eval_windows":  n_windows,
            "n_recent_months": n_recent_months,
            "champion_version": champion_version,
        })

        # ── Metrics ───────────────────────────────────────────────────────────
        metrics_to_log: dict[str, float] = {**live_metrics}

        if champion_test_rmse is not None:
            metrics_to_log["champion_test_rmse"] = champion_test_rmse
            if live_rmse and champion_test_rmse > 0:
                metrics_to_log["drift_ratio"] = live_rmse / champion_test_rmse

        mlflow.log_metrics(metrics_to_log)

    logger.info(
        "Evaluation run logged: experiment='%s'  run_id=%s",
        EVAL_EXPERIMENT_NAME, run_id,
    )
    return run_id
