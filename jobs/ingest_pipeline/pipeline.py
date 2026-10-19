"""
pipeline.py — Orchestration for the PV ingest pipeline with DVC versioning.

Steps:
  1. Load .env and ensure DVC is initialised with Supabase S3 remote.
  2. Check Supabase for the latest record timestamp (ts_utc_end).
  3. Run pvdata pipeline (fetch-dkasc + fetch-meteo + build + validate).
  4. Load latest train_table.parquet, filter usable rows, and split by month.
  5. Past months (< current active month):
     - If exports/YYYY-MM.csv.dvc exists: FROZEN, skipped completely.
     - If not: create temp CSV, dvc add, upload to Supabase, delete temp CSV.
  6. Current active month (e.g. 2026-10):
     - If new data is present: create temp CSV, dvc add (updates hash),
       upload to Supabase, delete temp CSV to keep local disk clean.
  7. DVC push new cache to Supabase S3 remote (dvc/ prefix).
  8. Auto git commit the updated .dvc pointer files.

Call:
    from jobs.ingest_pipeline.pipeline import run_ingest_pipeline
    run_ingest_pipeline()
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

# Repo root = two levels up from this file (jobs/ingest_pipeline/pipeline.py)
REPO_ROOT = Path(__file__).resolve().parents[2]
EXPORT_DIR = REPO_ROOT / "exports"


# ── Public entry point ─────────────────────────────────────────────────────────

def run_ingest_pipeline(
    skip_fetch: bool = False,
    until: str | None = None,
    dry_run: bool = False,
    metrics=None,
) -> None:
    """
    Run the full ingest pipeline end-to-end with DVC versioning.

    Args:
        skip_fetch: If True, skip the pvdata fetch+build step (use existing snapshot).
        until:      Pipeline cutoff date "YYYY-MM-DD" (default: yesterday Darwin time).
        dry_run:    If True, skip upload, dvc push, and git commit.
        metrics:    Optional jobs.common.otel_metrics.JobMetrics; receives row counts,
                    data lag, schema violations and QC flag counts for Grafana.
    """
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))

    # ── 1. Load environment variables & initialise DVC ─────────────────────────
    _load_env()

    from jobs.ingest_tools.dvc_helpers import (
        dvc_add,
        dvc_push,
        ensure_dvc_initialized,
        git_commit_dvc_changes,
    )
    from jobs.ingest_tools.monthly_export import (
        filter_usable_rows,
        prepare_export_df,
        save_month_csv,
        split_by_month,
    )
    from jobs.ingest_tools.bootstrap import ensure_raw_data, publish_raw_snapshot
    from jobs.ingest_tools.supabase_storage import get_latest_record_ts, upload_csv
    from pvdata.dataset import load_table

    ensure_dvc_initialized(REPO_ROOT)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR = REPO_ROOT / "data"

    # ── 1. Bootstrap raw data if running on a fresh machine ───────────────────
    ensure_raw_data(DATA_DIR)

    # ── 2. Check Supabase: find latest record timestamp ───────────────────────
    if dry_run:
        latest_month, latest_ts = None, None
        logger.info("=== [Step 1/4] DRY RUN — skipping Supabase remote check ===")
    else:
        logger.info("=== [Step 1/4] Checking latest record on Supabase ===")
        latest_month, latest_ts = get_latest_record_ts(prefix="processed")
        # latest_month: e.g. "2026-10" or None
        # latest_ts:    e.g. Timestamp("2026-10-08 23:00:00+00:00") or None

    # ── 3. Run pvdata pipeline ─────────────────────────────────────────────────
    if skip_fetch:
        logger.info("=== [Step 2/4] Skipped pvdata run (--skip-fetch) ===")
    else:
        logger.info("=== [Step 2/4] Running pvdata pipeline ===")
        _run_pvdata(until=until)


    # ── 4. Load + filter + split train_table ───────────────────────────────────
    logger.info("=== [Step 3/4] Loading and splitting train_table ===")
    df, _spec = load_table(version="latest")
    logger.info("train_table: %d rows × %d cols", *df.shape)

    df_usable = filter_usable_rows(df)

    if metrics is not None:
        from jobs.common.job_results import record_ingest_metrics, safe_record
        from jobs.ingest_tools.quality import summarize_ingest

        def _record_quality() -> None:
            record_ingest_metrics(metrics, summarize_ingest(df, df_usable, latest_ts=latest_ts))

        safe_record(_record_quality)

    df_export = prepare_export_df(df_usable)
    monthly = split_by_month(df_export)  # dict: {"2024-01": df, ..., "2026-10": df}

    if not monthly:
        logger.warning("No data found to export.")
        return

    current_active_month = max(monthly.keys())
    logger.info(
        "Total months in dataset: %d (Active month: %s)",
        len(monthly), current_active_month,
    )

    # ── 5. Process months (Past months vs Current active month) ────────────────
    logger.info("=== [Step 4/4] Processing monthly versions & DVC tracking ===")
    changed_dvc = False
    uploaded_files: list[str] = []

    for ym, df_month in sorted(monthly.items()):
        dvc_pointer = EXPORT_DIR / f"{ym}.csv.dvc"
        bucket_path = f"processed/{ym}.csv"

        if ym < current_active_month:
            # ── Past Month ────────────────────────────────────────────────────
            if dvc_pointer.exists():
                logger.debug("Past month %s is fixed (DVC version exists) — skipped", ym)
                continue

            logger.info("Past month %s: creating initial DVC baseline version", ym)
            temp_csv = save_month_csv(df_month, EXPORT_DIR, ym)
            dvc_add(temp_csv)
            changed_dvc = True

            if not dry_run:
                upload_csv(df_month, bucket_path)
                uploaded_files.append(bucket_path)

            # Clean up local CSV to save disk
            temp_csv.unlink(missing_ok=True)

        else:
            # ── Current Active Month (e.g. 2026-10) ───────────────────────────
            # Check if there is new data compared to Supabase or if DVC is unversioned
            ts_series = pd.to_datetime(df_month["ts_utc_end"], utc=True)
            max_current_ts = ts_series.max()

            has_new_data = (
                latest_ts is None
                or not dvc_pointer.exists()
                or (latest_month != current_active_month)
                or (max_current_ts > latest_ts)
            )

            if not has_new_data:
                logger.info(
                    "Current month %s has no newer records (latest: %s) — DVC version unchanged",
                    ym, latest_ts,
                )
                continue

            logger.info(
                "Current month %s: new records detected (latest: %s) — updating DVC version",
                ym, max_current_ts,
            )
            temp_csv = save_month_csv(df_month, EXPORT_DIR, ym)
            dvc_add(temp_csv)  # DVC recalculates hash and updates 2026-10.csv.dvc
            changed_dvc = True

            if not dry_run:
                upload_csv(df_month, bucket_path)
                uploaded_files.append(bucket_path)

            # Clean up local CSV to save disk
            temp_csv.unlink(missing_ok=True)

    # ── 6. DVC push to remote & git commit ────────────────────────────────────
    if changed_dvc and not dry_run:
        logger.info("Pushing updated DVC cache to Supabase S3 remote …")
        dvc_push(repo_root=REPO_ROOT)

        commit_msg = f"chore(data): update DVC data version ({current_active_month})"
        git_commit_dvc_changes(repo_root=REPO_ROOT, message=commit_msg)

        # Update raw data snapshot on Supabase so other machines get latest chunks
        publish_raw_snapshot(DATA_DIR)

    if metrics is not None:
        from jobs.common.job_results import record_ingest_uploads, safe_record
        safe_record(record_ingest_uploads, metrics, len(uploaded_files))

    # ── Summary ────────────────────────────────────────────────────────────────
    logger.info("=== Ingest pipeline complete ===")
    if uploaded_files:
        for p in uploaded_files:
            logger.info("  ✅ Uploaded & DVC tracked: %s", p)
    else:
        logger.info("  ℹ️ All versions are already up-to-date.")


# ── Internal helpers ───────────────────────────────────────────────────────────

def _load_env() -> None:
    """Load .env from repo root. Falls back gracefully if python-dotenv not installed."""
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        logger.debug(".env not found — relying on ambient env vars")
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(env_path, override=True)
        logger.debug("Loaded .env from %s", env_path)

    except ImportError:
        logger.warning(
            "python-dotenv not installed — set env vars manually: "
            "SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, SUPABASE_RAW_DATA_BUCKET"
        )


def _run_pvdata(until: str | None) -> None:
    """
    Execute `python -m pvdata run --overwrite [--until YYYY-MM-DD]` in-process.
    --overwrite makes it idempotent (safe to re-run on same date).
    """
    from pvdata.__main__ import main as pvdata_main

    argv = ["run", "--overwrite"]   # idempotent: always safe to re-run same date
    if until:
        argv += ["--until", until]

    logger.info("pvdata args: %s", argv)
    try:
        pvdata_main(argv)
    except SystemExit as exc:
        code = exc.code if exc.code is not None else 0
        if code != 0:
            raise RuntimeError(
                f"pvdata run failed with exit code {code}. "
                "Check logs above for details."
            ) from exc
