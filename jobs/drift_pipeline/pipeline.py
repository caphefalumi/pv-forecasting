"""
pipeline.py — Orchestration for the PV data drift detection pipeline.

Steps:
  1. Load environment variables.
  2. Download monthly CSVs covering the two windows from Supabase.
  3. Apply daytime filter (ghi_clear > 0) to both windows.
  4. Run drift detection:
       a. KS test for all BASE_FEATURES + target_norm.
       b. PSI for target_norm.
  5. Decide: DATA DRIFT DETECTED or NO SIGNIFICANT DRIFT.
  6. Log results to MLflow experiment 'pv-lstm-fleet-drift'.
  7. Send Discord notification.

Console summary (only two possible outcomes):
  ⚠️  DATA DRIFT DETECTED   — action recommended
  ✅  NO SIGNIFICANT DRIFT  — data stable, no action needed

Windows (non-overlapping, daytime rows only):
  current   : last 7 days      (configurable via DRIFT_CURRENT_DAYS)
  reference : preceding 30 days (configurable via DRIFT_REFERENCE_DAYS)

Call:
    from jobs.drift_pipeline.pipeline import run_drift_pipeline
    run_drift_pipeline()
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]


# ── Public entry point ─────────────────────────────────────────────────────────

def run_drift_pipeline(dry_run: bool = False) -> bool:
    """Run the full data drift detection pipeline end-to-end.

    Args:
        dry_run: If True, skip MLflow logging and Discord notification.

    Returns:
        drift_detected (bool): True if significant data drift was found.
    """
    import os

    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))

    _load_env()

    from jobs.drift_tools.data_loader    import load_windows, CURRENT_DAYS, REFERENCE_DAYS
    from jobs.drift_tools.detector       import detect_drift
    from jobs.drift_tools.mlflow_log     import log_drift_run, DRIFT_EXPERIMENT_NAME
    from jobs.drift_tools.discord_notify import (
        send_drift_notification, send_drift_error_notification,
    )
    from jobs.retrain_tools.mlflow_helpers import get_mlflow_run_url

    # Allow overriding window sizes via env vars
    current_days   = int(os.environ.get("DRIFT_CURRENT_DAYS",   CURRENT_DAYS))
    reference_days = int(os.environ.get("DRIFT_REFERENCE_DAYS", REFERENCE_DAYS))

    try:
        # ── Step 1: Load windows ───────────────────────────────────────────────
        logger.info(
            "=== [Step 1/4] Loading windows "
            "(current=%d days, reference=%d days) ===",
            current_days, reference_days,
        )
        df_current, df_reference, cur_start, cur_end, ref_start, ref_end = \
            load_windows(
                current_days=current_days,
                reference_days=reference_days,
            )

        # ── Step 2: Drift detection ────────────────────────────────────────────
        logger.info("=== [Step 2/4] Running drift detection ===")
        report = detect_drift(df_reference, df_current)

        # ── Step 3: Console summary ────────────────────────────────────────────
        logger.info("=== [Step 3/4] Printing summary ===")
        _print_summary(
            report=report,
            cur_start=cur_start, cur_end=cur_end,
            ref_start=ref_start, ref_end=ref_end,
        )

        # ── Step 4: MLflow + Discord ───────────────────────────────────────────
        logger.info("=== [Step 4/4] Reporting results ===")

        drift_run_id  = "dry-run"
        mlflow_url    = "http://127.0.0.1:5001"

        if not dry_run:
            drift_run_id = log_drift_run(
                report=report,
                cur_start=cur_start,     cur_end=cur_end,
                ref_start=ref_start,     ref_end=ref_end,
                current_days=current_days,
                reference_days=reference_days,
            )
            mlflow_url = get_mlflow_run_url(drift_run_id)

            send_drift_notification(
                report=report,
                cur_start=cur_start,      cur_end=cur_end,
                ref_start=ref_start,      ref_end=ref_end,
                drift_run_id=drift_run_id,
                mlflow_drift_url=mlflow_url,
                dry_run=False,
            )
        else:
            logger.info("DRY RUN — skipping MLflow logging and Discord.")
            send_drift_notification(
                report=report,
                cur_start=cur_start,      cur_end=cur_end,
                ref_start=ref_start,      ref_end=ref_end,
                drift_run_id="dry-run",
                mlflow_drift_url=mlflow_url,
                dry_run=True,
            )

        logger.info("=== Drift detection pipeline complete ===")
        return report.drift_detected

    except Exception as exc:
        logger.error("❌ Drift detection pipeline failed: %s", exc, exc_info=True)
        if not dry_run:
            try:
                send_drift_error_notification(str(exc))
            except Exception:
                pass
        raise


# ── Internal helpers ───────────────────────────────────────────────────────────

def _load_env() -> None:
    """Load .env from repo root. Mirrors retrain pipeline pattern."""
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        logger.debug(".env not found — relying on ambient env vars")
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(env_path, override=True)
        logger.debug("Loaded .env from %s", env_path)
    except ImportError:
        logger.warning(
            "python-dotenv not installed — set env vars manually: "
            "SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, SUPABASE_RAW_DATA_BUCKET, "
            "MLFLOW_TRACKING_URI, DISCORD_WEBHOOK_URL"
        )


def _print_summary(report, cur_start, cur_end, ref_start, ref_end) -> None:
    """Print a concise drift summary to stdout."""
    sep = "=" * 60

    if report.drift_detected:
        verdict = "⚠️  DATA DRIFT DETECTED"
    else:
        verdict = "✅  NO SIGNIFICANT DRIFT"

    # Top-5 drifted features
    top_drifted = sorted(
        (r for r in report.feature_results if r.drifted),
        key=lambda r: -r.ks_stat,
    )[:5]

    lines = [
        "",
        sep,
        "  PV-LSTM Data Drift Detection",
        sep,
        f"  Reference window : {ref_start} → {ref_end}"
        f"  ({report.n_rows_reference:,} rows)",
        f"  Current window   : {cur_start} → {cur_end}"
        f"  ({report.n_rows_current:,} rows)",
        "",
        f"  KS drift score   : {report.drift_score:.2f}"
        f"  ({report.n_features_drifted}/{report.n_features_tested} features)"
        f"  [threshold: {report.ks_threshold:.2f}]"
        + ("  ⚠️ TRIGGERED" if report.ks_triggered else "  ✅ OK"),
    ]

    if report.target_psi is not None:
        psi_flag = "  ⚠️ TRIGGERED" if report.psi_triggered else "  ✅ OK"
        lines.append(
            f"  Target PSI       : {report.target_psi:.4f}"
            f"  [threshold: {report.psi_threshold:.2f}]{psi_flag}"
        )

    if top_drifted:
        lines.append("")
        lines.append("  Top drifted features:")
        for r in top_drifted:
            lines.append(
                f"    {r.feature:<28}  KS={r.ks_stat:.3f}"
                f"  p={r.ks_pvalue:.4f}  Δmean={r.mean_delta:+.4f}"
            )

    lines += [
        "",
        f"  {verdict}",
        f"  Reason: {report.reason}",
        sep,
        "",
    ]

    print("\n".join(lines), flush=True)
