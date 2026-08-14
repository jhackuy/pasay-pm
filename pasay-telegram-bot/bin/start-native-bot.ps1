# Native start wrapper for pasay-telegram-bot on Windows (Scheduled Task /
# Startup folder). Mirrors start-native-bot.sh fail-closed style:
#   - loads .env WITHOUT printing secrets
#   - ensures STATE_DB parent dir (bot migrates schema on start)
#   - getMe self-check via --dry-run (FAIL-CLOSED: never polls on bad token)
#   - starts polling hidden with logs under <repo>\.runtime
$ErrorActionPreference = 'Stop'
$Project = 'D:\AI-Review\pasay-pm\pasay-telegram-bot'
$VenvPy = Join-Path $Project '.venv\Scripts\python.exe'
$EnvFile = Join-Path $Project '.env'
if (-not (Test-Path -LiteralPath $EnvFile)) {
    $EnvFile = 'D:\AI-Review\pasay-pm\.env'
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
$out = 'D:\AI-Review\pasay-pm\.runtime\bot_native.out.log'
$err = 'D:\AI-Review\pasay-pm\.runtime\bot_native.err.log'
$p = Start-Process -FilePath $VenvPy -ArgumentList @('-u', '-m', 'pasay_bot.main') `
    -WorkingDirectory $Project -WindowStyle Hidden `
    -RedirectStandardOutput $out -RedirectStandardError $err -PassThru
Write-Output "[pasay-bot] polling started (pid $($p.Id))"
