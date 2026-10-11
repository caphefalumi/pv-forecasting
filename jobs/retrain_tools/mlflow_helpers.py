"""
mlflow_helpers.py — MLflow tracking, model registration, and alias management.

Registered model: "pv-lstm-fleet"
Experiment:       "pv-lstm-fleet"
Aliases used:
  champion   — the production model (set manually via promote workflow)
  candidate  — challenger that beat the current champion (pending approval)

First-model handling:
  If no "champion" alias exists, the new model is registered as "candidate"
  and requires manual promotion via `python promote.py --version <N>`.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

REGISTERED_MODEL_NAME = "pv-lstm-fleet"
EXPERIMENT_NAME       = "pv-lstm-fleet"


def _get_tracking_uri() -> str:
    """Resolve the tracking URI: MLFLOW_TRACKING_URI -> SQL_ALCHEMY_DATABASE_URL -> localhost."""
    uri = os.environ.get("MLFLOW_TRACKING_URI") or os.environ.get("SQL_ALCHEMY_DATABASE_URL")
    if uri:
        # Supabase pooler: port 5432 is Session mode (limited to 15 clients).
        # Port 6543 is Transaction mode (supports concurrent serverless/CI clients).
        if "pooler.supabase.com:5432" in uri:
            uri = uri.replace("pooler.supabase.com:5432", "pooler.supabase.com:6543")
        # Transaction mode (port 6543) does not support psycopg3 prepared statements.
        # Force psycopg2 driver which does not prepare statements on the server.
        if uri.startswith("postgresql://"):
            uri = "postgresql+psycopg2://" + uri[len("postgresql://"):]
        elif uri.startswith("postgres://"):
            uri = "postgresql+psycopg2://" + uri[len("postgres://"):]
        return uri
    return "http://127.0.0.1:5001"



def _get_client():
    """Return an MLflow MlflowClient connected to the configured tracking URI."""
    try:
        import mlflow
        from mlflow.tracking import MlflowClient
    except ImportError as exc:
        raise ImportError("mlflow is required: pip install mlflow") from exc

    tracking_uri = _get_tracking_uri()
    mlflow.set_tracking_uri(tracking_uri)
    return MlflowClient(), mlflow




def get_champion_metrics() -> Tuple[Optional[float], Optional[str]]:
    """Return (test_rmse, run_id) of the current champion model.

    Returns (None, None) if no champion alias exists yet.
    """
    try:
        client, mlflow = _get_client()
        mv = client.get_model_version_by_alias(REGISTERED_MODEL_NAME, "champion")
        run = client.get_run(mv.run_id)
        rmse = run.data.metrics.get("test_rmse")
        logger.info(
            "Champion: model version=%s  run_id=%s  test_rmse=%.5f",
            mv.version, mv.run_id, rmse or float("nan"),
        )
        return rmse, mv.run_id
    except Exception as exc:
        # MlflowException is raised when alias does not exist
        logger.info("No champion alias found (%s). This is the first model.", exc)
        return None, None


def log_retrain_run(
    model,                   # nn.Module (eval mode, best weights)
    checkpoint: dict,        # from lstm_model.build_checkpoint()
    metrics: dict,           # from evaluation.compute_metrics()
    split_info,              # SplitInfo dataclass
    cfg,                     # TrainConfig dataclass
    feature_cols: list,
    history,                 # TrainHistory dataclass
    checkpoint_path: Path,   # local path to the .pt file
) -> Tuple[str, str]:
    """Log one retrain run to MLflow and register the model.

    Returns:
        (run_id, model_version_str)
    """
    import dataclasses
    try:
        import mlflow
    except ImportError as exc:
        raise ImportError("mlflow is required: pip install mlflow") from exc

    tracking_uri = _get_tracking_uri()
    mlflow.set_tracking_uri(tracking_uri)


    mlflow.set_experiment(EXPERIMENT_NAME)

    # Ensure the registered model exists (idempotent)
    client, _ = _get_client()
    try:
        client.create_registered_model(REGISTERED_MODEL_NAME)
        logger.info("Created registered model: %s", REGISTERED_MODEL_NAME)
    except Exception:
        pass  # already exists

    with mlflow.start_run() as run:
        run_id = run.info.run_id

        # ── Tags ───────────────────────────────────────────────────────────
        mlflow.set_tags({
            "data.train_start":  split_info.train_start,
            "data.train_end":    split_info.train_end,
            "data.val_start":    split_info.val_start,
            "data.val_end":      split_info.val_end,
            "data.test_start":   split_info.test_start,
            "data.test_end":     split_info.test_end,
            "model.type":        "LSTM",
            "model.name":        REGISTERED_MODEL_NAME,
        })

        # ── Parameters ─────────────────────────────────────────────────────
        params = {
            "n_features":    len(feature_cols),
            "n_months_train": split_info.n_months_train,
            "n_months_val":   split_info.n_months_val,
            "n_months_test":  split_info.n_months_test,
            "n_rows_train":   split_info.n_rows_train,
            "n_rows_val":     split_info.n_rows_val,
            "n_rows_test":    split_info.n_rows_test,
        }
        params.update(cfg.as_dict())
        mlflow.log_params(params)

        # ── Metrics ────────────────────────────────────────────────────────
        mlflow.log_metrics(metrics)
        mlflow.log_metric("best_val_loss", history.best_val_loss)
        mlflow.log_metric("best_epoch",    history.best_epoch)

        # Per-epoch loss curves
        for i, (tl, vl) in enumerate(
            zip(history.train_losses, history.val_losses), start=1
        ):
            mlflow.log_metric("epoch_train_loss", tl, step=i)
            mlflow.log_metric("epoch_val_loss",   vl, step=i)

        # ── Register model version ─────────────────────────────────────────
        # Artifacts are stored directly in Supabase Storage (see artifact_store.py).
        # Register the model version directly pointing to Supabase to prevent
        # generating local mlruns/ directories.
        mv = client.create_model_version(
            name=REGISTERED_MODEL_NAME,
            source=f"supabase://artifacts/{run_id}/checkpoint.pt",
            run_id=run_id,
            tags={
                "artifact_store": "supabase",
                "checkpoint_file": "checkpoint.pt",
            },
        )
        model_version = str(mv.version)

        logger.info(
            "MLflow run logged: run_id=%s  model_version=%s",
            run_id, model_version,
        )

    return run_id, model_version



def assign_alias(model_version: str, alias: str) -> None:
    """Set an alias on a registered model version.

    Args:
        model_version: Version string, e.g. "2".
        alias:         Alias to assign, e.g. "candidate" or "champion".
    """
    client, _ = _get_client()
    client.set_registered_model_alias(
        name=REGISTERED_MODEL_NAME,
        alias=alias,
        version=model_version,
    )
    logger.info(
        "Alias '%s' assigned to %s version %s",
        alias, REGISTERED_MODEL_NAME, model_version,
    )


def compare_and_maybe_assign_candidate(
    challenger_rmse: float,
    model_version: str,
) -> Tuple[bool, Optional[float]]:
    """Compare challenger RMSE with champion RMSE.

    - If no champion exists: assign 'candidate' alias (first-model case).
    - If champion exists and challenger RMSE < champion RMSE: assign 'candidate'.
    - If champion exists and challenger RMSE >= champion RMSE: no alias assigned.

    Returns:
        (is_better: bool, champion_rmse: Optional[float])
    """
    champion_rmse, _ = get_champion_metrics()

    if champion_rmse is None:
        # First model ever — treat as "better by default" to surface it
        logger.info(
            "First model (no champion yet). "
            "Assigning 'candidate' alias to version %s.", model_version,
        )
        assign_alias(model_version, "candidate")
        return True, None

    if challenger_rmse < champion_rmse:
        logger.info(
            "Challenger RMSE %.5f < Champion RMSE %.5f — assigning 'candidate'.",
            challenger_rmse, champion_rmse,
        )
        assign_alias(model_version, "candidate")
        return True, champion_rmse
    else:
        logger.info(
            "Challenger RMSE %.5f >= Champion RMSE %.5f — champion retained.",
            challenger_rmse, champion_rmse,
        )
        return False, champion_rmse


def get_candidate_version() -> Optional[str]:
    """Return the model version string currently tagged with the 'candidate' alias.

    Returns None if no candidate alias exists.
    """
    try:
        client, _ = _get_client()
        cv = client.get_model_version_by_alias(REGISTERED_MODEL_NAME, "candidate")
        return cv.version
    except Exception as exc:
        logger.debug("No candidate alias found: %s", exc)
        return None


def promote_to_champion(
    model_version: str,
    approver: str = "unknown",
) -> str:
    """Promote a model version to the 'champion' alias.

    Also removes the 'candidate' alias from any previous candidate and
    adds provenance tags to the model version.

    Args:
        model_version: Version string to promote, e.g. "2".
        approver:      GitHub username or name of approving admin.

    Returns:
        promoted_at: ISO timestamp of promotion.
    """
    from datetime import datetime, timezone

    client, _ = _get_client()

    # Remove 'candidate' alias from any existing candidate
    try:
        cv = client.get_model_version_by_alias(REGISTERED_MODEL_NAME, "candidate")
        if cv.version != model_version:
            client.delete_registered_model_alias(REGISTERED_MODEL_NAME, "candidate")
            logger.info("Removed 'candidate' alias from version %s", cv.version)
    except Exception:
        pass

    # Set 'champion' alias on the chosen version
    client.set_registered_model_alias(
        name=REGISTERED_MODEL_NAME,
        alias="champion",
        version=model_version,
    )

    # Tag provenance on the model version
    promoted_at = datetime.now(timezone.utc).isoformat()
    client.set_model_version_tag(
        name=REGISTERED_MODEL_NAME,
        version=model_version,
        key="promoted_by",
        value=approver,
    )
    client.set_model_version_tag(
        name=REGISTERED_MODEL_NAME,
        version=model_version,
        key="promoted_at",
        value=promoted_at,
    )

    logger.info(
        "✅ Version %s promoted to 'champion' by '%s' at %s",
        model_version, approver, promoted_at,
    )
    return promoted_at


def get_mlflow_run_url(run_id: str) -> str:
    """Build the MLflow UI URL for a run."""
    ui_url = os.environ.get("MLFLOW_UI_URL", "http://127.0.0.1:5001")
    return f"{ui_url.rstrip('/')}/#/runs/{run_id}"


