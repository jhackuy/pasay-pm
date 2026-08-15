# start-native-bot.ps1 - delegate the legacy bot-start path to the canonical
# runtime starter (bin/start-runtime.ps1).
#
# Previously this wrapper unconditionally Start-Process'd a second pasay_bot.main
# poller. That collided with the durable "Pasay Runtime Autostart" scheduled task
# (and the old Startup Pasay_Native_Bot.vbs): if the runtime had already started
# the bot, this wrapper created a DUPLICATE poller and Telegram fired a 409
# getUpdates Conflict. The legacy bot-only autostart is now REDUNDANT.
#
# New behavior (smallest fix, same layout):
#   - loads .env WITHOUT printing secrets
#   - ensures STATE_DB parent dir (bot migrates schema on start)
#   - getMe self-check via --dry-run (FAIL-CLOSED: never proceeds on bad token)
#   - then DELEGATES to bin/start-runtime.ps1, which idempotently brings up
#     exactly ONE API + ONE bot poller + ONE operations worker (skips anything
#     already live), so a manual/legacy invocation can never race a duplicate.
$ErrorActionPreference = 'Stop'
$Project = "D:\AI-Review\pasay-pm\pasay-telegram-bot"
$Repo    = "D:\AI-Review\pasay-pm"
$VenvPy  = Join-Path $Project '.venv\Scripts\python.exe'
$EnvFile = Join-Path $Project '.env'
if (-not (Test-Path -LiteralPath $EnvFile)) {
    $EnvFile = Join-Path $Repo '.env'
}
if (Test-Path -LiteralPath $EnvFile) {
    Get-Content -LiteralPath $EnvFile | ForEach-Object {
        if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
            $k = $Matches[1]
            $v = $Matches[2].Trim().Trim('"')
            [Environment]::SetEnvironmentVariable($k, $v, 'Process')
        }
    }
}
$StateDb = if ($env:STATE_DB) { $env:STATE_DB } else { Join-Path $Project 'state\bot_state.db' }
$stateDir = Split-Path -Parent $StateDb
New-Item -ItemType Directory -Force -Path $stateDir | Out-Null
Set-Location $Project

Write-Output "[pasay-bot] state db ready: $StateDb"
Write-Output "[pasay-bot] running getMe self-check ..."
& $VenvPy -m pasay_bot.main --dry-run
if ($LASTEXITCODE -ne 0) {
    Write-Error "[pasay-bot] getMe self-check FAILED; not starting polling."
    exit 1
}
Write-Output "[pasay-bot] getMe self-check OK."

# Delegate to the canonical idempotent runtime starter (API + bot + worker).
$Starter = Join-Path $Repo 'bin\start-runtime.ps1'
if (-not (Test-Path -LiteralPath $Starter)) {
    Write-Error "canonical runtime starter missing: $Starter"
    exit 1
}
Write-Output "[pasay-bot] delegating to canonical runtime starter: $Starter"
& powershell.exe -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File $Starter
exit $LASTEXITCODE
