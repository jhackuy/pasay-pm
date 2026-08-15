# start-runtime.ps1 - canonical pasay runtime (re)start for the Windows node.
#
# Starts (idempotently) exactly ONE of each:
#   - Backend API            (uvicorn app.main:app on 127.0.0.1:8001)
#   - Telegram bot poller    (pasay_bot.main)
#   - Operations worker      (bin/run-operations-worker.py --interval 60)
# all from the pinned runtime worktree, so a normal deploy/restart brings the
# whole probe-up set back up together and never spawns a duplicate worker.
#
# Fail-closed:
#   - refuses to run from a dirty worktree (deployed tree must be the pinned commit)
#   - idempotent: never starts a second API / bot / worker (skips any already live),
#     so there is always exactly one of each.
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File bin/start-runtime.ps1
$ErrorActionPreference = 'Stop'

$Repo     = 'D:\AI-Review\pasay-pm'
$RT       = Join-Path $Repo 'worktrees\BOT-V1-USABLE-001-RUNTIME'
$Runtime  = Join-Path $Repo '.runtime'
$AppPy    = Join-Path $Repo '.venv\Scripts\python.exe'
$BotPy    = Join-Path $Repo 'pasay-telegram-bot\.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $RT)) { Write-Error "runtime worktree missing: $RT"; exit 1 }
if (-not (Test-Path -LiteralPath $AppPy)) { Write-Error "app venv missing: $AppPy"; exit 1 }
if (-not (Test-Path -LiteralPath $BotPy)) { Write-Error "bot venv missing: $BotPy"; exit 1 }
New-Item -ItemType Directory -Force -Path $Runtime | Out-Null

# The runtime worktree HEAD is the LIVE_RUNTIME_SHA. The deploy step checks it
# out to the final commit; this script records it and refuses to start from a
# dirty worktree (deployed tree must be exactly the pinned commit).
$head = (& git -C $RT rev-parse HEAD).Trim()
$statusLines = @(& git -C $RT status --porcelain)
if ($statusLines.Count -gt 0) {
    Write-Error "runtime worktree is not clean ($($statusLines.Count) lines); not starting."
    exit 1
}
Write-Output "runtime worktree HEAD=$head (LIVE_RUNTIME_SHA)"
$proof = @{ live_runtime_sha = $head; captured_at = (Get-Date -Format o) }
$proof | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $Runtime 'runtime-version-proof.json')

function Test-ProcessAlive([int]$ProcessId) {
    if (-not $ProcessId -or -not (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) { return $false }
    return $true
}
function Get-PidFile([string]$Name) {
    $f = Join-Path $Runtime $Name
    if (Test-Path -LiteralPath $f) { return ([int](Get-Content $f -Raw -ErrorAction SilentlyContinue).Trim()) }
    return 0
}

# --- determine already-running services to stay idempotent (never two of one kind) ---------
# Authoritative no-duplicate guard: match by live command line (robust even when
# a prior instance was launched without a pid file), falling back to pid files.
$running = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.Name -match 'python' })
function Have-Cmd([string]$re) {
    foreach ($r in $running) { if ($r.CommandLine -match $re) { return $true } }
    return $false
}

# API: nothing else may already listen on 8001.
$existingApi = Get-NetTCPConnection -LocalPort 8001 -State Listen -ErrorAction SilentlyContinue
if (($existingApi)) {
    Write-Output "API already running (listener pid=$($existingApi[0].OwningProcess)); skipping."
    $apiStarted = $false
} else {
    $apiStarted = $true
}

# Bot: skip if a pasay_bot.main poller is already alive.
$botStarted = -not (Have-Cmd 'pasay_bot\.main')

# Worker: skip if an operations worker loop is already alive (idempotent lone worker).
$workerStarted = -not (Have-Cmd 'run-operations-worker\.py')

# --- start missing services -----------------------------------------------------------------
if ($apiStarted) {
    $out = Join-Path $Runtime 'api_runtime.log'
    $err = Join-Path $Runtime 'api_runtime.log.err'
    $p = Start-Process -FilePath $AppPy `
        -ArgumentList '-m','uvicorn','app.main:app','--host','127.0.0.1','--port','8001' `
        -WorkingDirectory $RT -RedirectStandardOutput $out -RedirectStandardError $err `
        -WindowStyle Hidden -PassThru
    Set-Content -LiteralPath (Join-Path $Runtime 'api_runtime.pid') -Value $p.Id
    Write-Output "api started pid=$($p.Id)"
}

if ($botStarted) {
    $out = Join-Path $Runtime 'bot_runtime.log'
    $err = Join-Path $Runtime 'bot_runtime.log.err'
    $env:PYTHONPATH = "$RT\pasay-telegram-bot"
    $p = Start-Process -FilePath $BotPy `
        -ArgumentList '-u','-m','pasay_bot.main' `
        -WorkingDirectory (Join-Path $RT 'pasay-telegram-bot') `
        -RedirectStandardOutput $out -RedirectStandardError $err `
        -WindowStyle Hidden -PassThru
    Set-Content -LiteralPath (Join-Path $Runtime 'bot_runtime.pid') -Value $p.Id
    Write-Output "bot started pid=$($p.Id)"
}

if ($workerStarted) {
    $out = Join-Path $Runtime 'worker_runtime.log'
    $err = Join-Path $Runtime 'worker_runtime.log.err'
    $p = Start-Process -FilePath $AppPy `
        -ArgumentList (Join-Path $RT 'bin\run-operations-worker.py'), '--interval','60' `
        -WorkingDirectory $RT -RedirectStandardOutput $out -RedirectStandardError $err `
        -WindowStyle Hidden -PassThru
    Set-Content -LiteralPath (Join-Path $Runtime 'worker_runtime.pid') -Value $p.Id
    Write-Output "worker started pid=$($p.Id)"
}

# --- verify ---------------------------------------------------------------------------------
Start-Sleep -Seconds 6
$apiNow = Get-NetTCPConnection -LocalPort 8001 -State Listen -ErrorAction SilentlyContinue
$wPid = Get-PidFile 'worker_runtime.pid'
Write-Output ""
Write-Output "--- verify ---"
Write-Output ("API  listener : " + $(if ($apiNow) { "pid=$($apiNow[0].OwningProcess)" } else { 'NONE' }))
Write-Output ("WORKER pid    : " + $(if (Test-ProcessAlive $wPid) { "$wPid (alive)" } else { 'NONE' }))
$botSt = Get-PidFile 'bot_runtime.pid'
Write-Output ("BOT   pid file: " + $(if ($botSt) { "$botSt" } else { 'none' }))
Write-Output "--- worker_runtime.log tail ---"
Get-Content (Join-Path $Runtime 'worker_runtime.log') -Tail 10 -ErrorAction SilentlyContinue
Write-Output "--- worker_runtime.log.err tail ---"
Get-Content (Join-Path $Runtime 'worker_runtime.log.err') -Tail 10 -ErrorAction SilentlyContinue
