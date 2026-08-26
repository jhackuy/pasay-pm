#!/usr/bin/env bash
set -euo pipefail

AGENTTEAMS_PINNED_VERSION="${AGENTTEAMS_VERSION:-v1.2.3}"
PASAY_AGENT_MODEL="${PASAY_AGENT_MODEL:-}"
PASAY_GITHUB_MCP_URL="${PASAY_GITHUB_MCP_URL:-}"
AGENTTEAMS_OPENAI_BASE_URL="${AGENTTEAMS_OPENAI_BASE_URL:-}"
AGENTTEAMS_LLM_API_KEY="${AGENTTEAMS_LLM_API_KEY:-}"
AGENTTEAMS_ADMIN_PASSWORD="${AGENTTEAMS_ADMIN_PASSWORD:-}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

fail() {
  printf 'PASAY_AGENTTEAMS_BLOCKED: %s\n' "$1" >&2
  exit 1
}

require_value() {
  local name="$1"
  local value="$2"
  [ -n "${value}" ] || fail "missing required environment variable ${name}"
}

require_value PASAY_AGENT_MODEL "${PASAY_AGENT_MODEL}"
require_value PASAY_GITHUB_MCP_URL "${PASAY_GITHUB_MCP_URL}"
require_value AGENTTEAMS_OPENAI_BASE_URL "${AGENTTEAMS_OPENAI_BASE_URL}"
require_value AGENTTEAMS_LLM_API_KEY "${AGENTTEAMS_LLM_API_KEY}"
require_value AGENTTEAMS_ADMIN_PASSWORD "${AGENTTEAMS_ADMIN_PASSWORD}"

[[ "${PASAY_AGENT_MODEL}" =~ ^[A-Za-z0-9._:/-]+$ ]] || fail "PASAY_AGENT_MODEL contains unsupported characters"
[[ "${PASAY_GITHUB_MCP_URL}" =~ ^https?://[^[:space:]]+$ ]] || fail "PASAY_GITHUB_MCP_URL must be an http(s) URL"
[[ "${AGENTTEAMS_OPENAI_BASE_URL}" =~ ^https?://[^[:space:]]+$ ]] || fail "AGENTTEAMS_OPENAI_BASE_URL must be an http(s) URL"
[ "${#AGENTTEAMS_ADMIN_PASSWORD}" -ge 8 ] || fail "AGENTTEAMS_ADMIN_PASSWORD must be at least 8 characters"

command -v curl >/dev/null 2>&1 || fail "curl is required"
command -v python3 >/dev/null 2>&1 || fail "python3 is required"
if command -v docker >/dev/null 2>&1; then
  CONTAINER_CMD=docker
elif command -v podman >/dev/null 2>&1; then
  CONTAINER_CMD=podman
else
  fail "Docker or Podman is required"
fi
"${CONTAINER_CMD}" ps >/dev/null 2>&1 || fail "${CONTAINER_CMD} daemon is not available to this user"

PASAY_AGENTTEAMS_TMP="$(mktemp -d)"
cleanup() {
  rm -f -- "${PASAY_AGENTTEAMS_TMP}/agentteams-install.sh" \
    "${PASAY_AGENTTEAMS_TMP}/agentteams-apply.sh" \
    "${PASAY_AGENTTEAMS_TMP}/pasay-team.yaml"
  rmdir -- "${PASAY_AGENTTEAMS_TMP}" 2>/dev/null || true
}
trap cleanup EXIT

BASE_RAW_URL="https://raw.githubusercontent.com/agentscope-ai/AgentTeams/${AGENTTEAMS_PINNED_VERSION}"
curl --fail --silent --show-error --location \
  "${BASE_RAW_URL}/install/agentteams-install.sh" \
  --output "${PASAY_AGENTTEAMS_TMP}/agentteams-install.sh"
curl --fail --silent --show-error --location \
  "${BASE_RAW_URL}/install/agentteams-apply.sh" \
  --output "${PASAY_AGENTTEAMS_TMP}/agentteams-apply.sh"

export AGENTTEAMS_NON_INTERACTIVE=1
export AGENTTEAMS_LLM_PROVIDER=openai-compat
export AGENTTEAMS_DEFAULT_MODEL="${PASAY_AGENT_MODEL}"
export AGENTTEAMS_OPENAI_BASE_URL
export AGENTTEAMS_LLM_API_KEY
export AGENTTEAMS_ADMIN_PASSWORD
export PASAY_AGENT_MODEL
export PASAY_GITHUB_MCP_URL
export AGENTTEAMS_VERSION="${AGENTTEAMS_PINNED_VERSION}"
export AGENTTEAMS_MOUNT_SOCKET=1
export AGENTTEAMS_DASHBOARD=1

bash "${PASAY_AGENTTEAMS_TMP}/agentteams-install.sh" manager

python3 - "${SCRIPT_DIR}/pasay-team.yaml.tpl" "${PASAY_AGENTTEAMS_TMP}/pasay-team.yaml" <<'PY'
import os
import pathlib
import sys

source = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
rendered = source.replace("__PASAY_AGENT_MODEL__", os.environ["PASAY_AGENT_MODEL"])
rendered = rendered.replace("__PASAY_GITHUB_MCP_URL__", os.environ["PASAY_GITHUB_MCP_URL"])
if "__PASAY_" in rendered:
    raise SystemExit("unresolved PASAY template placeholder")
pathlib.Path(sys.argv[2]).write_text(rendered, encoding="utf-8")
PY

AGENTTEAMS_CONTAINER_CMD="${CONTAINER_CMD}" \
  bash "${PASAY_AGENTTEAMS_TMP}/agentteams-apply.sh" -f "${PASAY_AGENTTEAMS_TMP}/pasay-team.yaml"

PASAY_WATCHDOG_CONTAINER_CMD="${CONTAINER_CMD}" bash "${SCRIPT_DIR}/install-watchdog.sh"

"${CONTAINER_CMD}" exec agentteams-manager agt get workers pasay-lead -o json >/dev/null
"${CONTAINER_CMD}" exec agentteams-manager agt get workers pasay-builder -o json >/dev/null
"${CONTAINER_CMD}" exec agentteams-manager agt get teams pasay-engineering -o json >/dev/null

printf '%s\n' 'PASAY_AGENTTEAMS_READY'
printf '%s\n' 'Element chat: http://<GX10-IP>:18088'
printf '%s\n' 'Dashboard:    http://<GX10-IP>:13000'
