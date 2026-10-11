"""
model_loader.py — Load the champion model checkpoint from Supabase Storage.

Flow:
  1. Query MLflow Model Registry for the version tagged with alias 'champion'.
  2. Read the run_id from that version.
  3. Download the checkpoint file from Supabase:
       {bucket}/artifacts/{run_id}/checkpoint.pt
  4. Load the checkpoint dict with torch.load() (weights_only=False).
  5. Return (checkpoint, champion_version, run_id, champion_test_rmse).

The checkpoint dict contains:
  - model_state_dict : OrderedDict  — PyTorch weights
  - model_config     : dict         — input_size, hidden_size, num_layers, dropout
  - preprocessor     : sklearn Pipeline (SimpleImputer + StandardScaler)
  - feature_cols     : list[str]
  - window_size      : int
"""

from __future__ import annotations

import io
import logging
import os
import tempfile
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

REGISTERED_MODEL_NAME = "pv-lstm-fleet"
ARTIFACT_PREFIX       = "artifacts"
CHECKPOINT_FILENAME   = "checkpoint.pt"


def load_champion_checkpoint(
    bucket_name: str | None = None,
) -> Tuple[dict, str, str, Optional[float]]:
    """Load the champion model checkpoint from MLflow + Supabase.

    Args:
        bucket_name: Supabase bucket (default: SUPABASE_RAW_DATA_BUCKET env var).

    Returns:
        Tuple of:
          - checkpoint (dict): loaded PyTorch checkpoint dict
          - champion_version (str): MLflow model version string, e.g. "3"
          - run_id (str): MLflow run ID that produced this version
          - champion_test_rmse (float | None): test_rmse logged during training

    Raises:
        RuntimeError: If no champion alias exists or download fails.
    """
    from jobs.retrain_tools.mlflow_helpers import _get_client, REGISTERED_MODEL_NAME

    client, mlflow = _get_client()

    # ── Step 1: Resolve champion alias in Model Registry ─────────────────────
    try:
        mv = client.get_model_version_by_alias(REGISTERED_MODEL_NAME, "champion")
    except Exception as exc:
        raise RuntimeError(
            "No 'champion' alias found in MLflow Model Registry. "
            "Train and promote a model first.\n"
            f"(Original error: {exc})"
        ) from exc

    champion_version = str(mv.version)
    run_id           = mv.run_id
    logger.info(
        "Champion: model='%s'  version=%s  run_id=%s",
        REGISTERED_MODEL_NAME, champion_version, run_id,
    )

    # ── Step 2: Fetch training-time test_rmse from the MLflow run ────────────
    champion_test_rmse: Optional[float] = None
    try:
        run = client.get_run(run_id)
        champion_test_rmse = run.data.metrics.get("test_rmse")
        if champion_test_rmse is not None:
            logger.info(
                "Champion training-time test_rmse: %.5f", champion_test_rmse
            )
        else:
            logger.warning(
                "Champion run has no 'test_rmse' metric — "
                "drift threshold check will be skipped."
            )
    except Exception as exc:
        logger.warning("Could not fetch champion run metrics: %s", exc)

    # ── Step 3: Download checkpoint.pt from Supabase Storage ─────────────────
    bucket_path = f"{ARTIFACT_PREFIX}/{run_id}/{CHECKPOINT_FILENAME}"
    logger.info("Downloading checkpoint from Supabase: %s …", bucket_path)

    try:
        from supabase import create_client
    except ImportError as exc:
        raise ImportError(
            "supabase-py is required: pip install supabase"
        ) from exc

    from urllib.parse import urlparse

    bucket_name  = bucket_name or os.environ["SUPABASE_RAW_DATA_BUCKET"]
    raw_url      = os.environ["SUPABASE_URL"]
    parsed       = urlparse(raw_url)
    base_url     = f"{parsed.scheme}://{parsed.hostname}"
    key          = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

    sb_client = create_client(base_url, key)
    raw_bytes = sb_client.storage.from_(bucket_name).download(bucket_path)

    if not raw_bytes:
        raise RuntimeError(
            f"Downloaded empty file from Supabase path '{bucket_path}'. "
            "The checkpoint may not have been uploaded correctly."
        )

    size_mb = len(raw_bytes) / 1e6
    logger.info("Downloaded checkpoint (%.2f MB). Loading with torch …", size_mb)

    # ── Step 4: Load checkpoint dict from bytes ───────────────────────────────
    try:
        import torch
    except ImportError as exc:
        raise ImportError(
            "PyTorch is required: pip install torch"
        ) from exc

    with tempfile.NamedTemporaryFile(suffix=".pt", delete=False) as tmp:
        tmp.write(raw_bytes)
        tmp_path = tmp.name

    try:
        checkpoint = torch.load(tmp_path, map_location="cpu", weights_only=False)
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    # Validate checkpoint keys
    required = {"model_state_dict", "model_config", "preprocessor", "feature_cols"}
    missing  = required - checkpoint.keys()
    if missing:
        raise RuntimeError(
            f"Champion checkpoint is missing required keys: {sorted(missing)}"
        )

    logger.info(
        "Champion checkpoint loaded. "
        "Features: %d  |  window_size: %d  |  model: %s",
        len(checkpoint["feature_cols"]),
        checkpoint.get("window_size", "?"),
        checkpoint["model_config"],
    )

    return checkpoint, champion_version, run_id, champion_test_rmse


def build_model_from_checkpoint(checkpoint: dict):
    """Reconstruct the nn.Module from a checkpoint dict (eval mode).

    Args:
        checkpoint: Dict returned by ``load_champion_checkpoint()``.

    Returns:
        PyTorch nn.Module in eval mode, on CPU.
    """
    from jobs.retrain_tools.lstm_model import _make_module

    cfg       = checkpoint["model_config"]
    state     = checkpoint["model_state_dict"]
    model     = _make_module(**cfg)
    model.load_state_dict(state)
    model.eval()
    logger.debug("Model reconstructed from checkpoint: %s", cfg)
    return model
