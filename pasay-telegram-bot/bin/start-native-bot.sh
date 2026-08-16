#!/bin/bash
# DEPRECATED legacy macOS/launchd bot start entry — retired on the Windows node.
#
# The canonical (and only) runtime start path is bin/start-runtime.ps1, which
# idempotently brings up exactly ONE API + ONE bot poller + ONE operations
# worker. This script no longer starts pasay_bot.main so it cannot create a
# second @pasayhousebot polling consumer (the Telegram 409 Conflict root cause).
echo "DEPRECATED: start-native-bot.sh is retired. Use bin/start-runtime.ps1 (canonical)." >&2
exit 2
