#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh"

require_bin curl

BOT_TOKEN="${TELEGRAM_ALERT_BOT_TOKEN:-}"
CHAT_ID="${TELEGRAM_ALERT_CHAT_ID:-}"
MESSAGE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --bot-token)
      BOT_TOKEN="$2"
      shift 2
      ;;
    --chat-id)
      CHAT_ID="$2"
      shift 2
      ;;
    --message)
      MESSAGE="$2"
      shift 2
      ;;
    *)
      prod_die "Unknown argument: $1"
      ;;
  esac
done

[[ -n "${BOT_TOKEN}" ]] || prod_die "Missing Telegram bot token."
[[ -n "${CHAT_ID}" ]] || prod_die "Missing Telegram chat ID."
[[ -n "${MESSAGE}" ]] || prod_die "Missing alert message."

curl -fsS -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
  -d "chat_id=${CHAT_ID}" \
  --data-urlencode "text=${MESSAGE}" \
  -d "disable_web_page_preview=true" \
  >/dev/null
