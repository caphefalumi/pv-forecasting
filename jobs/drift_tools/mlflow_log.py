"""
mlflow_log.py — Log data drift detection results to MLflow.

Uses a dedicated experiment ("pv-lstm-fleet-drift") to keep drift runs
separate from retrain and evaluation runs.

Each drift run records:
  Tags:
    - drift.detected         : "true" / "false"
    - drift.ks_triggered     : "true" / "false"
    - drift.psi_triggered    : "true" / "false"
    - drift.reason           : human-readable decision summary
    - drift.window_cur_start / cur_end
    - drift.window_ref_start / ref_end

  Params:
    - current_days           : int
    - reference_days         : int
    - n_rows_current         : int
    - n_rows_reference       : int
    - ks_alpha               : float
    - ks_threshold           : float
    - psi_threshold          : float
    - n_features_tested      : int

  Metrics (scalars):
    - drift_score            : fraction of features drifted (KS)
    - n_features_drifted     : count
    - target_psi             : PSI of target_norm

  Metrics (per-feature, logged with step = feature index):
    - feature_ks_stat        : KS statistic per feature
    - feature_ks_pvalue      : KS p-value per feature
    - feature_mean_delta     : cur_mean - ref_mean per feature
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Optional

logger = logging.getLogger(__name__)

DRIFT_EXPERIMENT_NAME = "pv-lstm-fleet-drift"


def log_drift_run(
    report,              # DriftReport from detector.detect_drift()
    cur_start:   date,
    cur_end:     date,
    ref_start:   date,
    ref_end:     date,
    current_days:   int,
    reference_days: int,
) -> str:
    """Log one drift detection run to MLflow and return the new run_id.

    Args:
        report:         DriftReport from detector.detect_drift().
        cur_start/end:  Current window date range.
        ref_start/end:  Reference window date range.
        current_days:   Length of the current window in days.
        reference_days: Length of the reference window in days.

    Returns:
        run_id (str): MLflow run ID of the newly logged drift run.
    """
    try:
        import mlflow
    except ImportError as exc:
        raise ImportError("mlflow is required: pip install mlflow") from exc

    from jobs.retrain_tools.mlflow_helpers import _get_tracking_uri

    tracking_uri = _get_tracking_uri()
    mlflow.set_tracking_uri(tracking_uri)
    mlflow.set_experiment(DRIFT_EXPERIMENT_NAME)

    with mlflow.start_run() as run:
        run_id = run.info.run_id

        # ── Tags ──────────────────────────────────────────────────────────────
        mlflow.set_tags({
            "drift.detected":       str(report.drift_detected).lower(),
            "drift.ks_triggered":   str(report.ks_triggered).lower(),
            "drift.psi_triggered":  str(report.psi_triggered).lower(),
            "drift.reason":         report.reason[:500],
            "drift.window_cur_start": str(cur_start),
            "drift.window_cur_end":   str(cur_end),
            "drift.window_ref_start": str(ref_start),
            "drift.window_ref_end":   str(ref_end),
        })

        # ── Params ────────────────────────────────────────────────────────────
        mlflow.log_params({
            "current_days":      current_days,
            "reference_days":    reference_days,
            "n_rows_current":    report.n_rows_current,
            "n_rows_reference":  report.n_rows_reference,
            "ks_alpha":          report.ks_alpha,
            "ks_threshold":      report.ks_threshold,
            "psi_threshold":     report.psi_threshold,
            "n_features_tested": report.n_features_tested,
        })

        # ── Scalar metrics ────────────────────────────────────────────────────
        scalar_metrics: dict[str, float] = {
            "drift_score":         report.drift_score,
            "n_features_drifted":  float(report.n_features_drifted),
        }
        if report.target_psi is not None:
            scalar_metrics["target_psi"] = report.target_psi

        mlflow.log_metrics(scalar_metrics)

        # ── Per-feature metrics (step = feature index) ────────────────────────
        for i, fr in enumerate(report.feature_results):
            mlflow.log_metric("feature_ks_stat",    fr.ks_stat,    step=i)
            mlflow.log_metric("feature_ks_pvalue",  fr.ks_pvalue,  step=i)
            mlflow.log_metric("feature_mean_delta", fr.mean_delta, step=i)

        # ── Feature drift table as artifact (Markdown) ────────────────────────
        import tempfile, os
        from pathlib import Path

        table_md = _build_feature_table(report)
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8"
        ) as f:
            f.write(table_md)
            tmp_path = f.name

        try:
            mlflow.log_artifact(tmp_path, artifact_path="drift_report")
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    logger.info(
        "Drift run logged: experiment='%s'  run_id=%s",
        DRIFT_EXPERIMENT_NAME, run_id,
    )
    return run_id


def _build_feature_table(report) -> str:
    """Build a Markdown table of per-feature KS results."""
    lines = [
        f"# Data Drift Report",
        f"",
        f"**Drift detected:** {'⚠️ YES' if report.drift_detected else '✅ NO'}",
        f"**Drift score (KS):** {report.drift_score:.2f}  "
        f"(threshold: {report.ks_threshold:.2f})",
        f"**Target PSI:** "
        + (f"{report.target_psi:.4f}" if report.target_psi is not None else "N/A")
        + f"  (threshold: {report.psi_threshold:.2f})",
        f"**Reason:** {report.reason}",
        f"",
        f"## Per-Feature KS Results",
        f"",
        f"| Feature | KS stat | p-value | Drifted | Ref mean | Cur mean | Δ mean |",
        f"|---------|---------|---------|---------|----------|----------|--------|",
    ]
    # Sort: drifted first, then by KS statistic descending
    sorted_results = sorted(
        report.feature_results,
        key=lambda r: (-int(r.drifted), -r.ks_stat),
    )
    for r in sorted_results:
        flag = "⚠️ YES" if r.drifted else "✅ no"
        lines.append(
            f"| {r.feature} | {r.ks_stat:.4f} | {r.ks_pvalue:.4f} | {flag} "
            f"| {r.ref_mean:.4f} | {r.cur_mean:.4f} | {r.mean_delta:+.4f} |"
        )
    return "\n".join(lines)
