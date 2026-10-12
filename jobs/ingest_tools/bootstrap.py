"""
bootstrap.py — Auto-bootstrap raw data on fresh machines from Supabase Storage.

Enables running `python ingest.py` on any freshly cloned repository without
manually copying the `data/` folder.

Functions:
  - ensure_raw_data(): Checks if raw data is missing; downloads and extracts
                       snapshot from Supabase if needed.
  - publish_raw_snapshot(): Packages recent raw files and uploads snapshot
                            to Supabase Storage.
"""

from __future__ import annotations

import io
import logging
import os
import tarfile
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

SNAPSHOT_BUCKET_PATH = "raw_snapshots/data_snapshot.tar.gz"


def _get_storage():
    """Return Supabase storage client for default bucket."""
    from jobs.ingest_tools.supabase_storage import _get_client, _default_bucket
    client = _get_client()
    return client.storage.from_(_default_bucket())


def is_raw_data_missing(data_dir: Path) -> bool:
    """Check if the local data directory lacks the minimum raw files needed by pvdata."""
    api_dir = data_dir / "dkasc_api_5min"
    if not api_dir.exists():
        return True

    # Count chunk CSV files
    chunks = list(api_dir.glob("*.csv"))
    # If fewer than 10 chunk files, it's missing data
    if len(chunks) < 10:
        return True

    # Check for metadata
    if not (data_dir / "arrays_metadata.csv").exists():
        return True

    return False


def ensure_raw_data(data_dir: Path) -> bool:
    """Check if raw data is present locally. If missing, download snapshot from Supabase.

    Returns:
        True if snapshot was downloaded and extracted, False if already present.
    """
    if not is_raw_data_missing(data_dir):
        logger.debug("Local raw data is present. Skipping bootstrap.")
        return False

    logger.info("⚡ Fresh environment detected (missing raw data in %s).", data_dir)
    logger.info("Downloading raw data snapshot from Supabase Storage (%s) …", SNAPSHOT_BUCKET_PATH)

    try:
        storage = _get_storage()
        raw_bytes = storage.download(SNAPSHOT_BUCKET_PATH)
        logger.info("Downloaded snapshot (%.1f MB). Extracting into %s …", len(raw_bytes) / 1e6, data_dir)

        data_dir.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(raw_bytes), mode="r:gz") as tar:
            # Extract into project root (archive paths start with 'data/')
            project_root = data_dir.parent
            tar.extractall(path=project_root)

        logger.info("✅ Auto-bootstrap complete! Raw data restored successfully.")
        return True

    except Exception as exc:
        logger.warning(
            "⚠️ Could not auto-download raw data snapshot from Supabase: %s. "
            "Pipeline will attempt to fetch from source APIs directly.",
            exc,
        )
        return False


def publish_raw_snapshot(data_dir: Path) -> None:
    """Package essential raw files and upload snapshot to Supabase Storage."""
    logger.info("Packaging raw data snapshot for remote sync …")

    # Files and folders to include in snapshot
    targets = [
        data_dir / "Alice_Springs_2026.csv",
        data_dir / "arrays_metadata.csv",
        data_dir / "array_overrides.csv",
        data_dir / "dkasc_api_5min",
        data_dir / "open_meteo_raw",
    ]

    project_root = data_dir.parent

    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
        tmp_path = Path(tmp.name)

    try:
        with tarfile.open(tmp_path, mode="w:gz") as tar:
            for target in targets:
                if target.exists():
                    arcname = target.relative_to(project_root)
                    tar.add(target, arcname=str(arcname))
                    logger.debug("Added to snapshot: %s", arcname)

        size_mb = tmp_path.stat().st_size / 1e6
        logger.info("Uploading snapshot to Supabase (%s, %.1f MB) …", SNAPSHOT_BUCKET_PATH, size_mb)

        file_bytes = tmp_path.read_bytes()
        storage = _get_storage()
        storage.upload(
            path=SNAPSHOT_BUCKET_PATH,
            file=file_bytes,
            file_options={
                "content-type": "application/gzip",
                "upsert": "true",
            },
        )
        logger.info("✅ Raw data snapshot updated on Supabase: %s", SNAPSHOT_BUCKET_PATH)

    except Exception as exc:
        logger.warning("Could not upload raw data snapshot: %s", exc)

    finally:
        tmp_path.unlink(missing_ok=True)
