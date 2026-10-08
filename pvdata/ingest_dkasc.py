"""DKASC API gap ingest module.

Features & guarantees:
- Fetches 5-minute DKASC data in 3-day chunks.
- Completeness gate:
  - Header starts with 'Timestamp'
  - >= 51 columns
  - Every column in source_names.csv
  - Rows >= 98% of (288 * days)
  - End date strictly before today (local Darwin time)
- No overlaps with existing chunks.
- 3 retries with backoff.
- Politeness: >= 2s sleep, User-Agent: COS40007-student-project.
"""

from __future__ import annotations

import csv
import json
import logging
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Set, Tuple

from .config import PipelineConfig

logger = logging.getLogger(__name__)

BASE_URL = "https://solarcentre.spinifexvalley.com.au/power/export/average"
ACTIVE_POWER_QUANTITY = 193
WEATHER_SOURCE_ID = "101"
WEATHER_QUANTITIES = [10021, 9000, 9304, 506]  # rain, temp, GHI, wind

CHUNK_DAYS = 3
SLEEP_BETWEEN_REQUESTS_S = 2
USER_AGENT = "COS40007-student-project"

ARRAY_IDS = [
    52, 54, 55, 56, 57, 58, 59, 60, 61, 63, 64, 66, 67, 68, 69, 70, 71, 72, 73,
    74, 77, 78, 79, 81, 84, 85, 86, 87, 89, 90, 91, 92, 93, 95, 97, 98, 99, 100,
    102, 103, 105, 106, 212, 213, 214, 218
]


def _darwin_today() -> date:
    """Current date in Australia/Darwin timezone (UTC+09:30)."""
    return datetime.now(timezone(timedelta(hours=9, minutes=30))).date()


def _get_existing_chunk_ranges(api_dir: Path) -> List[Tuple[date, date, Path]]:
    """Return sorted list of (start_date, end_date, path) for existing chunks."""
    ranges = []
    if not api_dir.exists():
        return ranges

    for p in api_dir.glob("*.csv"):
        if p.name == "source_names.csv":
            continue
        m = re.match(r"^(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})\.csv$", p.name)
        if m:
            try:
                s = date.fromisoformat(m.group(1))
                e = date.fromisoformat(m.group(2))
                ranges.append((s, e, p))
            except ValueError:
                continue
    ranges.sort(key=lambda x: (x[0], x[1]))
    return ranges


def get_default_start_date(api_dir: Path) -> date:
    """Default start: day after the last fully covered day in dkasc_api_5min."""
    ranges = _get_existing_chunk_ranges(api_dir)
    if not ranges:
        return date(2026, 2, 12)
    max_end = max(r[1] for r in ranges)
    return max_end + timedelta(days=1)


def _load_allowed_columns(source_names_path: Path) -> Set[str]:
    """Load export_column names from source_names.csv."""
    if not source_names_path.exists():
        logger.warning("source_names.csv not found at %s", source_names_path)
        return set()
    with open(source_names_path, "r", encoding="utf-8", errors="replace") as fh:
        reader = csv.DictReader(fh)
        return {row["export_column"].strip() for row in reader if row.get("export_column")}


def validate_chunk(
    csv_text: str,
    start_date: date,
    end_date: date,
    allowed_columns: Set[str],
    today_darwin: Optional[date] = None,
) -> Tuple[bool, str]:
    """Check completeness gate (valid row count and columns).

    Returns:
        (is_valid, reason)
    """
    if today_darwin is None:
        today_darwin = _darwin_today()

    if end_date >= today_darwin:
        return False, f"Chunk end {end_date} is today or later ({today_darwin})"

    lines = csv_text.splitlines()
    if not lines:
        return False, "Empty response"

    first_line = lines[0].strip()
    if not (first_line.startswith("Timestamp") or first_line.startswith('"Timestamp"')):
        return False, f"Header does not start with Timestamp: {first_line[:50]}"

    reader = csv.reader([lines[0]])
    header = next(reader, [])
    if len(header) < 51:
        return False, f"Header has only {len(header)} columns (< 51)"

    if allowed_columns:
        data_cols = [c.strip() for c in header[1:] if c.strip()]
        unknown_cols = [c for c in data_cols if c not in allowed_columns]
        if unknown_cols:
            return False, f"Found {len(unknown_cols)} columns not in source_names.csv: {unknown_cols[:3]}"

    n_days = (end_date - start_date).days + 1
    expected_rows = 288 * n_days
    min_rows = int(0.98 * expected_rows)
    data_rows_count = len(lines) - 1

    if data_rows_count < min_rows:
        return False, f"Row count {data_rows_count} is < 98% of {expected_rows} ({min_rows})"

    return True, "OK"


