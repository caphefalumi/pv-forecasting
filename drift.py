#!/usr/bin/env python3
"""
drift.py — Entry point for the PV data drift detection pipeline.

Compares data distribution between a current window (last 7 days) and
a reference window (preceding 30 days) using daytime rows only.

Console output:
  ✅  NO SIGNIFICANT DRIFT  — data distribution is stable
  ⚠️  DATA DRIFT DETECTED   — data distribution shifted significantly

Results are logged to the MLflow experiment 'pv-lstm-fleet-drift'
and posted to Discord via webhook.

Usage:
    python drift.py               # Full drift pipeline run
    python drift.py --dry-run     # Skip MLflow logging and Discord
    python drift.py -v            # Verbose debug logging
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
        prog="python drift.py",
        description=(
            "PV Data Drift Detection — test feature stability between "
            "the last 7 days and preceding 30 days."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Run drift detection locally without MLflow logging or "
            "Discord notification."
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

    # Ensure repository root is on sys.path
    repo_root = Path(__file__).resolve().parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    from jobs.drift_pipeline.pipeline import run_drift_pipeline

    try:
        drift_detected = run_drift_pipeline(dry_run=args.dry_run)
        # Exit code 2 signals drift detected to CI/CD workflows.
        # Exit code 0 signals data is stable.
        sys.exit(2 if drift_detected else 0)
    except Exception as exc:
        logger.error("❌ Drift detection failed: %s", exc, exc_info=args.verbose)
        sys.exit(1)


if __name__ == "__main__":
    main()
