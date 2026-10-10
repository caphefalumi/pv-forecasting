"""
evaluation.py — Compute regression metrics for model evaluation.

Metrics computed on target_norm (normalized PV output, 0–1+ scale):
  - RMSE  (root mean squared error)
  - MAE   (mean absolute error)
  - R²    (coefficient of determination)

Optionally unnormalizes predictions and targets by kwp to report kW metrics.
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


def compute_metrics(
    y_true: np.ndarray,
    y_pred: np.ndarray,
    prefix: str = "",
) -> dict[str, float]:
    """Compute RMSE, MAE, and R² for a set of predictions.

    Args:
        y_true: Ground-truth target values.
        y_pred: Model predictions.
        prefix: Optional prefix added to metric keys (e.g. "test_").

    Returns:
        Dict with keys "{prefix}rmse", "{prefix}mae", "{prefix}r2".
    """
    y_true = np.asarray(y_true, dtype=np.float64)
    y_pred = np.asarray(y_pred, dtype=np.float64)

    # Drop rows where either value is NaN
    valid = np.isfinite(y_true) & np.isfinite(y_pred)
    y_true = y_true[valid]
    y_pred = y_pred[valid]

    if len(y_true) == 0:
        logger.warning("No valid predictions for metric computation.")
        return {f"{prefix}rmse": float("nan"),
                f"{prefix}mae":  float("nan"),
                f"{prefix}r2":   float("nan")}

    residuals = y_true - y_pred
    rmse = float(np.sqrt(np.mean(residuals ** 2)))
    mae  = float(np.mean(np.abs(residuals)))

    ss_res = np.sum(residuals ** 2)
    ss_tot = np.sum((y_true - y_true.mean()) ** 2)
    r2     = float(1.0 - ss_res / ss_tot) if ss_tot > 0 else float("nan")

    metrics = {
        f"{prefix}rmse": rmse,
        f"{prefix}mae":  mae,
        f"{prefix}r2":   r2,
    }

    logger.info(
        "%sRMSE=%.5f  MAE=%.5f  R²=%.4f  (n=%d)",
        prefix.upper() if prefix else "",
        rmse, mae, r2, len(y_true),
    )
    return metrics


def evaluate_model(
    model,                    # nn.Module (eval mode)
    X: np.ndarray,            # (N, W, F) scaled test windows
    y_true: np.ndarray,       # (N,) true target_norm values
    split_name: str = "test",
) -> dict[str, float]:
    """Run inference on a pre-built window array and return metrics.

    Args:
        model:      PyTorch nn.Module with best weights, in eval mode.
        X:          Scaled feature windows, shape (N, window_size, n_features).
        y_true:     Ground-truth target_norm values, shape (N,).
        split_name: Name prefix for metric keys (e.g. "val", "test").

    Returns:
        Dict of metrics with "{split_name}_" prefix.
    """
    try:
        import torch
    except ImportError as exc:
        raise ImportError("PyTorch is required: pip install torch") from exc

    model.eval()
    device = next(model.parameters()).device

    predictions: list[np.ndarray] = []
    batch_size = 1024

    with torch.no_grad():
        for start in range(0, len(X), batch_size):
            xb = torch.tensor(X[start: start + batch_size]).to(device)
            pred = model(xb).cpu().numpy()
            predictions.append(pred)

    y_pred = np.concatenate(predictions)
    return compute_metrics(y_true, y_pred, prefix=f"{split_name}_")
