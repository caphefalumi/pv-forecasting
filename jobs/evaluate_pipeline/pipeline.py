"""
pipeline.py — Orchestration for the PV LSTM champion evaluation pipeline.

Steps:
  1. Load environment variables.
  2. Load the two most-recent monthly CSVs from Supabase; take the last 500 rows.
  3. Resolve the 'champion' alias in MLflow Model Registry.
  4. Download the champion checkpoint from Supabase artifacts/.
  5. Build sliding windows from the evaluation rows.
  6. Run model inference.
  7. Compute live metrics (RMSE, MAE, R²).
  8. Decide: RETRAIN NEEDED or RETRAIN NOT NEEDED (based on drift threshold).
  9. Log results to MLflow experiment 'pv-lstm-fleet-eval'.
 10. Send Discord notification with the result embed.

Console summary (the only two outcomes printed to stdout):
  ⚠️  RETRAIN NEEDED   — live RMSE has drifted beyond the threshold
  ✅  RETRAIN NOT NEEDED — champion is performing within acceptable range

Call:
    from jobs.evaluate_pipeline.pipeline import run_evaluate_pipeline
    run_evaluate_pipeline()
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]


# ── Public entry point ─────────────────────────────────────────────────────────

def run_evaluate_pipeline(dry_run: bool = False, metrics=None) -> bool:
    """Run the full champion evaluation pipeline end-to-end.

    Args:
        dry_run: If True, skip MLflow logging and Discord.
                 Data loading, inference, and metrics still run.
        metrics: Optional jobs.common.otel_metrics.JobMetrics; receives the live
                 metrics and retrain decision for Grafana.

    Returns:
        needs_retrain (bool): True if retrain is recommended.
    """
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))

    _load_env()

    from jobs.evaluate_tools.data_loader    import load_recent_rows, N_RECENT_MONTHS, N_EVAL_ROWS, BASE_FEATURES
    from jobs.evaluate_tools.model_loader   import load_champion_checkpoint, build_model_from_checkpoint
    from jobs.evaluate_tools.inference      import run_inference
    from jobs.evaluate_tools.drift          import decide_retrain
    from jobs.evaluate_tools.mlflow_log     import log_eval_run, EVAL_EXPERIMENT_NAME
    from jobs.evaluate_tools.discord_notify import send_eval_notification, send_eval_error_notification
    from jobs.retrain_tools.evaluation      import compute_metrics
    from jobs.retrain_tools.mlflow_helpers  import get_mlflow_run_url

    try:
        # ── Step 1: Load recent data ───────────────────────────────────────────
        logger.info(
            "=== [Step 1/5] Loading %d most-recent monthly CSVs (last %d rows) ===",
            N_RECENT_MONTHS, N_EVAL_ROWS,
        )
        df_eval = load_recent_rows(n_months=N_RECENT_MONTHS, n_rows=N_EVAL_ROWS)
        logger.info(
            "Evaluation dataset: %d rows × %d cols  |  arrays: %d",
            *df_eval.shape,
            df_eval["source_id"].nunique() if "source_id" in df_eval.columns else -1,
        )

        # Resolve timestamp range for logging
        ts_start = ts_end = "unknown"
        if "ts_utc_end" in df_eval.columns:
            import pandas as pd
            ts_series = pd.to_datetime(df_eval["ts_utc_end"])
            ts_start  = str(ts_series.min())
            ts_end    = str(ts_series.max())

        # ── Step 2: Load champion model ────────────────────────────────────────
        logger.info("=== [Step 2/5] Loading champion model from MLflow + Supabase ===")
        checkpoint, champion_version, champion_run_id, champion_test_rmse = \
            load_champion_checkpoint()
        model = build_model_from_checkpoint(checkpoint)

        # ── Step 3: Inference ──────────────────────────────────────────────────
        logger.info("=== [Step 3/5] Running inference on evaluation window ===")
        y_pred, y_true, n_windows = run_inference(checkpoint, model, df_eval)

        # ── Step 4: Compute metrics ────────────────────────────────────────────
        logger.info("=== [Step 4/5] Computing live metrics ===")
        raw_metrics  = compute_metrics(y_true, y_pred, prefix="live_")
        live_metrics = {
            "live_rmse": raw_metrics["live_rmse"],
            "live_mae":  raw_metrics["live_mae"],
            "live_r2":   raw_metrics["live_r2"],
        }
        live_rmse = live_metrics["live_rmse"]

        # ── Step 5: Drift decision ─────────────────────────────────────────────
        logger.info("=== [Step 5/5] Drift decision & reporting ===")
        needs_retrain, reason = decide_retrain(live_rmse, champion_test_rmse)

        if metrics is not None:
            from jobs.common.job_results import record_eval_metrics, safe_record
            safe_record(
                record_eval_metrics, metrics,
                live_metrics=live_metrics,
                champion_version=champion_version,
                champion_test_rmse=champion_test_rmse,
                needs_retrain=needs_retrain,
            )

        # ── Print the single decisive console summary ──────────────────────────
        _print_summary(
            needs_retrain=needs_retrain,
            reason=reason,
            live_metrics=live_metrics,
            champion_version=champion_version,
            champion_test_rmse=champion_test_rmse,
            n_eval_rows=len(df_eval),
            n_windows=n_windows,
            ts_start=ts_start,
            ts_end=ts_end,
        )

        # ── MLflow logging ─────────────────────────────────────────────────────
        eval_run_id  = "dry-run"
        mlflow_url   = "http://127.0.0.1:5001"

        if not dry_run:
            eval_run_id = log_eval_run(
                live_metrics=live_metrics,
                champion_version=champion_version,
                champion_run_id=champion_run_id,
                champion_test_rmse=champion_test_rmse,
                needs_retrain=needs_retrain,
                reason=reason,
                df_eval=df_eval,
                n_windows=n_windows,
                n_recent_months=N_RECENT_MONTHS,
            )
            mlflow_url = get_mlflow_run_url(eval_run_id)

            send_eval_notification(
                needs_retrain=needs_retrain,
                reason=reason,
                live_metrics=live_metrics,
                champion_version=champion_version,
                champion_test_rmse=champion_test_rmse,
                eval_run_id=eval_run_id,
                ts_start=ts_start,
                ts_end=ts_end,
                n_eval_rows=len(df_eval),
                n_windows=n_windows,
                mlflow_eval_url=mlflow_url,
                dry_run=False,
            )
        else:
            logger.info("DRY RUN — skipping MLflow logging and Discord.")
            send_eval_notification(
                needs_retrain=needs_retrain,
                reason=reason,
                live_metrics=live_metrics,
                champion_version=champion_version,
                champion_test_rmse=champion_test_rmse,
                eval_run_id="dry-run",
                ts_start=ts_start,
                ts_end=ts_end,
                n_eval_rows=len(df_eval),
                n_windows=n_windows,
                mlflow_eval_url=mlflow_url,
                dry_run=True,
            )

        logger.info("=== Evaluation pipeline complete ===")
        return needs_retrain

    except Exception as exc:
        logger.error("❌ Evaluation pipeline failed: %s", exc, exc_info=True)
        if not dry_run:
            try:
                from jobs.evaluate_tools.discord_notify import send_eval_error_notification
                send_eval_error_notification(str(exc))
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


def _print_summary(
    needs_retrain:      bool,
    reason:             str,
    live_metrics:       dict,
    champion_version:   str,
    champion_test_rmse,
    n_eval_rows:        int,
    n_windows:          int,
    ts_start:           str,
    ts_end:             str,
) -> None:
    """Print a concise evaluation summary to stdout."""
    live_rmse = live_metrics.get("live_rmse", float("nan"))
    live_mae  = live_metrics.get("live_mae",  float("nan"))
    live_r2   = live_metrics.get("live_r2",   float("nan"))

    sep = "=" * 60

    if needs_retrain:
        verdict = "⚠️  RETRAIN NEEDED"
    else:
        verdict = "✅  RETRAIN NOT NEEDED"

    lines = [
        "",
        sep,
        f"  PV-LSTM Champion Evaluation",
        sep,
        f"  Champion version : v{champion_version}",
        f"  Eval window      : {ts_start}  →  {ts_end}",
        f"  Rows / Windows   : {n_eval_rows:,} rows  |  {n_windows:,} windows",
        "",
        f"  Live RMSE : {live_rmse:.5f}",
        f"  Live MAE  : {live_mae:.5f}",
        f"  Live R²   : {live_r2:.4f}",
    ]

    if champion_test_rmse is not None:
        drift_ratio = live_rmse / champion_test_rmse if champion_test_rmse > 0 else float("nan")
        lines.append(f"  Champ RMSE: {champion_test_rmse:.5f}  (train-time test)")
        lines.append(f"  Drift ratio: {drift_ratio:.3f}")

    lines += [
        "",
        f"  {verdict}",
        f"  Reason: {reason}",
        sep,
        "",
    ]

    print("\n".join(lines), flush=True)
