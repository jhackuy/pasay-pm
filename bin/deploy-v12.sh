#!/bin/bash
# Deploy V1.2 dev tree -> /opt/pasay-pm (production native copy).
# Sync source only (no venv, state db, uploads, backups, git, caches).
# NOTE: bin/start-native-api.sh is the CANONICAL production wrapper in /opt and
# the dev tree only holds a shim that forwards to it. NEVER overwrite /opt's
# canonical wrapper with the dev shim (would cause infinite recursion). Excluded.
set -euo pipefail
DEV=/Users/jhackuy/Documents/Codex/pasay-pm
PROD=/opt/pasay-pm

echo "[deploy] syncing ${DEV} -> ${PROD}"
rsync -a --delete \
  --exclude='.venv' --exclude='.git' --exclude='__pycache__' \
  --exclude='.pytest_cache' --exclude='uploads' --exclude='backups' \
  --exclude='*.pyc' --exclude='.env' \
  --exclude='state/' --exclude='*.db' --exclude='*.db-journal' \
  --exclude='bin/start-native-api.sh' \
  --exclude='bin/deploy-v12.sh' \
  "${DEV}/" "${PROD}/"
echo "[deploy] source sync done (canonical bin/start-native-api.sh preserved in /opt)"

echo "[deploy] alembic upgrade head (backend)"
cd "${PROD}" && .venv/bin/python -m alembic upgrade head

echo "[deploy] done. Restart services as root:"
echo "  sudo launchctl kickstart -k system/ai.pasay.api"
echo "  sudo launchctl kickstart -k system/ai.pasay.telegram-bot"
