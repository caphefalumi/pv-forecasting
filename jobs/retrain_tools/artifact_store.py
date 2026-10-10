"""
artifact_store.py — Upload model checkpoint to Supabase Storage artifacts/ folder.

Artifacts are stored at:
    {bucket}/artifacts/{run_id}/checkpoint.pt

This keeps model files alongside the processed CSV data in the same bucket,
using a separate folder prefix so they don't conflict with ingested data.
"""

from __future__ import annotations

import io
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

ARTIFACT_PREFIX = "artifacts"


def upload_model_artifact(
    run_id: str,
    checkpoint_path: Path,
    bucket_name: str | None = None,
) -> str:
    """Upload a model checkpoint to Supabase Storage.

    Args:
        run_id:          MLflow run ID (used as sub-folder name).
        checkpoint_path: Local path to the checkpoint .pt file.
        bucket_name:     Bucket name (default: SUPABASE_RAW_DATA_BUCKET env var).

    Returns:
        The bucket path where the file was uploaded, e.g.
        "artifacts/abc123/checkpoint.pt".
    """
    try:
        from supabase import create_client
    except ImportError as exc:
        raise ImportError("supabase-py is required: pip install supabase") from exc

    from urllib.parse import urlparse

    bucket_name = bucket_name or os.environ["SUPABASE_RAW_DATA_BUCKET"]
    raw_url = os.environ["SUPABASE_URL"]
    parsed = urlparse(raw_url)
    base_url = f"{parsed.scheme}://{parsed.hostname}"
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

    client = create_client(base_url, key)
    storage = client.storage.from_(bucket_name)

    checkpoint_path = Path(checkpoint_path)
    file_bytes = checkpoint_path.read_bytes()
    bucket_path = f"{ARTIFACT_PREFIX}/{run_id}/{checkpoint_path.name}"
    size_mb = len(file_bytes) / 1e6

    logger.info(
        "Uploading model artifact to bucket '%s' / %s (%.2f MB) …",
        bucket_name, bucket_path, size_mb,
    )

    storage.upload(
        path=bucket_path,
        file=file_bytes,
        file_options={
            "content-type": "application/octet-stream",
            "upsert": "true",
        },
    )

    logger.info("✅ Model artifact uploaded: %s", bucket_path)
    return bucket_path


def get_artifact_public_path(run_id: str, filename: str = "checkpoint.pt") -> str:
    """Return the Supabase Storage path for a run's model artifact."""
    return f"{ARTIFACT_PREFIX}/{run_id}/{filename}"
