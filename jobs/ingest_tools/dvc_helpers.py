"""
dvc_helpers.py — DVC initialisation, file tracking, and remote push.

DVC remote is Supabase S3-compatible storage:
  - Endpoint:   {SUPABASE_URL}/storage/v1/s3
  - Bucket:     SUPABASE_RAW_DATA_BUCKET  (under a "dvc/" prefix)
  - Access key: project ref (subdomain of SUPABASE_URL)
  - Secret key: SUPABASE_SERVICE_ROLE_KEY  (stored in local config, never committed)

Run once:  ensure_dvc_initialized(repo_root)
Per file:  dvc_add(path)
At end:    dvc_push()
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


# ── Internal helpers ───────────────────────────────────────────────────────────

def _run(cmd: list[str], cwd: Path | None = None) -> str:
    """Run a shell command, raise RuntimeError on failure, return stdout."""
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"Command `{' '.join(cmd)}` failed (exit {result.returncode}):\n"
            f"{result.stderr.strip()}"
        )
    return result.stdout.strip()


def _remote_already_configured(repo_root: Path, remote_name: str = "supabase") -> bool:
    """Check .dvc/config directly — avoids running `dvc remote list` on broken configs."""
    config_path = repo_root / ".dvc" / "config"
    if not config_path.exists():
        return False
    content = config_path.read_text(encoding="utf-8")
    return f'remote "{remote_name}"' in content


# ── Public API ─────────────────────────────────────────────────────────────────

def ensure_dvc_initialized(repo_root: Path, remote_name: str = "supabase") -> None:
    """
    Idempotently initialise DVC and configure the Supabase S3 remote.

    - Runs `dvc init` only if .dvc/ does not exist.
    - Adds the remote only if it is not already listed.
    - Stores secret_access_key in LOCAL config (.dvc/config.local, gitignored).
    """
    dvc_dir = repo_root / ".dvc"
    if not dvc_dir.exists():
        logger.info("Initialising DVC in %s", repo_root)
        _run(["dvc", "init"], cwd=repo_root)
        logger.info("DVC initialised")
    else:
        logger.debug("DVC already initialised (.dvc/ exists)")

    if _remote_already_configured(repo_root, remote_name):
        logger.info("DVC remote '%s' already configured — skipping", remote_name)
        return

    supabase_url = os.environ["SUPABASE_URL"]
    service_role_key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    bucket = os.environ["SUPABASE_RAW_DATA_BUCKET"]

    # Supabase project ref = first part of the hostname
    # e.g. https://abcdefgh.supabase.co/rest/v1/  →  abcdefgh
    parsed = urlparse(supabase_url)
    project_ref = parsed.hostname.split(".")[0]
    # Always build endpoint from scheme+hostname only, ignoring any trailing path
    # (SUPABASE_URL may contain /rest/v1/ which must not be included)
    base_url = f"{parsed.scheme}://{parsed.hostname}"
    endpoint = f"{base_url}/storage/v1/s3"

    logger.info(
        "Configuring DVC remote '%s' → s3://%s/dvc (endpoint: %s)",
        remote_name, bucket, endpoint,
    )

    _run(["dvc", "remote", "add", "-d", remote_name, f"s3://{bucket}/dvc"], cwd=repo_root)
    _run(["dvc", "remote", "modify", remote_name, "endpointurl", endpoint], cwd=repo_root)
    _run(["dvc", "remote", "modify", remote_name, "access_key_id", project_ref], cwd=repo_root)

    # secret goes into .dvc/config.local (gitignored) — never committed
    _run(
        ["dvc", "remote", "modify", "--local", remote_name, "secret_access_key", service_role_key],
        cwd=repo_root,
    )
    logger.info("DVC remote '%s' configured ✅", remote_name)


def dvc_add(file_path: Path) -> None:
    """
    Track a file with DVC (`dvc add <file>`).
    Creates/updates the companion .dvc pointer file.
    """
    _run(["dvc", "add", str(file_path)])
    logger.info("DVC tracked: %s  →  %s.dvc", file_path.name, file_path.name)


def dvc_push(remote_name: str = "supabase") -> None:
    """Push all DVC-tracked files to the configured remote."""
    logger.info("Pushing DVC cache to remote '%s' …", remote_name)
    _run(["dvc", "push", "-r", remote_name])
    logger.info("DVC push complete ✅")