def fetch_request(
    start: date,
    end: date,
    sources: dict,
    retries: int = 3,
) -> Optional[str]:
    """Execute HTTP GET to DKASC endpoint with retries and politeness."""
    q = urllib.parse.urlencode({
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "interval": "",
        "sources": json.dumps(sources),
    })
    url = f"{BASE_URL}?{q}"

    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": USER_AGENT},
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = resp.read().decode("utf-8")
                return data
        except Exception as exc:
            logger.warning("Attempt %d/%d failed for %s -> %s: %s", attempt, retries, start, end, exc)
            if attempt < retries:
                time.sleep(5 * attempt)
            else:
                raise
    return None


def fetch_dkasc(
    cfg: PipelineConfig,
    start: Optional[str] = None,
    end: Optional[str] = None,
) -> int:
    """Fetch DKASC gap data chunks from start to end (local dates).

    Returns 0 on success, non-zero on failure.
    """
    api_dir = cfg.path(cfg.api_dir)
    api_dir.mkdir(parents=True, exist_ok=True)
    name_map_path = cfg.path(cfg.api_name_map)

    today = _darwin_today()
    start_dt = date.fromisoformat(start) if start else get_default_start_date(api_dir)
    end_dt = date.fromisoformat(end) if end else (today - timedelta(days=1))

    if start_dt > end_dt:
        logger.info("Start date %s is after end date %s. Nothing to fetch.", start_dt, end_dt)
        return 0

    allowed_cols = _load_allowed_columns(name_map_path)
    existing_ranges = _get_existing_chunk_ranges(api_dir)

    sources = {str(i): [ACTIVE_POWER_QUANTITY] for i in ARRAY_IDS}
    sources[WEATHER_SOURCE_ID] = WEATHER_QUANTITIES

    logger.info("Fetching DKASC data from %s to %s (chunks of %d days)...", start_dt, end_dt, CHUNK_DAYS)

    curr = start_dt
    failures = []

    while curr <= end_dt:
        chunk_end = min(curr + timedelta(days=CHUNK_DAYS - 1), end_dt)

        # Check for overlaps with existing chunks
        overlap = False
        for ex_s, ex_e, ex_p in existing_ranges:
            if max(curr, ex_s) <= min(chunk_end, ex_e):
                overlap = True
                logger.info("Chunk %s -> %s overlaps %s (%s -> %s), skipping.",
                            curr, chunk_end, ex_p.name, ex_s, ex_e)
                curr = ex_e + timedelta(days=1)
                break
        if overlap:
            continue

        chunk_file = api_dir / f"{curr}_{chunk_end}.csv"
        if chunk_file.exists():
            logger.info("Chunk %s already exists, skipping.", chunk_file.name)
            curr = chunk_end + timedelta(days=1)
            continue

        logger.info("Requesting DKASC: %s -> %s ...", curr, chunk_end)
        try:
            txt = fetch_request(curr, chunk_end, sources, retries=3)
        except Exception as exc:
            logger.error("Failed to fetch DKASC chunk %s -> %s: %s", curr, chunk_end, exc)
            return 1

        is_valid, reason = validate_chunk(txt or "", curr, chunk_end, allowed_cols, today_darwin=today)
        if not is_valid:
            logger.warning("Chunk %s -> %s rejected by completeness gate: %s", curr, chunk_end, reason)
            failures.append(f"{curr}_{chunk_end} ({reason})")
        else:
            chunk_file.write_text(txt, encoding="utf-8")
            logger.info("Wrote %s (%d rows)", chunk_file.name, len(txt.splitlines()) - 1)
            existing_ranges.append((curr, chunk_end, chunk_file))

        time.sleep(SLEEP_BETWEEN_REQUESTS_S)
        curr = chunk_end + timedelta(days=1)

    if failures:
        logger.warning("Completed with some rejected chunks: %s", failures)
        return 1

    logger.info("fetch-dkasc completed successfully.")
    return 0
