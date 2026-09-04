#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
MONITOR_DIR="${ROOT_DIR}/monitor"
BUILD_DIR="${ROOT_DIR}/build"
STAGE_DIR="${BUILD_DIR}/stage"
ZIP_PATH="${BUILD_DIR}/monitor.zip"

rm -rf "${STAGE_DIR}" "${ZIP_PATH}"
mkdir -p "${STAGE_DIR}"

cp "${MONITOR_DIR}/handler.py" "${STAGE_DIR}/"

if [ -f "${MONITOR_DIR}/requirements.txt" ]; then
  # psycopg2-binary ships compiled C extensions. Force the actual Lambda
  # runtime's platform/wheels regardless of what OS this script runs on
  # (e.g. a Mac) - without --only-binary, pip would happily build a wheel
  # for the host instead, which then fails to import once deployed.
  pip install --quiet --no-cache-dir \
    --platform manylinux2014_x86_64 \
    --python-version 3.12 \
    --implementation cp \
    --only-binary=:all: \
    -r "${MONITOR_DIR}/requirements.txt" -t "${STAGE_DIR}"
fi

find "${STAGE_DIR}" -name "__pycache__" -type d -prune -exec rm -rf {} +

(cd "${STAGE_DIR}" && zip -r -q "${ZIP_PATH}" . -x "*.dist-info/*" "*.egg-info/*")
rm -rf "${STAGE_DIR}"

echo "Built ${ZIP_PATH}"
