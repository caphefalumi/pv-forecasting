"""
dvc_helpers.py — DVC initialisation, file tracking, remote push, and git commit.

DVC remote is Supabase S3-compatible storage:
  - Endpoint:   {SUPABASE_URL}/storage/v1/s3
  - Bucket:     SUPABASE_RAW_DATA_BUCKET  (under a "dvc/" prefix)
  - Access key: project ref (subdomain of SUPABASE_URL)
  - Secret key: SUPABASE_SERVICE_ROLE_KEY  (stored in local config, never committed)
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


def clean_stale_locks(repo_root: Path) -> None:
    """Remove any stale lock files in .dvc/tmp left over from interrupted runs."""
    tmp_dir = repo_root / ".dvc" / "tmp"
    if tmp_dir.exists():
        for lock_file in tmp_dir.glob("*lock*"):
            try:
                lock_file.unlink()
                logger.debug("Removed stale DVC lock: %s", lock_file.name)
            except OSError:
                pass


def _remote_already_configured(repo_root: Path, remote_name: str = "supabase") -> bool:
    """Check .dvc/config directly — avoids running `dvc remote list` on broken configs."""
    config_path = repo_root / ".dvc" / "config"
    if not config_path.exists():
        return False
    content = config_path.read_text(encoding="utf-8")
    return f'remote "{remote_name}"' in content


# ── Public API ─────────────────────────────────────────────────────────────────

def _in_git_repo(repo_root: Path) -> bool:
    """True if repo_root is inside a git work tree (False in container images)."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=repo_root, capture_output=True, text=True,
        )
    except FileNotFoundError:  # git not installed in the image
        return False
    return result.returncode == 0 and result.stdout.strip() == "true"


def ensure_dvc_initialized(repo_root: Path, remote_name: str = "supabase") -> None:
    """
    Idempotently initialise DVC and configure the Supabase S3 remote.

    - Runs `dvc init` only if .dvc/ does not exist.
    - Adds the remote only if it is not already listed.
    - Stores secret_access_key in LOCAL config (.dvc/config.local, gitignored).
    """
    clean_stale_locks(repo_root)
    dvc_dir = repo_root / ".dvc"
    if not dvc_dir.exists():
        logger.info("Initialising DVC in %s", repo_root)
        cmd = ["dvc", "init"]
        if not _in_git_repo(repo_root):
            # Container images ship without .git, so DVC must run without an SCM.
            cmd.append("--no-scm")
        _run(cmd, cwd=repo_root)
        logger.info("DVC initialised")
    else:
        logger.debug("DVC already initialised (.dvc/ exists)")

    if _remote_already_configured(repo_root, remote_name):
        logger.info("DVC remote '%s' already configured — skipping", remote_name)
        return

    supabase_url = os.environ["SUPABASE_URL"]
    service_role_key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    bucket = os.environ["SUPABASE_RAW_DATA_BUCKET"]

    parsed = urlparse(supabase_url)
    project_ref = parsed.hostname.split(".")[0]
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
    repo_root = file_path.resolve().parents[1] if file_path.parent.name == "exports" else file_path.parent
    clean_stale_locks(repo_root)
    _run(["dvc", "add", str(file_path)], cwd=repo_root)
    logger.info("DVC tracked: %s  →  %s.dvc", file_path.name, file_path.name)


def dvc_push(repo_root: Path, target: Path | str | None = None, remote_name: str = "supabase") -> None:
    """Push DVC-tracked files to the configured remote."""
    clean_stale_locks(repo_root)
    cmd = ["dvc", "push", "-r", remote_name]
    if target:
        cmd.append(str(target))
    logger.info("Pushing DVC cache to remote '%s' …", remote_name)
    _run(cmd, cwd=repo_root)
    logger.info("DVC push complete ✅")


def git_commit_dvc_changes(repo_root: Path, message: str) -> bool:
    """
    Stage and commit .dvc files and exports/.gitignore if changed.

    Returns True if a commit was made, False if there was nothing to commit.
    """
    if not _in_git_repo(repo_root):
        logger.warning("No git repository at %s - skipping DVC pointer commit.", repo_root)
        return False

    # Check if there are any changes in exports/ or .dvc/
    status = _run(["git", "status", "--porcelain", "exports", ".dvc"], cwd=repo_root)
    if not status.strip():
        logger.info("No DVC pointer changes to commit in git.")
        return False

    _run(["git", "add", "exports", ".dvc/config"], cwd=repo_root)
    _run(["git", "commit", "-m", message], cwd=repo_root)
    logger.info("Git committed DVC version changes: %s", message)
    return True
