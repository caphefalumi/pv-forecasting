#!/usr/bin/env bash
set -euo pipefail

# Weekly PV Pipeline Run script
# Requires: PV_S3_URI environment variable (e.g. s3://my-bucket/pv-forecast)
# No AWS credentials in code; uses ambient IAM role or AWS CLI configuration.

if [ -z "${PV_S3_URI:-}" ]; then
  echo "ERROR: PV_S3_URI environment variable is not set." >&2
  exit 1
fi

echo "=== Syncing raw data and metadata from S3 ==="
aws s3 sync "${PV_S3_URI}/raw/" data/

echo "=== Running weekly pipeline snapshot ==="
python -m pvdata run

echo "=== Syncing new raw data to S3 (no --delete) ==="
aws s3 sync data/dkasc_api_5min/ "${PV_S3_URI}/raw/dkasc_api_5min/"
aws s3 sync data/open_meteo_raw/ "${PV_S3_URI}/raw/open_meteo_raw/"

# Read the newly built version from latest.json
LATEST_VERSION=$(python -c 'import json; print(json.load(open("data/processed/latest.json"))["version"])')
echo "=== Snapshot built: ${LATEST_VERSION} ==="

echo "=== Syncing processed snapshot and latest.json to S3 ==="
aws s3 sync "data/processed/${LATEST_VERSION}" "${PV_S3_URI}/processed/${LATEST_VERSION}"
aws s3 cp "data/processed/latest.json" "${PV_S3_URI}/processed/latest.json"

echo "=== Weekly pipeline run completed successfully ==="
