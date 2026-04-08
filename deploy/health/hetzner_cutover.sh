#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"

REMOTE_HOST="${REMOTE_HOST:-root@46.62.231.14}"
REMOTE_APP_DIR="${REMOTE_APP_DIR:-/opt/biomeclaw}"
REMOTE_LEGACY_DIR="${REMOTE_LEGACY_DIR:-/opt/TradingAgents}"
REMOTE_STATE_DIR="${REMOTE_STATE_DIR:-/root/.nanobot}"
ENV_FILE="${ENV_FILE:-${ROOT_DIR}/.env}"

required_vars=(
  DOMAIN
  MINIMAX_API_KEY
  TELEGRAM_BOT_TOKEN
  HEALTH_VAULT_KEY
  POSTGRES_PASSWORD
  HEALTH_ONBOARDING_BASE_URL
)

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "Missing env file: ${ENV_FILE}" >&2
  exit 1
fi

missing_vars=()
for var_name in "${required_vars[@]}"; do
  if ! grep -Eq "^${var_name}=.+" "${ENV_FILE}"; then
    missing_vars+=("${var_name}")
  fi
done

if (( ${#missing_vars[@]} > 0 )); then
  printf 'Missing required values in %s:\n' "${ENV_FILE}" >&2
  printf '  - %s\n' "${missing_vars[@]}" >&2
  exit 1
fi

echo "Preparing remote app directory on ${REMOTE_HOST}..."
ssh "${REMOTE_HOST}" "mkdir -p '${REMOTE_APP_DIR}'"

echo "Syncing BiomeClaw source to ${REMOTE_HOST}:${REMOTE_APP_DIR}..."
rsync -avz \
  --exclude='.git' \
  --exclude='.venv' \
  --exclude='results' \
  --exclude='.pytest_cache' \
  --exclude='.ruff_cache' \
  --exclude='.DS_Store' \
  "${ROOT_DIR}/" \
  "${REMOTE_HOST}:${REMOTE_APP_DIR}/"

echo "Uploading environment file..."
scp "${ENV_FILE}" "${REMOTE_HOST}:${REMOTE_APP_DIR}/.env"

echo "Stopping legacy stack, backing it up, and starting BiomeClaw..."
ssh "${REMOTE_HOST}" "
set -euo pipefail
timestamp=\$(date +%Y%m%d-%H%M%S)

if [ -d '${REMOTE_LEGACY_DIR}' ]; then
  cd '${REMOTE_LEGACY_DIR}'
  docker compose down || true
  mv '${REMOTE_LEGACY_DIR}' '${REMOTE_LEGACY_DIR}.backup-'\${timestamp}
fi

mkdir -p '${REMOTE_STATE_DIR}/workspace' '${REMOTE_STATE_DIR}/whatsapp-auth'

cd '${REMOTE_APP_DIR}'
docker compose up -d --build
docker compose ps
"

echo
echo "Cutover complete. Recommended follow-up checks:"
echo "  ssh ${REMOTE_HOST} 'cd ${REMOTE_APP_DIR} && docker compose logs --tail=200'"
echo "  curl -I https://<your-domain>/healthz"
