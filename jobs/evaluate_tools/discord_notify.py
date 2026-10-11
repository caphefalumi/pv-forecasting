"""
discord_notify.py — Send champion evaluation results to a Discord channel.

Reuses the webhook infrastructure from retrain_tools.discord_notify.
Sends a rich embed with:
  - Evaluation window date range and row count
  - Live RMSE / MAE / R² vs champion training-time test_rmse
  - Drift ratio
  - Retrain decision: ⚠️ RETRAIN NEEDED or ✅ RETRAIN NOT NEEDED
  - Link to the MLflow evaluation run

Color codes:
  Green  — live performance acceptable (retrain not needed)
  Orange — performance has drifted (retrain needed)
  Red    — pipeline error
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_COLOR_GREEN  = 0x57F287   # retrain not needed
_COLOR_ORANGE = 0xFEE75C   # retrain needed
_COLOR_RED    = 0xED4245   # error


def _send_embed(payload: dict) -> None:
    """POST an embed payload to the Discord webhook."""
    try:
        from dotenv import load_dotenv
        from pathlib import Path
        repo_root = Path(__file__).resolve().parents[2]
        env_file  = repo_root / ".env"
        if env_file.exists():
            load_dotenv(env_file, override=True)
    except Exception:
        pass

    url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not url:
        logger.warning(
            "DISCORD_WEBHOOK_URL not set — skipping Discord notification."
        )
        return

    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        logger.info("Discord notification sent (status %d).", resp.status_code)
    except requests.RequestException as exc:
        logger.warning("Discord notification failed: %s", exc)


def send_eval_notification(
    needs_retrain:         bool,
    reason:                str,
    live_metrics:          dict,
    champion_version:      str,
    champion_test_rmse:    Optional[float],
    eval_run_id:           str,
    ts_start:              str,
    ts_end:                str,
    n_eval_rows:           int,
    n_windows:             int,
    mlflow_eval_url:       str,
    dry_run:               bool = False,
) -> None:
    """Build and send the evaluation result embed to Discord.

    Args:
        needs_retrain:      True if drift threshold was exceeded.
        reason:             Human-readable drift decision explanation.
        live_metrics:       Dict with "live_rmse", "live_mae", "live_r2".
        champion_version:   MLflow model version string of the champion.
        champion_test_rmse: Champion training-time test RMSE, or None.
        eval_run_id:        MLflow run ID of the evaluation run.
        ts_start:           Earliest timestamp in the evaluation window.
        ts_end:             Latest timestamp in the evaluation window.
        n_eval_rows:        Number of filtered rows evaluated.
        n_windows:          Number of sliding windows evaluated.
        mlflow_eval_url:    URL to the MLflow evaluation run.
        dry_run:            If True, log payload but do not send.
    """
    live_rmse = live_metrics.get("live_rmse", float("nan"))
    live_mae  = live_metrics.get("live_mae",  float("nan"))
    live_r2   = live_metrics.get("live_r2",   float("nan"))

    if needs_retrain:
        title  = "⚠️  PV-LSTM | Evaluation — RETRAIN NEEDED"
        color  = _COLOR_ORANGE
        status = (
            f"⚠️ **Performance drift detected.**\n"
            f"{reason}\n\n"
            f"**Recommended action:** trigger `python retrain.py` or the "
            f"retrain GitHub Actions workflow."
        )
    else:
        title  = "✅  PV-LSTM | Evaluation — Champion OK"
        color  = _COLOR_GREEN
        status = (
            f"✅ **Champion model is performing within acceptable range.**\n"
            f"{reason}"
        )

    # RMSE comparison field
    if champion_test_rmse is not None:
        drift_ratio  = live_rmse / champion_test_rmse if champion_test_rmse else float("nan")
        drift_pct    = (drift_ratio - 1.0) * 100.0
        rmse_compare = (
            f"Live:     `{live_rmse:.5f}`\n"
            f"Champion: `{champion_test_rmse:.5f}` (train-time test)\n"
            f"Drift:    `{drift_pct:+.1f}%` (ratio `{drift_ratio:.3f}`)"
        )
    else:
        rmse_compare = (
            f"Live: `{live_rmse:.5f}`\n"
            f"Champion baseline: *not available*"
        )

    fields = [
        {
            "name": "📅 Evaluation Window",
            "value": (
                f"**Rows:**    {n_eval_rows:,} filtered  ({n_windows:,} windows)\n"
                f"**From:**  `{ts_start}`\n"
                f"**To:**    `{ts_end}`"
            ),
            "inline": False,
        },
        {
            "name": "📈 Live Metrics",
            "value": (
                f"RMSE: `{live_rmse:.5f}`\n"
                f"MAE:  `{live_mae:.5f}`\n"
                f"R²:   `{live_r2:.4f}`"
            ),
            "inline": True,
        },
        {
            "name": "🏆 RMSE Comparison",
            "value": rmse_compare,
            "inline": True,
        },
        {
            "name": "🔖 Decision",
            "value": status,
            "inline": False,
        },
        {
            "name": "🔗 MLflow Evaluation Run",
            "value": mlflow_eval_url,
            "inline": False,
        },
    ]

    payload = {
        "username":   "PV Forecasting Bot",
        "avatar_url": "https://cdn-icons-png.flaticon.com/512/1163/1163621.png",
        "embeds": [{
            "title":  title,
            "color":  color,
            "fields": fields,
            "footer": {
                "text": (
                    f"Champion: v{champion_version}  |  "
                    f"eval run_id: {eval_run_id}"
                    + ("  |  DRY RUN" if dry_run else "")
                )
            },
        }],
    }

    if dry_run:
        import json
        logger.info(
            "DRY RUN — Discord payload (not sent):\n%s",
            json.dumps(payload, indent=2),
        )
        return

    _send_embed(payload)


def send_eval_error_notification(error_msg: str) -> None:
    """Send a pipeline failure alert to Discord."""
    payload = {
        "username": "PV Forecasting Bot",
        "embeds": [{
            "title":       "❌ PV-LSTM Evaluation FAILED",
            "description": f"```\n{error_msg[:1800]}\n```",
            "color":       _COLOR_RED,
        }],
    }
    _send_embed(payload)
