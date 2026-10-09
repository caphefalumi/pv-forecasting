"""
pipeline.py — Orchestration for the PV ingest pipeline.

Steps:
  1. Load .env
  2. Check Supabase for the latest month already uploaded
  3. Run pvdata pipeline  (fetch-dkasc + fetch-meteo + build + validate)
  4. Load latest train_table.parquet
  5. Filter usable rows + reorder columns
  6. Split by data month
  7. Upload only months >= latest Supabase month  (incremental)
     First run: uploads all months (~34 files)
     Subsequent runs: uploads current month only (1–2 files)

No local CSV files are created — data is uploaded directly from memory.

Call:
    from jobs.ingest_pipeline.pipeline import run_ingest_pipeline
    run_ingest_pipeline()
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# Repo root = two levels up from this file  (jobs/ingest_pipeline/pipeline.py)
REPO_ROOT = Path(__file__).resolve().parents[2]


# ── Public entry point ─────────────────────────────────────────────────────────

def run_ingest_pipeline(
    skip_fetch: bool = False,
    until: str | None = None,
    dry_run: bool = False,
) -> None:
    """
    Run the full ingest pipeline end-to-end.

    Args:
        skip_fetch: If True, skip the pvdata fetch+build step (use existing snapshot).
        until:      Pipeline cutoff date "YYYY-MM-DD" (default: yesterday Darwin time).
        dry_run:    If True, skip Supabase upload (local processing only).
    """
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))

    # ── 1. Load environment variables ─────────────────────────────────────────
    _load_env()

    # ── 2. Check Supabase: find latest month already uploaded ─────────────────
    from jobs.ingest_tools.supabase_storage import get_latest_record_ts, upload_csv, download_csv

    if dry_run:
        latest_month, latest_ts = None, None
        logger.info("=== [Step 1/4] DRY RUN — skipping Supabase check ===")
    else:
        logger.info("=== [Step 1/4] Checking latest record on Supabase ===")
        latest_month, latest_ts = get_latest_record_ts(prefix="processed")
        # latest_month: "2026-10" or None (first run)
        # latest_ts:    Timestamp("2026-10-08 23:00:00+00:00") or None

    # ── 3. Run pvdata pipeline ─────────────────────────────────────────────────
    if skip_fetch:
        logger.info("=== [Step 2/4] Skipped pvdata run (--skip-fetch) ===")
    else:
        logger.info("=== [Step 2/4] Running pvdata pipeline ===")
        _run_pvdata(until=until)

    # ── 4. Load + filter + reorder train_table ─────────────────────────────────
    logger.info("=== [Step 3/4] Loading train_table ===")
    from pvdata.dataset import load_table
    from jobs.ingest_tools.monthly_export import (
        filter_usable_rows,
        prepare_export_df,
        split_by_month,
    )

    df, _spec = load_table(version="latest")
    logger.info("train_table: %d rows × %d cols", *df.shape)

    df_usable = filter_usable_rows(df)
    df_export = prepare_export_df(df_usable)

    # ── 5. Split by month ──────────────────────────────────────────────────────
    monthly = split_by_month(df_export)       # dict: {"2024-01": df, ..., "2026-10": df}

    # ── 6. Determine new rows and which months to upload ──────────────────────
    import pandas as pd

    if latest_ts is None:
        # First run: upload all months as-is
        to_upload = {ym: (df_m, False) for ym, df_m in monthly.items()}
        logger.info("First run — uploading all %d months", len(to_upload))
    else:
        # Filter to only rows strictly newer than the latest record on Supabase
        ts_series = pd.to_datetime(df_export["ts_utc_end"], utc=True)
        df_new = df_export[ts_series > latest_ts]

        if df_new.empty:
            logger.info("No new records since %s — nothing to upload", latest_ts)
            return

        monthly_new = split_by_month(df_new)
        logger.info(
            "New records: %d rows across %d month(s) after %s",
            len(df_new), len(monthly_new), latest_ts,
        )

        to_upload = {}
        for ym, df_new_month in monthly_new.items():
            if ym == latest_month:
                # Same month as last Supabase record — append to existing CSV
                to_upload[ym] = (df_new_month, True)   # True = append mode
            else:
                # New month — upload fresh
                to_upload[ym] = (df_new_month, False)

    # ── 7. Upload to Supabase Storage (directly from memory) ──────────────────
    logger.info("=== [Step 4/4] Uploading %d monthly file(s) ===", len(to_upload))
    uploaded: list[str] = []

    for year_month, (df_month, append_mode) in sorted(to_upload.items()):
        bucket_path = f"processed/{year_month}.csv"
        logger.info(
            "--- %s  (%d new rows, mode=%s) ---",
            year_month, len(df_month), "append" if append_mode else "new",
        )

        if dry_run:
            logger.info("[DRY RUN] Would upload → %s", bucket_path)
            continue

        if append_mode:
            # Download existing month CSV, concat new rows, re-upload
            df_existing = download_csv(bucket_path)
            import pandas as pd
            df_combined = pd.concat(
                [df_existing, df_month], ignore_index=True
            ).drop_duplicates(subset=["source_id", "ts_utc_end"])
            upload_csv(df_combined, bucket_path)
        else:
            upload_csv(df_month, bucket_path)

        uploaded.append(bucket_path)

    # ── Summary ────────────────────────────────────────────────────────────────
    if dry_run:
        logger.info("=== [DRY RUN] Pipeline complete — %d file(s) would be uploaded ===", len(to_upload))
    else:
        logger.info("=== Ingest pipeline complete — %d file(s) uploaded ===", len(uploaded))
        for p in uploaded:
            logger.info("  ✅ %s", p)


# ── Internal helpers ───────────────────────────────────────────────────────────

def _load_env() -> None:
    """Load .env from repo root. Falls back gracefully if python-dotenv not installed."""
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        logger.debug(".env not found — relying on ambient env vars")
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(env_path, override=False)
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
