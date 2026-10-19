#!/usr/bin/env python3
"""
ingest.py — PV Ingest Pipeline Entry Point

Fetches raw data, runs the full preprocess + feature engineering pipeline,
then exports train-ready monthly CSV files to Supabase Storage, tracked by DVC.

Run from the repo root (pv-forecasting/):

    python ingest.py                        # full run, yesterday's data
    python ingest.py --until 2026-09-28     # specify cutoff date
    python ingest.py --skip-fetch           # skip fetch, use existing snapshot
    python ingest.py --dry-run              # local only, no upload/push
    python ingest.py --dry-run -v           # verbose dry-run for debugging

What happens each run:
  1. pvdata run  →  fetches new DKASC + Open-Meteo data, rebuilds train_table.parquet
  2. train_table is split into monthly CSVs  (data/exports/YYYY-MM.csv)
  3. Each CSV is uploaded to Supabase Storage:  processed/YYYY-MM.csv  (upsert)
  4. DVC tracks each CSV version locally and pushes to Supabase S3 remote

Required environment variables (.env):
    SUPABASE_URL
    SUPABASE_SERVICE_ROLE_KEY
    SUPABASE_RAW_DATA_BUCKET

Optional:
    PV_CONFIG      path to pipeline.yaml  (default: config/pipeline.yaml)
    PV_DATA_ROOT   override data_root in config
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
        prog="python ingest.py",
        description="PV Ingest Pipeline — fetch, process, export to Supabase S3",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--skip-fetch",
        action="store_true",
        help="Skip pvdata fetch+build; use the existing latest snapshot on disk.",
    )
    parser.add_argument(
        "--until",
        default=None,
        metavar="YYYY-MM-DD",
        help="Pipeline data cutoff date (default: yesterday Darwin time).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Run locally: save monthly CSVs but do NOT upload to Supabase "
            "and do NOT push DVC cache. Safe for testing."
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
        logger.info("🔶 DRY RUN — no files will be uploaded to Supabase")

    # Add repo root to path so `pvdata` and `jobs` are importable
    repo_root = Path(__file__).resolve().parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    from jobs.common.job_results import INGEST_JOB
    from jobs.common.otel_metrics import job_metrics
    from jobs.ingest_pipeline.pipeline import run_ingest_pipeline

    try:
        # Results go to Grafana over OTLP on exit (no-op without OTLP config or in dry runs).
        with job_metrics(INGEST_JOB, enabled=not args.dry_run) as jm:
            run_ingest_pipeline(
                skip_fetch=args.skip_fetch,
                until=args.until,
                dry_run=args.dry_run,
                metrics=jm,
            )
    except Exception as exc:
        logger.error("❌ Ingest pipeline failed: %s", exc, exc_info=args.verbose)
        sys.exit(1)


if __name__ == "__main__":
    main()
