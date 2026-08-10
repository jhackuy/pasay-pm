#!/bin/bash
# Native start wrapper for pasay-telegram-bot, managed by launchd (system domain).
# Mirrors start-native-api.sh fail-closed style:
#   - set -e; loads .env WITHOUT printing secrets
#   - ensures STATE_DB parent dir + schema
#   - getMe self-check via --dry-run (FAIL-CLOSED: never starts polling on bad token)
#   - exec python -m pasay_bot.main
set -e

PROJECT=/opt/pasay-pm/pasay-telegram-bot
VENV_PY="$PROJECT/.venv/bin/python"
ENV_FILE="$PROJECT/.env"
if [ ! -f "$ENV_FILE" ]; then
  ENV_FILE=/opt/pasay-pm/.env
fi

# Load bot .env into this shell's environment (never print secrets).
set -a
# shellcheck disable=SC1090
[ -f "$ENV_FILE" ] && . "$ENV_FILE"
set +a

# State DB must be writable; create parent dir before anything else.
STATE_DB="${STATE_DB:-/opt/pasay-pm/pasay-telegram-bot/state/bot_state.db}"
mkdir -p "$(dirname "$STATE_DB")"
echo "[pasay-bot] state db ready: $STATE_DB"

cd "$PROJECT"

# getMe self-check + schema migration (--dry-run). FAIL-CLOSED: if the token is
# invalid or conflicts with another consumer, we never start polling.
echo "[pasay-bot] running getMe self-check ..."
"$VENV_PY" -m pasay_bot.main --dry-run
echo "[pasay-bot] getMe self-check OK."

# exec so launchd tracks the real python process.
exec "$VENV_PY" -m pasay_bot.main
