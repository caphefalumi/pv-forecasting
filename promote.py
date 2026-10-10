#!/usr/bin/env python3
"""
promote.py — Promote a candidate model version to champion.

Assigns the 'champion' alias to the specified model version in MLflow,
removes the 'candidate' alias from it, tags the approver and timestamp,
and notifies Discord.

Usage:
    python promote.py                     # Promote current candidate model
    python promote.py --version 3         # Promote specific model version 3
    python promote.py --approver admin    # Specify approver name
    python promote.py --dry-run           # Test without changing MLflow/Discord
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent


def _setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def _load_env() -> None:
    env_path = REPO_ROOT / ".env"
    if env_path.exists():
        try:
            from dotenv import load_dotenv
            load_dotenv(env_path, override=True)
        except ImportError:

            pass


def _parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python promote.py",
        description="Promote a candidate PV-LSTM model to champion in MLflow",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--version",
        default=None,
        help="Model version to promote (e.g. 2). If omitted, the current 'candidate' is promoted.",
    )
    parser.add_argument(
        "--approver",
        default=os.environ.get("USER", os.environ.get("GITHUB_ACTOR", "admin")),
        help="Name or handle of the approving admin (default: current system/GitHub user).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate promotion without modifying MLflow or sending Discord notifications.",
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
    _load_env()

    logger = logging.getLogger(__name__)

    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))

    from jobs.retrain_tools.mlflow_helpers import (
        get_candidate_version,
        promote_to_champion,
    )
    from jobs.retrain_tools.discord_notify import send_promotion_notification

    target_version = args.version
    if not target_version:
        logger.info("No model version specified — querying current 'candidate' alias …")
        target_version = get_candidate_version()
        if not target_version:
            logger.error("❌ No model currently has the 'candidate' alias. Specify --version explicitly.")
            sys.exit(1)

    logger.info("Target model version for champion promotion: v%s", target_version)
    logger.info("Approver: %s", args.approver)

    if args.dry_run:
        logger.info("🔶 DRY RUN — would promote v%s to champion (approver: %s)", target_version, args.approver)
        return

    try:
        promoted_at = promote_to_champion(
            model_version=str(target_version),
            approver=args.approver,
        )
        send_promotion_notification(
            model_version=str(target_version),
            approver=args.approver,
            promoted_at=promoted_at,
        )
        logger.info("🎉 Model v%s is now the CHAMPION in MLflow!", target_version)
    except Exception as exc:
        logger.error("❌ Promotion failed: %s", exc, exc_info=args.verbose)
        sys.exit(1)


if __name__ == "__main__":
    main()
