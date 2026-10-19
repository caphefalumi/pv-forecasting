#!/usr/bin/env bash
set -euo pipefail

# Weekly PV Pipeline Run script
# Object storage = Supabase S3-compatible Storage (not AWS S3).
# Requires:
#   PV_S3_URI              e.g. s3://processed_data
#   AWS_ENDPOINT_URL_S3    e.g. https://<ref>.supabase.co/storage/v1/s3
#   AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY  (Supabase S3 keys)

if [ -z "${PV_S3_URI:-}" ]; then
  echo "ERROR: PV_S3_URI environment variable is not set." >&2
  exit 1
fi

ENDPOINT="${AWS_ENDPOINT_URL_S3:-${MLFLOW_S3_ENDPOINT_URL:-}}"
if [ -z "$ENDPOINT" ]; then
  echo "ERROR: Set AWS_ENDPOINT_URL_S3 (or MLFLOW_S3_ENDPOINT_URL) to the Supabase S3 endpoint." >&2
  exit 1
fi

s3() {
  aws --endpoint-url "$ENDPOINT" s3 "$@"
}

echo "=== Syncing raw data and metadata from Supabase S3 ==="
s3 sync "${PV_S3_URI}/raw/" data/

echo "=== Running weekly pipeline snapshot ==="
python -m pvdata run

echo "=== Syncing new raw data to Supabase S3 (no --delete) ==="
s3 sync data/dkasc_api_5min/ "${PV_S3_URI}/raw/dkasc_api_5min/"
s3 sync data/open_meteo_raw/ "${PV_S3_URI}/raw/open_meteo_raw/"

LATEST_VERSION=$(python -c 'import json; print(json.load(open("data/processed/latest.json"))["version"])')
echo "=== Snapshot built: ${LATEST_VERSION} ==="

echo "=== Syncing processed snapshot and latest.json to Supabase S3 ==="
s3 sync "data/processed/${LATEST_VERSION}" "${PV_S3_URI}/processed/${LATEST_VERSION}"
s3 cp "data/processed/latest.json" "${PV_S3_URI}/processed/latest.json"

echo "=== Weekly pipeline run completed successfully ==="
