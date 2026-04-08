#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"

REMOTE_HOST="${REMOTE_HOST:-46.62.231.14}"
REMOTE_USER="${REMOTE_USER:-root}"
REMOTE_APP_DIR="${REMOTE_APP_DIR:-/opt/biomeclaw}"
REMOTE_LEGACY_DIR="${REMOTE_LEGACY_DIR:-/opt/TradingAgents}"
REMOTE_STATE_DIR="${REMOTE_STATE_DIR:-/root/.nanobot}"
PROD_BASE_URL="${PROD_BASE_URL:-http://${REMOTE_HOST}}"

exec "${ROOT_DIR}/scripts/prod/deploy.sh" \
  --host "${REMOTE_HOST}" \
  --user "${REMOTE_USER}" \
  --app-dir "${REMOTE_APP_DIR}" \
  --legacy-dir "${REMOTE_LEGACY_DIR}" \
  --state-dir "${REMOTE_STATE_DIR}" \
  --base-url "${PROD_BASE_URL}"
