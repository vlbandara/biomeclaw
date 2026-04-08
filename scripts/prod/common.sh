#!/usr/bin/env bash

set -euo pipefail

PROD_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROD_ROOT_DIR="$(cd "${PROD_SCRIPT_DIR}/../.." && pwd)"

prod_log() {
  printf '[prod] %s\n' "$*" >&2
}

prod_die() {
  printf '[prod] ERROR: %s\n' "$*" >&2
  exit 1
}

require_bin() {
  local bin="$1"
  command -v "${bin}" >/dev/null 2>&1 || prod_die "Missing required command: ${bin}"
}

timestamp_utc() {
  date -u +"%Y-%m-%dT%H:%M:%SZ"
}

ssh_target() {
  local host="${1:-${PROD_HOST:-}}"
  local user="${2:-${PROD_USER:-root}}"
  [[ -n "${host}" ]] || prod_die "Missing production host. Set PROD_HOST or pass --host."
  printf '%s@%s' "${user}" "${host}"
}

default_base_url() {
  local host="${1:-${PROD_HOST:-}}"
  if [[ -n "${PROD_BASE_URL:-}" ]]; then
    printf '%s' "${PROD_BASE_URL}"
    return
  fi
  [[ -n "${host}" ]] || prod_die "Missing production base URL. Set PROD_BASE_URL or pass --base-url."
  printf 'http://%s' "${host}"
}

ensure_dir() {
  local dir="$1"
  mkdir -p "${dir}"
}
