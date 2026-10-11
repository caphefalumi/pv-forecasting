"""
drift.py — Drift detection and retrain decision logic.

Rule:
  - Compare live_rmse (on recent data) with champion_test_rmse (from training).
  - If live_rmse > champion_test_rmse × DRIFT_THRESHOLD → RETRAIN NEEDED.
  - If champion_test_rmse is unavailable → use an absolute fallback threshold.

DRIFT_THRESHOLD is configurable via the EVAL_DRIFT_THRESHOLD env var (default 1.15,
meaning a 15% RMSE degradation triggers a retrain recommendation).

FALLBACK_RMSE_THRESHOLD is used when no champion baseline exists (default 0.15).
This value is in target_norm units (0–1 scale).
"""

from __future__ import annotations

import logging
import os
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# Relative degradation threshold: live_rmse / champion_test_rmse
DEFAULT_DRIFT_THRESHOLD: float = 1.15

# Absolute RMSE threshold used when no champion baseline is available
DEFAULT_FALLBACK_RMSE: float = 0.15


def decide_retrain(
    live_rmse: float,
    champion_test_rmse: Optional[float],
    drift_threshold: Optional[float] = None,
    fallback_rmse: Optional[float] = None,
) -> Tuple[bool, str]:
    """Decide whether a retrain is needed based on RMSE drift.

    Args:
        live_rmse:          RMSE computed on the most-recent evaluation window.
        champion_test_rmse: RMSE the champion achieved on its training test split.
                            None if unavailable (fallback mode).
        drift_threshold:    Relative threshold (default: EVAL_DRIFT_THRESHOLD env
                            var or 1.15). live_rmse / champion_test_rmse must
                            exceed this value to trigger retrain.
        fallback_rmse:      Absolute RMSE limit when champion baseline is unknown.

    Returns:
        Tuple of:
          - needs_retrain (bool)
          - reason (str): human-readable explanation
    """
    if drift_threshold is None:
        drift_threshold = float(
            os.environ.get("EVAL_DRIFT_THRESHOLD", DEFAULT_DRIFT_THRESHOLD)
        )
    if fallback_rmse is None:
        fallback_rmse = float(
            os.environ.get("EVAL_FALLBACK_RMSE", DEFAULT_FALLBACK_RMSE)
        )

    if champion_test_rmse is not None and champion_test_rmse > 0:
        ratio = live_rmse / champion_test_rmse
        logger.info(
            "Drift check: live_rmse=%.5f  champion_test_rmse=%.5f  ratio=%.3f  "
            "threshold=×%.2f",
            live_rmse, champion_test_rmse, ratio, drift_threshold,
        )
        if ratio > drift_threshold:
            pct = (ratio - 1.0) * 100.0
            reason = (
                f"Live RMSE {live_rmse:.5f} is {pct:.1f}% above champion "
                f"baseline {champion_test_rmse:.5f} "
                f"(ratio {ratio:.3f} > threshold ×{drift_threshold:.2f})."
            )
            logger.warning("⚠️  RETRAIN NEEDED — %s", reason)
            return True, reason
        else:
            pct = (ratio - 1.0) * 100.0
            reason = (
                f"Live RMSE {live_rmse:.5f} is within acceptable range "
                f"(+{pct:.1f}% vs champion {champion_test_rmse:.5f}, "
                f"ratio {ratio:.3f} ≤ threshold ×{drift_threshold:.2f})."
            )
            logger.info("✅  RETRAIN NOT NEEDED — %s", reason)
            return False, reason
    else:
        # No champion baseline — use absolute fallback threshold
        logger.info(
            "No champion baseline available. "
            "Using absolute fallback threshold: %.5f",
            fallback_rmse,
        )
        if live_rmse > fallback_rmse:
            reason = (
                f"Live RMSE {live_rmse:.5f} exceeds fallback threshold "
                f"{fallback_rmse:.5f} (no champion baseline available)."
            )
            logger.warning("⚠️  RETRAIN NEEDED — %s", reason)
            return True, reason
        else:
            reason = (
                f"Live RMSE {live_rmse:.5f} is below fallback threshold "
                f"{fallback_rmse:.5f} (no champion baseline available)."
            )
            logger.info("✅  RETRAIN NOT NEEDED — %s", reason)
            return False, reason
