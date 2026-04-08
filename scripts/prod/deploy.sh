#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh"

require_bin git
require_bin rsync
require_bin ssh

HOST="${PROD_HOST:-}"
USER_NAME="${PROD_USER:-root}"
APP_DIR="${PROD_APP_DIR:-/opt/biomeclaw}"
LEGACY_DIR="${PROD_LEGACY_DIR:-/opt/TradingAgents}"
STATE_DIR="${PROD_STATE_DIR:-/root/.nanobot}"
BASE_URL="${PROD_BASE_URL:-}"
DRY_RUN=0
OUT_DIR=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)
      HOST="$2"
      shift 2
      ;;
    --user)
      USER_NAME="$2"
      shift 2
      ;;
    --app-dir)
      APP_DIR="$2"
      shift 2
      ;;
    --legacy-dir)
      LEGACY_DIR="$2"
      shift 2
      ;;
    --state-dir)
      STATE_DIR="$2"
      shift 2
      ;;
    --base-url)
      BASE_URL="$2"
      shift 2
      ;;
    --out-dir)
      OUT_DIR="$2"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    *)
      prod_die "Unknown argument: $1"
      ;;
  esac
done

[[ -n "${HOST}" ]] || prod_die "Missing production host. Set PROD_HOST or pass --host."
BASE_URL="${BASE_URL:-$(default_base_url "${HOST}")}"
OUT_DIR="${OUT_DIR:-$(mktemp -d "${TMPDIR:-/tmp}/biomeclaw-deploy.XXXXXX")}"
ensure_dir "${OUT_DIR}"
release_sha="${GITHUB_SHA:-$(git -C "${PROD_ROOT_DIR}" rev-parse HEAD)}"
deployed_at="$(timestamp_utc)"

rsync_flags=(
  -az
  --delete
  --exclude=.git
  --exclude=.venv
  --exclude=.env
  --exclude=.cursor
  --exclude=.pytest_cache
  --exclude=.ruff_cache
  --exclude=.mypy_cache
  --exclude=__pycache__
  --exclude=.DS_Store
  --exclude=results
  --exclude=eval_results
  --exclude=*.pyc
)

if (( DRY_RUN == 1 )); then
  rsync_flags+=(--dry-run --itemize-changes)
  dry_run_target="${OUT_DIR}/dry-run-target"
  mkdir -p "${dry_run_target}"
  prod_log "Running local dry-run sync into ${dry_run_target}"
  rsync "${rsync_flags[@]}" "${PROD_ROOT_DIR}/" "${dry_run_target}/" | tee "${OUT_DIR}/rsync.txt"
  prod_log "Dry run complete"
  exit 0
fi

target="$(ssh_target "${HOST}" "${USER_NAME}")"
prod_log "Preparing ${target}:${APP_DIR}"
ssh "${target}" "mkdir -p '${APP_DIR}' '${STATE_DIR}/workspace' '${STATE_DIR}/whatsapp-auth' && test -f '${APP_DIR}/.env'" || prod_die "Remote .env missing at ${APP_DIR}/.env. Create it on the server first."

prod_log "Syncing application source"
rsync "${rsync_flags[@]}" "${PROD_ROOT_DIR}/" "${target}:${APP_DIR}/" | tee "${OUT_DIR}/rsync.txt"

prod_log "Updating remote release markers and restarting services"
ssh "${target}" "
set -euo pipefail
timestamp=\$(date +%Y%m%d-%H%M%S)
if [ -d '${LEGACY_DIR}' ] && [ '${LEGACY_DIR}' != '${APP_DIR}' ]; then
  if [ ! -d '${LEGACY_DIR}.backup-'\"\${timestamp}\" ]; then
    cd '${LEGACY_DIR}'
    docker compose down || true
    mv '${LEGACY_DIR}' '${LEGACY_DIR}.backup-'\${timestamp}
  fi
fi
cd '${APP_DIR}'
python3 - <<'PY'
from pathlib import Path
env_path = Path('.env')
release_sha = '${release_sha}'
deployed_at = '${deployed_at}'
lines = env_path.read_text(encoding='utf-8').splitlines()
updates = {
    'APP_RELEASE': release_sha,
    'APP_DEPLOYED_AT': deployed_at,
}
seen = set()
new_lines = []
for line in lines:
    if '=' not in line or line.lstrip().startswith('#'):
        new_lines.append(line)
        continue
    key, _, _ = line.partition('=')
    if key in updates:
        new_lines.append(f'{key}={updates[key]}')
        seen.add(key)
    else:
        new_lines.append(line)
for key, value in updates.items():
    if key not in seen:
        new_lines.append(f'{key}={value}')
env_path.write_text('\\n'.join(new_lines) + '\\n', encoding='utf-8')
PY
docker compose up -d --build --remove-orphans
docker compose ps
" | tee "${OUT_DIR}/deploy-remote.txt"

prod_log "Running post-deploy smoke check"
if ! "${SCRIPT_DIR}/smoke_check.sh" --host "${HOST}" --user "${USER_NAME}" --app-dir "${APP_DIR}" --base-url "${BASE_URL}" --out-dir "${OUT_DIR}/smoke"; then
  prod_log "Smoke check failed; collecting diagnostics"
  "${SCRIPT_DIR}/collect_diagnostics.sh" --host "${HOST}" --user "${USER_NAME}" --app-dir "${APP_DIR}" --base-url "${BASE_URL}" --out-dir "${OUT_DIR}/diagnostics" || true
  exit 1
fi

prod_log "Deployment completed successfully"
