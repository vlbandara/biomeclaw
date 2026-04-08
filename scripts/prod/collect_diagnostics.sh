#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh"

require_bin curl
require_bin ssh

BASE_URL=""
HOST="${PROD_HOST:-}"
USER_NAME="${PROD_USER:-root}"
APP_DIR="${PROD_APP_DIR:-/opt/biomeclaw}"
LOG_SINCE="${LOG_SINCE:-2h}"
OUT_DIR=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --base-url)
      BASE_URL="$2"
      shift 2
      ;;
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
    --since)
      LOG_SINCE="$2"
      shift 2
      ;;
    --out-dir)
      OUT_DIR="$2"
      shift 2
      ;;
    *)
      prod_die "Unknown argument: $1"
      ;;
  esac
done

BASE_URL="${BASE_URL:-$(default_base_url "${HOST}")}"
OUT_DIR="${OUT_DIR:-$(mktemp -d "${TMPDIR:-/tmp}/biomeclaw-diagnostics.XXXXXX")}"
ensure_dir "${OUT_DIR}"
ensure_dir "${OUT_DIR}/endpoints"

target="$(ssh_target "${HOST}" "${USER_NAME}")"

prod_log "Collecting diagnostics into ${OUT_DIR}"

git -C "${PROD_ROOT_DIR}" rev-parse HEAD >"${OUT_DIR}/git-head.txt" 2>/dev/null || true
git -C "${PROD_ROOT_DIR}" status --short >"${OUT_DIR}/git-status.txt" 2>/dev/null || true
timestamp_utc >"${OUT_DIR}/collected-at.txt"

for endpoint in healthz readyz metrics api/admin/status; do
  safe_name="${endpoint//\//_}"
  curl -sS -X GET -H 'Accept: application/json' -D "${OUT_DIR}/endpoints/${safe_name}.headers" -o "${OUT_DIR}/endpoints/${safe_name}.body" "${BASE_URL%/}/${endpoint}" || true
done

ssh "${target}" "uname -a" >"${OUT_DIR}/remote-uname.txt" 2>"${OUT_DIR}/remote-uname.stderr" || true
ssh "${target}" "docker version" >"${OUT_DIR}/docker-version.txt" 2>"${OUT_DIR}/docker-version.stderr" || true
ssh "${target}" "cd '${APP_DIR}' && docker compose ps" >"${OUT_DIR}/docker-compose-ps.txt" 2>"${OUT_DIR}/docker-compose-ps.stderr" || true
ssh "${target}" "cd '${APP_DIR}' && docker compose logs --since '${LOG_SINCE}' --timestamps --no-color" >"${OUT_DIR}/docker-compose-logs.txt" 2>"${OUT_DIR}/docker-compose-logs.stderr" || true
ssh "${target}" "docker ps -a --format '{{.Names}}\t{{.Image}}\t{{.Status}}'" >"${OUT_DIR}/docker-ps.txt" 2>"${OUT_DIR}/docker-ps.stderr" || true
ssh "${target}" "for cid in \$(cd '${APP_DIR}' && docker compose ps -q); do docker inspect --format '{{.Name}}|{{.Id}}|{{.Config.Image}}|{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}|{{.RestartCount}}' \"\$cid\"; done" >"${OUT_DIR}/docker-inspect-state.txt" 2>"${OUT_DIR}/docker-inspect-state.stderr" || true

tar -czf "${OUT_DIR}.tar.gz" -C "${OUT_DIR}" .
prod_log "Wrote ${OUT_DIR}.tar.gz"
printf '%s\n' "${OUT_DIR}.tar.gz"
