"""
discord_notify.py — Send retrain result notifications to a Discord channel.

Uses a Discord Incoming Webhook (DISCORD_WEBHOOK_URL in .env).
Sends a rich embed with:
  - Data summary (months, rows)
  - Train/Val/Test split dates
  - Challenger vs Champion RMSE comparison
  - Model version and alias assignment status
  - MLflow run URL
  - Next-step instructions for the admin

Color codes:
  Green  — challenger is better (or first model)
  Orange — challenger did not beat champion
  Red    — pipeline error
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# Discord embed color palette (int representation of hex)
_COLOR_GREEN  = 0x57F287   # challenger better
_COLOR_ORANGE = 0xFEE75C   # challenger did not beat champion
_COLOR_RED    = 0xED4245   # error


def _send_embed(payload: dict) -> None:
    """POST an embed payload to the Discord webhook."""
    # Always reload .env with override=True to guarantee latest webhook URL is used
    try:
        from dotenv import load_dotenv
        from pathlib import Path
        repo_root = Path(__file__).resolve().parents[2]
        env_file = repo_root / ".env"
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


def send_retrain_notification(
    run_id:            str,
    model_version:     str,
    metrics:           dict,
    split_info,               # SplitInfo dataclass
    champion_rmse:     Optional[float],
    is_better:         bool,
    is_first_model:    bool,
    mlflow_run_url:    str,
    dry_run:           bool = False,
) -> None:
    """Build and send the retrain result embed to Discord.

    Args:
        run_id:         MLflow run ID.
        model_version:  Registered model version string (e.g. "2").
        metrics:        Dict with keys like "test_rmse", "test_mae", "test_r2".
        split_info:     SplitInfo from splitter.split_by_time().
        champion_rmse:  Current champion test_rmse, or None if first model.
        is_better:      True if challenger beat (or is the first) champion.
        is_first_model: True if no champion exists yet.
        mlflow_run_url: Full URL to the MLflow run UI.
        dry_run:        If True, log the payload but do not actually send.
    """
    challenger_rmse = metrics.get("test_rmse", float("nan"))
    challenger_mae  = metrics.get("test_mae",  float("nan"))
    challenger_r2   = metrics.get("test_r2",   float("nan"))

    # ── Determine status text and embed color ──────────────────────────────
    if is_first_model:
        status_text = (
            "🆕 **First model trained** — no champion to compare yet.\n"
            "Alias `candidate` has been assigned.\n"
            "**Promote manually via GitHub Actions** to set as champion."
        )
        color = _COLOR_GREEN
        title = "🤖 PV-LSTM | First Training Complete"
    elif is_better:
        delta = champion_rmse - challenger_rmse
        pct   = 100.0 * delta / champion_rmse if champion_rmse else 0.0
        status_text = (
            f"✅ **Challenger beats Champion** (RMSE −{delta:.5f}, −{pct:.1f}%)\n"
            f"Alias `candidate` assigned to version **v{model_version}**.\n"
            "Review in MLflow and promote via GitHub Actions."
        )
        color = _COLOR_GREEN
        title = "🚀 PV-LSTM | Challenger Wins"
    else:
        delta = challenger_rmse - champion_rmse
        status_text = (
            f"⚠️ **Challenger did NOT beat Champion** (RMSE +{delta:.5f})\n"
            f"Champion v stays in production. No alias assigned."
        )
        color = _COLOR_ORANGE
        title = "📊 PV-LSTM | Champion Retained"

    # ── Build comparison table ─────────────────────────────────────────────
    if champion_rmse is not None:
        rmse_line = (
            f"`{challenger_rmse:.5f}` (challenger)\n"
            f"`{champion_rmse:.5f}` (champion)"
        )
    else:
        rmse_line = f"`{challenger_rmse:.5f}` (first model)"

    # ── Assemble embed ─────────────────────────────────────────────────────
    fields = [
        {
            "name": "📅 Data Range",
            "value": (
                f"**Total months:** {split_info.n_months_total}\n"
                f"**Train:** {split_info.train_start} → {split_info.train_end} "
                f"({split_info.n_months_train} mo, {split_info.n_rows_train:,} rows)\n"
                f"**Val:** {split_info.val_start} → {split_info.val_end} "
                f"({split_info.n_months_val} mo, {split_info.n_rows_val:,} rows)\n"
                f"**Test:** {split_info.test_start} → {split_info.test_end} "
                f"({split_info.n_months_test} mo, {split_info.n_rows_test:,} rows)"
            ),
            "inline": False,
        },
        {
            "name": "📈 Challenger Metrics (Test)",
            "value": (
                f"RMSE: `{challenger_rmse:.5f}`\n"
                f"MAE:  `{challenger_mae:.5f}`\n"
                f"R²:   `{challenger_r2:.4f}`"
            ),
            "inline": True,
        },
        {
            "name": "🏆 RMSE Comparison",
            "value": rmse_line,
            "inline": True,
        },
        {
            "name": "🔖 Status",
            "value": status_text,
            "inline": False,
        },
        {
            "name": "🔗 MLflow Run",
            "value": mlflow_run_url,
            "inline": False,
        },
    ]

    if is_first_model or is_better:
        fields.append({
            "name": "⚡ Next Step",
            "value": (
                f"Go to **GitHub Actions → Promote Model**\n"
                f"Input: `model_version = {model_version}`"
            ),
            "inline": False,
        })

    payload = {
        "username": "PV Forecasting Bot",
        "avatar_url": "https://cdn-icons-png.flaticon.com/512/1163/1163621.png",
        "embeds": [{
            "title": title,
            "color": color,
            "fields": fields,
            "footer": {
                "text": f"run_id: {run_id}"
                        + ("  |  DRY RUN" if dry_run else "")
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


def send_error_notification(error_msg: str) -> None:
    """Send a pipeline failure alert to Discord."""
    payload = {
        "username": "PV Forecasting Bot",
        "embeds": [{
            "title": "❌ PV-LSTM Retrain FAILED",
            "description": f"```\n{error_msg[:1800]}\n```",
            "color": _COLOR_RED,
        }],
    }
    _send_embed(payload)


def send_promotion_notification(
    model_version: str,
    approver: str,
    promoted_at: str,
) -> None:
    """Send a notification to Discord when a model is promoted to champion."""
    payload = {
        "username": "PV Forecasting Bot",
        "avatar_url": "https://cdn-icons-png.flaticon.com/512/1163/1163621.png",
        "embeds": [{
            "title": "👑 PV-LSTM | New Champion Promoted",
            "color": 0xFEE75C,  # Gold
            "fields": [
                {
                    "name": "🏆 Model Version",
                    "value": f"`v{model_version}`",
                    "inline": True,
                },
                {
                    "name": "👤 Approved By",
                    "value": f"`{approver}`",
                    "inline": True,
                },
                {
                    "name": "⏰ Promotion Time",
                    "value": f"`{promoted_at}`",
                    "inline": False,
                },
                {
                    "name": "📌 Status",
                    "value": "Alias `champion` has been reassigned to this version.",
                    "inline": False,
                },
            ],
        }],
    }
    _send_embed(payload)

