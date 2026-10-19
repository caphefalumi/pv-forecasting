"""
pipeline.py — Orchestration for the PV LSTM retrain pipeline.

Steps:
  1. Load environment variables.
  2. Download all monthly CSVs from Supabase and merge into one DataFrame.
  3. Compute chronological 70 / 15 / 15 split by calendar month.
  4. Build sliding-window PyTorch datasets and fit feature preprocessor.
  5. Train LSTM model with early stopping on validation loss.
  6. Evaluate trained model on the test split (RMSE, MAE, R²).
  7. Log run to MLflow, register model version.
  8. Compare challenger RMSE with current champion:
       - First model  → assign 'candidate' alias (needs manual promote).
       - Better RMSE  → assign 'candidate' alias (review + promote).
       - Worse RMSE   → no alias assigned; champion stays in production.
  9. Upload checkpoint to Supabase artifacts/.
 10. Send Discord notification with full comparison summary.

Call:
    from jobs.retrain_pipeline.pipeline import run_retrain_pipeline
    run_retrain_pipeline()
"""

from __future__ import annotations

import logging
import sys
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

REPO_ROOT       = Path(__file__).resolve().parents[2]
CHECKPOINT_NAME = "checkpoint.pt"


# ── Public entry point ─────────────────────────────────────────────────────────

