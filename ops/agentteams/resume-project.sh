#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="${1:-}"
CONTAINER_CMD="${PASAY_WATCHDOG_CONTAINER_CMD:-docker}"

[[ "${PROJECT_ID}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || {
  printf '%s\n' 'usage: resume-project.sh <project-id>' >&2
  exit 2
}

for worker in lead auditor builder qa reviewer brake; do
  "${CONTAINER_CMD}" unpause "agentteams-worker-pasay-${worker}" 2>/dev/null || true
done

"${CONTAINER_CMD}" exec agentteams-manager \
  agt project resume "${PROJECT_ID}"

printf 'PASAY_PROJECT_RESUMED project=%s\n' "${PROJECT_ID}"
