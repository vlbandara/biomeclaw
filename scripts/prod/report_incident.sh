#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh"

require_bin gh
require_bin python3

REPO="${GITHUB_REPOSITORY:-}"
SIGNATURE=""
SUBSYSTEM="prod"
SUMMARY=""
RUN_URL="${GITHUB_SERVER_URL:-https://github.com}/${GITHUB_REPOSITORY:-}/actions/runs/${GITHUB_RUN_ID:-}"
ARTIFACT_NOTE=""
STATUS="open"
LABELS=(incident prod monitor)

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo)
      REPO="$2"
      shift 2
      ;;
    --signature)
      SIGNATURE="$2"
      shift 2
      ;;
    --subsystem)
      SUBSYSTEM="$2"
      shift 2
      ;;
    --summary)
      SUMMARY="$2"
      shift 2
      ;;
    --run-url)
      RUN_URL="$2"
      shift 2
      ;;
    --artifact-note)
      ARTIFACT_NOTE="$2"
      shift 2
      ;;
    --status)
      STATUS="$2"
      shift 2
      ;;
    --label)
      LABELS+=("$2")
      shift 2
      ;;
    *)
      prod_die "Unknown argument: $1"
      ;;
  esac
done

[[ -n "${REPO}" ]] || prod_die "Missing GitHub repository."
[[ -n "${SIGNATURE}" ]] || prod_die "Missing incident signature."
[[ -n "${SUMMARY}" ]] || prod_die "Missing incident summary."

title="[prod:${SIGNATURE}] ${SUMMARY}"
issue_number="$(gh issue list --repo "${REPO}" --state open --label incident --search "\"${title}\" in:title" --json number,title --jq '.[0].number')"

for label in "${LABELS[@]}" "${SUBSYSTEM}"; do
  gh label create "${label}" --repo "${REPO}" --color CFD3D7 --description "Managed by production reliability automation" --force >/dev/null 2>&1 || true
done

comment_body="$(python3 - "${SUMMARY}" "${RUN_URL}" "${ARTIFACT_NOTE}" "${STATUS}" <<'PY'
import sys
summary, run_url, artifact_note, status = sys.argv[1:]
lines = [
    f"Status: {status}",
    f"Summary: {summary}",
    f"Run: {run_url}",
]
if artifact_note:
    lines.append(f"Artifacts: {artifact_note}")
print("\n".join(lines))
PY
)"

if [[ -n "${issue_number}" && "${issue_number}" != "null" ]]; then
  gh issue comment "${issue_number}" --repo "${REPO}" --body "${comment_body}" >/dev/null
  if [[ "${STATUS}" == "resolved" ]]; then
    gh issue close "${issue_number}" --repo "${REPO}" --comment "Production monitor reports recovery. Closing incident." >/dev/null
  fi
  printf '%s\n' "${issue_number}"
  exit 0
fi

body="$(python3 - "${SUMMARY}" "${RUN_URL}" "${ARTIFACT_NOTE}" <<'PY'
import sys
summary, run_url, artifact_note = sys.argv[1:]
lines = [
    "## Symptom",
    summary,
    "",
    "## First Seen",
    "Automated production monitor",
    "",
    "## Affected Path",
    "See workflow artifacts and monitor summary.",
    "",
    "## Logs / Artifacts",
    artifact_note or "Attached in the linked workflow run.",
    "",
    "## Root Cause",
    "Triage pending.",
    "",
    "## Fix Commit / PR",
    "Triage pending.",
    "",
    "## Follow-up Prevention",
    "Triage pending.",
    "",
    f"Workflow run: {run_url}",
]
print("\n".join(lines))
PY
)"

gh issue create \
  --repo "${REPO}" \
  --title "${title}" \
  --label incident \
  --label prod \
  --label monitor \
  --label "${SUBSYSTEM}" \
  --body "${body}" \
  >/dev/null
