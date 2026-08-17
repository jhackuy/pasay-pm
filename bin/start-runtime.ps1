# start-runtime.ps1 - canonical pasay runtime (re)start for the Windows node.
#
# This is the ONLY production start entry. It is a thin launcher: it validates
# the pinned runtime worktree, records the LIVE_RUNTIME_SHA proof, then hands
# ALL lifecycle ownership (exactly-one API / poller / worker, stale-PID
# recovery, concurrent-start protection, real readiness) to the canonical
# Python owner ``bin/pasay_runtime.py bootstrap``.
#
# Rationale (WINDOWS-RUNTIME-SINGLETON-PERSISTENCE-007B): the previous guard
# scanned ``Get-CimInstance Win32_Process.CommandLine`` to avoid duplicates, but
# on the canonical Windows node that call can be denied, so it silently treated
# every run as "nothing running" and spawned a second Telegram poller (the 409
# root cause). Singleton ownership is now enforced by atomic O_EXCL lock files
# in the owner — not by a process scan — so a duplicate bootstrap always
# converges to the same single runtime.
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File bin/start-runtime.ps1
$ErrorActionPreference = 'Stop'

$Repo     = 'D:\AI-Review\pasay-pm'
$RT       = Join-Path $Repo 'worktrees\BOT-V1-USABLE-001-RUNTIME'
$Runtime  = Join-Path $Repo '.runtime'
$AppPy    = Join-Path $Repo '.venv\Scripts\python.exe'
$Owner    = Join-Path $Repo 'bin\pasay_runtime.py'

if (-not (Test-Path -LiteralPath $RT)) { Write-Error "runtime worktree missing: $RT"; exit 1 }
if (-not (Test-Path -LiteralPath $AppPy)) { Write-Error "app venv missing: $AppPy"; exit 1 }
if (-not (Test-Path -LiteralPath $Owner)) { Write-Error "canonical owner missing: $Owner"; exit 1 }
New-Item -ItemType Directory -Force -Path $Runtime | Out-Null

# Load config from the runtime worktree .env into this process so the owner and
# the components resolve them even when launched from a clean context (Scheduled
# Task / Startup). Secrets are never printed.
$EnvFile = Join-Path $RT '.env'
if (-not (Test-Path -LiteralPath $EnvFile)) { $EnvFile = Join-Path $Repo '.env' }
if (Test-Path -LiteralPath $EnvFile) {
    Get-Content -LiteralPath $EnvFile | ForEach-Object {
        if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
            $k = $Matches[1]
            $v = $Matches[2].Trim().Trim('"')
            [Environment]::SetEnvironmentVariable($k, $v, 'Process')
        }
    }
}

# The runtime worktree HEAD is the LIVE_RUNTIME_SHA. Record it + refuse to boot
# from a dirty worktree (deployed tree must be exactly the pinned commit).
$head = (& git -C $RT rev-parse HEAD).Trim()
$statusLines = @(& git -C $RT status --porcelain)
if ($statusLines.Count -gt 0) {
    Write-Error "runtime worktree is not clean ($($statusLines.Count) lines); not starting."
    exit 1
}
Write-Output "runtime worktree HEAD=$head (LIVE_RUNTIME_SHA)"
$proof = @{ live_runtime_sha = $head; captured_at = (Get-Date -Format o) }
$proof | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $Runtime 'runtime-version-proof.json')

# Delegate ALL lifecycle ownership to the canonical Python owner. The owner is
# idempotent: if a live canonical runtime already owns the unit, this is a
# no-op (never spawns a duplicate poller / worker / API).
& $AppPy $Owner bootstrap
$exit = $LASTEXITCODE
Write-Output "canonical owner exit=$exit"
exit $exit
