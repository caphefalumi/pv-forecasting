"""
discord_notify.py — Send data drift detection results to a Discord channel.

Sends a rich embed with:
  - Current vs reference window date ranges and row counts
  - KS test summary: drift_score and top-5 most-drifted features
  - PSI of target_norm
  - Overall drift decision: ⚠️ DATA DRIFT DETECTED or ✅ NO SIGNIFICANT DRIFT
  - Link to the MLflow drift run
  - Next-step instructions if drift is detected

Color codes:
  Orange — drift detected (retrain may be needed)
  Green  — no significant drift
  Red    — pipeline error
"""

from __future__ import annotations

import logging
import os
from datetime import date
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_COLOR_GREEN  = 0x57F287   # no drift
_COLOR_ORANGE = 0xFEE75C   # drift detected
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


def send_drift_notification(
    report,                  # DriftReport from detector
    cur_start:   date,
    cur_end:     date,
    ref_start:   date,
    ref_end:     date,
    drift_run_id:    str,
    mlflow_drift_url:str,
    dry_run:     bool = False,
) -> None:
    """Build and send the drift detection result embed to Discord.

    Args:
        report:           DriftReport from detect_drift().
        cur_start/end:    Current window date range.
        ref_start/end:    Reference window date range.
        drift_run_id:     MLflow run ID of the drift run.
        mlflow_drift_url: URL to the MLflow drift run UI.
        dry_run:          If True, log payload but do not send.
    """
    if report.drift_detected:
        title = "⚠️  PV-LSTM | Data Drift Detected"
        color = _COLOR_ORANGE
        status = (
            "⚠️ **Data distribution has shifted significantly.**\n"
            f"{report.reason}\n\n"
            "**Recommended action:** run `python evaluate.py` to check "
            "model performance, then trigger retrain if needed."
        )
    else:
        title = "✅  PV-LSTM | Data Drift — No Action Needed"
        color = _COLOR_GREEN
        status = (
            "✅ **No significant data drift detected.**\n"
            f"{report.reason}"
        )

    # Top-5 drifted features table (for Discord embed)
    drifted = sorted(
        (r for r in report.feature_results if r.drifted),
        key=lambda r: -r.ks_stat,
    )[:5]
    if drifted:
        top_lines = "\n".join(
            f"`{r.feature}`: KS={r.ks_stat:.3f} | Δmean={r.mean_delta:+.4f}"
            for r in drifted
        )
        top_field_value = top_lines
    else:
        top_field_value = "*No features flagged.*"

    # PSI field
    psi_value = (
        f"`{report.target_psi:.4f}`  (threshold: {report.psi_threshold:.2f})"
        if report.target_psi is not None
        else "*Not computed*"
    )
    psi_status = ""
    if report.target_psi is not None:
        if report.target_psi > report.psi_threshold:
            psi_status = "  ⚠️ ABOVE threshold"
        elif report.target_psi > 0.10:
            psi_status = "  ⚡ moderate"
        else:
            psi_status = "  ✅ stable"

    fields = [
        {
            "name": "📅 Windows",
            "value": (
                f"**Current:**   `{cur_start}` → `{cur_end}`  "
                f"({report.n_rows_current:,} rows)\n"
                f"**Reference:** `{ref_start}` → `{ref_end}`  "
                f"({report.n_rows_reference:,} rows)"
            ),
            "inline": False,
        },
        {
            "name": "📊 KS Drift Score",
            "value": (
                f"`{report.drift_score:.2f}` "
                f"({report.n_features_drifted}/{report.n_features_tested} features)  "
                f"threshold: `{report.ks_threshold:.2f}`"
                + ("  ⚠️ TRIGGERED" if report.ks_triggered else "  ✅ OK")
            ),
            "inline": False,
        },
        {
            "name": "🔝 Top Drifted Features (KS)",
            "value": top_field_value,
            "inline": True,
        },
        {
            "name": "🎯 Target PSI (target_norm)",
            "value": psi_value + psi_status,
            "inline": True,
        },
        {
            "name": "🔖 Decision",
            "value": status,
            "inline": False,
        },
        {
            "name": "🔗 MLflow Drift Run",
            "value": mlflow_drift_url,
            "inline": False,
        },
    ]

    if report.drift_detected:
        fields.append({
            "name": "⚡ Next Steps",
            "value": (
                "1. Run `python evaluate.py` to check champion performance.\n"
                "2. If evaluate also flags issues, run `python retrain.py`."
            ),
            "inline": False,
        })

    payload = {
        "username":   "PV Forecasting Bot",
        "avatar_url": "https://cdn-icons-png.flaticon.com/512/1163/1163621.png",
        "embeds": [{
            "title":  title,
            "color":  color,
            "fields": fields,
            "footer": {
                "text": (
                    f"drift run_id: {drift_run_id}"
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


def send_drift_error_notification(error_msg: str) -> None:
    """Send a pipeline failure alert to Discord."""
    payload = {
        "username": "PV Forecasting Bot",
        "embeds": [{
            "title":       "❌ PV-LSTM Drift Detection FAILED",
            "description": f"```\n{error_msg[:1800]}\n```",
            "color":       _COLOR_RED,
        }],
    }
    _send_embed(payload)
