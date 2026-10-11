#!/usr/bin/env python3
"""
evaluate.py — Entry point for the PV LSTM champion evaluation pipeline.

Loads the model currently tagged as 'champion' in MLflow Model Registry,
evaluates it on the 500 most-recent records from Supabase, and reports
whether a retrain is recommended.

Console output:
  ✅  RETRAIN NOT NEEDED — champion is performing within acceptable range
  ⚠️  RETRAIN NEEDED    — live RMSE has drifted beyond the threshold

Full results are also logged to the MLflow experiment 'pv-lstm-fleet-eval'
and sent as a Discord notification.

Usage:
    python evaluate.py               # Full evaluation run
    python evaluate.py --dry-run     # Skip MLflow logging and Discord
    python evaluate.py -v            # Verbose debug logging

Environment variables (set in .env or shell):
    SUPABASE_URL                  Supabase project URL
    SUPABASE_SERVICE_ROLE_KEY     Supabase service-role API key
    SUPABASE_RAW_DATA_BUCKET      Bucket name containing processed/ CSVs
    MLFLOW_TRACKING_URI           MLflow tracking server URI
    MLFLOW_UI_URL                 MLflow UI base URL (for Discord links)
    DISCORD_WEBHOOK_URL           Discord incoming webhook URL
    EVAL_DRIFT_THRESHOLD          RMSE degradation ratio threshold (default: 1.15)
    EVAL_FALLBACK_RMSE            Absolute RMSE limit if no baseline (default: 0.15)
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path


def _setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def _parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python evaluate.py",
        description=(
            "PV LSTM Champion Evaluation — test live performance and "
            "decide whether retraining is needed."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Run evaluation locally without MLflow logging or Discord "
            "notification. Useful for debugging."
        ),
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable DEBUG-level logging.",
    )
    return parser.parse_args(argv)


def main(argv=None) -> None:
    args = _parse_args(argv)
    _setup_logging(args.verbose)

    logger = logging.getLogger(__name__)

    if args.dry_run:
        logger.info(
            "🔶 DRY RUN — skipping MLflow logging and Discord notification."
        )

    # Add repo root to sys.path so job packages resolve correctly
    repo_root = Path(__file__).resolve().parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    from jobs.evaluate_pipeline.pipeline import run_evaluate_pipeline

    try:
        needs_retrain = run_evaluate_pipeline(dry_run=args.dry_run)
        # Exit code 2 signals "retrain needed" to CI/CD or shell scripts.
        # Exit code 0 signals "champion is OK".
        sys.exit(2 if needs_retrain else 0)
    except Exception as exc:
        logger.error("❌ Evaluation failed: %s", exc, exc_info=args.verbose)
        sys.exit(1)


if __name__ == "__main__":
    main()
