"""CLI entry point for the PV data pipeline.

Usage:
    python -m pvdata fetch-meteo [--start 2024-01-01] [--end 2026-09-24]
    python -m pvdata fetch-dkasc [--start 2026-09-26] [--end 2026-10-01]
    python -m pvdata build       --version v0 [--end 2026-02-11] [--force] [--overwrite]
    python -m pvdata validate    --version v0
    python -m pvdata sample      --version v0 --start 2025-06-01 --end 2025-08-31
    python -m pvdata run         [--until 2026-10-01] [--skip-fetch] [--force] [--overwrite]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


def _setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m pvdata",
        description="PV Historical Data Pipeline (DKASC + Open-Meteo)",
    )
    parser.add_argument("--config", default=None, help="Path to pipeline.yaml")
    parser.add_argument("--data-root", default=None, help="Override data_root in config")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")

    subparsers = parser.add_subparsers(dest="command", required=True)

    # fetch-meteo
    p_fetch = subparsers.add_parser("fetch-meteo", help="Fetch Open-Meteo Previous Runs data")
    p_fetch.add_argument("--start", default=None, help="Start date YYYY-MM-DD")
    p_fetch.add_argument("--end", default=None, help="End date YYYY-MM-DD")

    # fetch-dkasc
    p_fetch_dkasc = subparsers.add_parser("fetch-dkasc", help="Fetch DKASC API 5-minute data")
    p_fetch_dkasc.add_argument("--start", default=None, help="Start date YYYY-MM-DD (local)")
    p_fetch_dkasc.add_argument("--end", default=None, help="End date YYYY-MM-DD (local)")

    # build
    p_build = subparsers.add_parser("build", help="Build versioned training table")
    p_build.add_argument("--version", required=True, help="Version string e.g. v0")
    p_build.add_argument("--end", default=None, help="Override window end date")
    p_build.add_argument("--force", action="store_true", help="Force rebuild of interim files")
    p_build.add_argument("--overwrite", action="store_true", help="Allow overwriting existing version")

    # validate
    p_val = subparsers.add_parser("validate", help="Validate a versioned build")
    p_val.add_argument("--version", required=True, help="Version string e.g. v0")

    # sample
    p_sample = subparsers.add_parser("sample", help="Export teammate sample CSVs")
    p_sample.add_argument("--version", required=True, help="Version string e.g. v0")
    p_sample.add_argument("--start", required=True, help="Sample start date YYYY-MM-DD (local)")
    p_sample.add_argument("--end", required=True, help="Sample end date YYYY-MM-DD (local)")

    # run
    p_run = subparsers.add_parser("run", help="Run full pipeline snapshot (fetch, build, validate, update latest)")
    p_run.add_argument("--until", default=None, help="Snapshot end date YYYY-MM-DD (local Darwin)")
    p_run.add_argument("--skip-fetch", action="store_true", help="Skip fetching raw data")
    p_run.add_argument("--force", action="store_true", help="Force rebuild of interim files")
    p_run.add_argument("--overwrite", action="store_true", help="Allow overwriting existing version")

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    # Load config
    from .config import load_config
    cfg = load_config(config_path=args.config, data_root_override=args.data_root)

    if args.command == "fetch-meteo":
        from .io_openmeteo import fetch
        fetch(cfg, start=args.start, end=args.end)

    elif args.command == "fetch-dkasc":
        from .ingest_dkasc import fetch_dkasc
        rc = fetch_dkasc(cfg, start=args.start, end=args.end)
        sys.exit(rc)

    elif args.command == "build":
        from .build import build
        build(
            version=args.version,
            cfg=cfg,
            end_override=args.end,
            force=args.force,
            overwrite=args.overwrite,
        )

    elif args.command == "validate":
        from .validate import validate
        rc = validate(version=args.version, cfg=cfg)
        sys.exit(rc)

    elif args.command == "sample":
        from .sample import export_sample
        export_sample(version=args.version, start=args.start, end=args.end, cfg=cfg)

    elif args.command == "run":
        from .ingest_dkasc import fetch_dkasc, _darwin_today
        from .io_openmeteo import fetch as fetch_meteo
        from .build import build, _git_commit
        from .validate import validate

        darwin_today = _darwin_today()
        until = args.until or str(darwin_today - timedelta(days=1))
        version = until

        if not args.skip_fetch:
            logging.info("=== Run Step 1: fetch-dkasc up to %s ===", until)
            rc = fetch_dkasc(cfg, start=None, end=until)
            if rc != 0:
                logging.error("fetch-dkasc failed")
                sys.exit(rc)

            until_dt = date.fromisoformat(until)
            meteo_end = str(until_dt + timedelta(days=1))
            logging.info("=== Run Step 2: fetch-meteo up to %s ===", meteo_end)
            fetch_meteo(cfg, start=None, end=meteo_end)

        logging.info("=== Run Step 3: build version %s with end %s ===", version, until)
        build(
            version=version,
            cfg=cfg,
            end_override=until,
            force=args.force,
            overwrite=args.overwrite,
        )

        logging.info("=== Run Step 4: validate version %s ===", version)
        rc = validate(version=version, cfg=cfg)
        if rc != 0:
            logging.error("Validation failed for version %s", version)
            sys.exit(rc)

        # Step 5: Update latest.json
        proc_dir = cfg.path("processed")
        latest_data = {
            "version": version,
            "until": until,
            "build_time_utc": datetime.now(timezone.utc).isoformat(),
            "git_commit": _git_commit(),
        }
        latest_path = proc_dir / "latest.json"
        with open(latest_path, "w", encoding="utf-8") as fh:
            json.dump(latest_data, fh, indent=2)
        logging.info("Updated latest.json -> version %s (until %s)", version, until)

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
