#!/usr/bin/env python3
"""
retrain.py — Entry point for the PV LSTM retrain pipeline.

Downloads processed monthly datasets from Supabase Storage,
splits data into train/val/test, trains a fleet LSTM model,
evaluates on test split, logs results to MLflow,
uploads model artifacts to Supabase, and notifies Discord.

Usage:
    python retrain.py               # Full retrain pipeline run
    python retrain.py --dry-run     # Dry run: train locally, skip uploads and Discord
    python retrain.py -v            # Verbose debug logging
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
        prog="python retrain.py",
        description="PV LSTM Retrain Pipeline — train fleet model and log to MLflow",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run training locally without MLflow/Supabase/Discord publishing.",
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
        logger.info("🔶 DRY RUN — skipping MLflow logging, Supabase upload, and Discord.")

    # Add repo root to path
    repo_root = Path(__file__).resolve().parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    from jobs.retrain_pipeline.pipeline import run_retrain_pipeline

    try:
        run_retrain_pipeline(dry_run=args.dry_run)
    except Exception as exc:
        logger.error("❌ Retrain pipeline failed: %s", exc, exc_info=args.verbose)
        sys.exit(1)


if __name__ == "__main__":
    main()
