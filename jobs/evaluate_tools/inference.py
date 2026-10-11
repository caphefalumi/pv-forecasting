"""
inference.py — Run champion model inference on the evaluation DataFrame.

Reuses the window-building logic from lstm_model._build_windows() to ensure
the input format exactly matches what the model was trained on.

Steps:
  1. Resolve feature columns from the checkpoint (intersection with df columns).
  2. Build sliding windows from the evaluation DataFrame.
  3. Scale features with the checkpoint's fitted preprocessor.
  4. Run model forward pass in batches.
  5. Return (y_pred, y_true, n_windows).
"""

from __future__ import annotations

import logging
from typing import Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

BATCH_SIZE = 512


def run_inference(
    checkpoint: dict,
    model,              # nn.Module in eval mode
    df_eval: pd.DataFrame,
) -> Tuple[np.ndarray, np.ndarray, int]:
    """Build windows from df_eval and run champion model inference.

    Args:
        checkpoint: Champion checkpoint dict (contains preprocessor, feature_cols,
                    window_size).
        model:      PyTorch nn.Module in eval mode (from model_loader).
        df_eval:    Evaluation DataFrame (filtered, last N rows).

    Returns:
        Tuple of:
          - y_pred  (np.ndarray, shape (n_windows,)): model predictions
          - y_true  (np.ndarray, shape (n_windows,)): ground-truth target_norm
          - n_windows (int): number of valid windows built

    Raises:
        RuntimeError: If no valid windows can be built from df_eval.
    """
    try:
        import torch
    except ImportError as exc:
        raise ImportError("PyTorch is required: pip install torch") from exc

    from jobs.retrain_tools.lstm_model import _build_windows
    from jobs.retrain_tools.data_loader import TARGET_COL

    feature_cols  = checkpoint["feature_cols"]
    preprocessor  = checkpoint["preprocessor"]
    window_size   = checkpoint.get("window_size", 6)

    # Resolve feature columns present in df_eval
    available = [c for c in feature_cols if c in df_eval.columns]
    missing   = [c for c in feature_cols if c not in df_eval.columns]
    if missing:
        logger.warning(
            "Evaluation data is missing %d feature column(s): %s",
            len(missing), missing,
        )
    if not available:
        raise RuntimeError(
            "None of the champion's feature columns are present in the "
            "evaluation DataFrame."
        )

    logger.info(
        "Building evaluation windows (window_size=%d, features=%d) …",
        window_size, len(available),
    )

    X_raw, y_true = _build_windows(
        df_eval,
        feature_cols=available,
        target_col=TARGET_COL,
        window_size=window_size,
    )
    n_windows = len(X_raw)
    logger.info("Built %d evaluation windows.", n_windows)

    # Scale with the champion's fitted preprocessor (flatten → transform → reshape)
    n, W, F = X_raw.shape
    X_flat   = preprocessor.transform(X_raw.reshape(-1, F))
    X_scaled = X_flat.reshape(n, W, F).astype(np.float32)

    # Batch inference
    device      = next(model.parameters()).device
    predictions = []

    with torch.no_grad():
        for start in range(0, n_windows, BATCH_SIZE):
            xb   = torch.tensor(X_scaled[start: start + BATCH_SIZE]).to(device)
            pred = model(xb).cpu().numpy()
            predictions.append(pred)

    y_pred = np.concatenate(predictions)
    logger.info(
        "Inference complete — pred range: [%.4f, %.4f]",
        y_pred.min(), y_pred.max(),
    )

    return y_pred, y_true, n_windows
