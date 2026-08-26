#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${HOME}/.local/share/pasay-agentteams"
UNIT_DIR="${HOME}/.config/systemd/user"
WATCHDOG_CONTAINER_CMD="${PASAY_WATCHDOG_CONTAINER_CMD:-docker}"

case "${WATCHDOG_CONTAINER_CMD}" in
  docker|podman) ;;
  *)
    printf 'PASAY_WATCHDOG_BLOCKED: unsupported container command %s\n' \
      "${WATCHDOG_CONTAINER_CMD}" >&2
    exit 1
    ;;
esac

command -v systemctl >/dev/null 2>&1 || {
  printf '%s\n' 'PASAY_WATCHDOG_BLOCKED: systemd is required' >&2
  exit 1
}

install -d -m 0755 "${INSTALL_DIR}" "${UNIT_DIR}"
install -m 0755 "${SCRIPT_DIR}/watchdog.py" "${INSTALL_DIR}/watchdog.py"
install -m 0755 "${SCRIPT_DIR}/resume-project.sh" "${INSTALL_DIR}/resume-project.sh"

sed "s/PASAY_WATCHDOG_CONTAINER_CMD=docker/PASAY_WATCHDOG_CONTAINER_CMD=${WATCHDOG_CONTAINER_CMD}/" \
  "${SCRIPT_DIR}/systemd/pasay-agentteams-watchdog.service" \
  > "${UNIT_DIR}/pasay-agentteams-watchdog.service"
chmod 0644 "${UNIT_DIR}/pasay-agentteams-watchdog.service"
install -m 0644 "${SCRIPT_DIR}/systemd/pasay-agentteams-watchdog.timer" \
  "${UNIT_DIR}/pasay-agentteams-watchdog.timer"

systemctl --user daemon-reload
systemctl --user enable --now pasay-agentteams-watchdog.timer
systemctl --user start pasay-agentteams-watchdog.service
systemctl --user is-active pasay-agentteams-watchdog.timer >/dev/null

printf '%s\n' 'PASAY_WATCHDOG_READY'