def run_retrain_pipeline(
    dry_run: bool = False,
    metrics=None,
) -> None:
    """Run the full retrain pipeline end-to-end.

    Args:
        dry_run: If True, skip MLflow upload, Supabase upload, and Discord.
                 Training and evaluation still run. Useful for debugging.
        metrics: Optional jobs.common.otel_metrics.JobMetrics; receives the test
                 RMSE and candidate decision for Grafana.
    """
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))

    _load_env()

    from jobs.retrain_tools.data_loader   import load_all_monthly_csvs, BASE_FEATURES, TARGET_COL
    from jobs.retrain_tools.splitter      import split_by_time
    from jobs.retrain_tools.lstm_model    import (
        build_datasets, train_model, TrainConfig,
        build_checkpoint, save_checkpoint,
    )
    from jobs.retrain_tools.evaluation    import evaluate_model
    from jobs.retrain_tools.mlflow_helpers import (
        log_retrain_run, compare_and_maybe_assign_candidate,
        get_mlflow_run_url,
    )
    from jobs.retrain_tools.artifact_store  import upload_model_artifact
    from jobs.retrain_tools.discord_notify  import (
        send_retrain_notification, send_error_notification,
    )

    try:
        # ── Step 1: Load data from Supabase ───────────────────────────────
        logger.info("=== [Step 1/7] Downloading monthly CSVs from Supabase ===")
        df = load_all_monthly_csvs()
        logger.info(
            "Loaded dataset: %d rows × %d cols  |  arrays: %d",
            *df.shape, df["source_id"].nunique(),
        )

        # ── Step 2: Time-based split ──────────────────────────────────────
        logger.info("=== [Step 2/7] Computing chronological 70/15/15 split ===")
        df_train, df_val, df_test, split_info = split_by_time(df)

        # Resolve available feature columns (drop missing ones gracefully)
        feature_cols = [c for c in BASE_FEATURES if c in df.columns]
        missing = set(BASE_FEATURES) - set(feature_cols)
        if missing:
            logger.warning("Missing feature columns (will be excluded): %s", sorted(missing))

        # ── Step 3: Build datasets ────────────────────────────────────────
        logger.info("=== [Step 3/7] Building window datasets and fitting preprocessor ===")
        cfg = TrainConfig()   # uses module-level defaults (window=6, hidden=128, …)

        train_data, val_data, test_data, preprocessor = build_datasets(
            df_train, df_val, df_test,
            feature_cols=feature_cols,
            target_col=TARGET_COL,
            window_size=cfg.window_size,
        )
        X_tr, y_tr = train_data
        X_va, y_va = val_data
        X_te, y_te = test_data
        logger.info(
            "Windows: train=%d  val=%d  test=%d",
            len(X_tr), len(X_va), len(X_te),
        )

        # ── Step 4: Train LSTM ────────────────────────────────────────────
        logger.info("=== [Step 4/7] Training LSTM model ===")
        model, history = train_model(
            train_data=(X_tr, y_tr),
            val_data=(X_va, y_va),
            feature_cols=feature_cols,
            cfg=cfg,
        )

        # ── Step 5: Evaluate on test set ──────────────────────────────────
        logger.info("=== [Step 5/7] Evaluating on test split ===")
        val_metrics  = evaluate_model(model, X_va, y_va, split_name="val")
        test_metrics = evaluate_model(model, X_te, y_te, split_name="test")
        all_metrics  = {**val_metrics, **test_metrics}

        challenger_rmse = test_metrics["test_rmse"]
        logger.info("Challenger test RMSE: %.5f", challenger_rmse)

        # ── Step 6: Save checkpoint & log to MLflow ───────────────────────
        logger.info("=== [Step 6/7] Logging run to MLflow ===")

        with tempfile.TemporaryDirectory() as tmpdir:
            checkpoint_path = Path(tmpdir) / CHECKPOINT_NAME
            checkpoint = build_checkpoint(model, preprocessor, feature_cols, cfg)
            save_checkpoint(checkpoint, checkpoint_path)

            run_id = None
            model_version = None
            is_first_model = False
            is_better = False
            champion_rmse = None

            if not dry_run:
                run_id, model_version = log_retrain_run(
                    model=model,
                    checkpoint=checkpoint,
                    metrics=all_metrics,
                    split_info=split_info,
                    cfg=cfg,
                    feature_cols=feature_cols,
                    history=history,
                    checkpoint_path=checkpoint_path,
                )
                logger.info(
                    "MLflow run: run_id=%s  model_version=%s",
                    run_id, model_version,
                )

                # ── Step 7a: Compare with champion ─────────────────────────
                logger.info("=== [Step 7/7] Champion comparison & notification ===")
                champion_rmse_val, _ = _get_champion_rmse_safe()
                is_first_model = champion_rmse_val is None

                is_better, champion_rmse = compare_and_maybe_assign_candidate(
                    challenger_rmse=challenger_rmse,
                    model_version=model_version,
                )

                if metrics is not None:
                    from jobs.common.job_results import record_retrain_metrics, safe_record
                    safe_record(
                        record_retrain_metrics, metrics,
                        test_rmse=challenger_rmse,
                        candidate_assigned=is_better,
                        model_version=model_version,
                        champion_rmse=champion_rmse,
                    )

                # ── Step 7b: Upload checkpoint to Supabase ─────────────────
                if is_better or is_first_model:
                    logger.info("Uploading model checkpoint to Supabase artifacts/ …")
                    upload_model_artifact(run_id, checkpoint_path)
                else:
                    logger.info(
                        "Challenger did not beat champion (RMSE %.5f >= %.5f). "
                        "Skipping checkpoint upload to Supabase.",
                        challenger_rmse, champion_rmse,
                    )

                # ── Step 7c: Discord notification ──────────────────────────
                mlflow_url = get_mlflow_run_url(run_id)
                send_retrain_notification(
                    run_id=run_id,
                    model_version=model_version,
                    metrics=all_metrics,
                    split_info=split_info,
                    champion_rmse=champion_rmse,
                    is_better=is_better,
                    is_first_model=is_first_model,
                    mlflow_run_url=mlflow_url,
                    dry_run=False,
                )
            else:
                # Dry run: log summary locally only
                logger.info("=== [Step 6-7/7] DRY RUN — skipping MLflow/Supabase/Discord ===")
                logger.info("Val metrics:  %s", val_metrics)
                logger.info("Test metrics: %s", test_metrics)

                if metrics is not None:
                    from jobs.common.job_results import record_retrain_metrics, safe_record
                    safe_record(
                        record_retrain_metrics, metrics,
                        test_rmse=challenger_rmse,
                        candidate_assigned=None,  # no champion comparison in dry run
                    )

                # Still show what the Discord message would look like
                from jobs.retrain_tools.discord_notify import send_retrain_notification
                send_retrain_notification(
                    run_id="DRY-RUN",
                    model_version="N/A",
                    metrics=all_metrics,
                    split_info=split_info,
                    champion_rmse=None,
                    is_better=True,
                    is_first_model=True,
                    mlflow_run_url="http://127.0.0.1:5001",
                    dry_run=True,
                )


    except Exception as exc:
        logger.error("❌ Retrain pipeline failed: %s", exc, exc_info=True)
        if not dry_run:
            try:
                send_error_notification(str(exc))
            except Exception:
                pass
        raise

    logger.info("=== Retrain pipeline complete ===")


# ── Internal helpers ───────────────────────────────────────────────────────────

def _load_env() -> None:
    """Load .env from repo root. Mirrors ingest pipeline pattern."""
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


def _get_champion_rmse_safe():
    """Safely get champion RMSE — returns (None, None) on any error."""
    try:
        from jobs.retrain_tools.mlflow_helpers import get_champion_metrics
        return get_champion_metrics()
    except Exception as exc:
        logger.warning("Could not fetch champion metrics: %s", exc)
        return None, None
