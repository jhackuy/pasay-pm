# trae-nd-entry.ps1
#
# Invokes the Python bridge once and, if it emits a CANONICAL_ND_CMD, hands
# it to TRAE. THIS SCRIPT NEVER LOOPS — one-shot per call, matching the
# PASAY-TASK-010 "one event → one issue → one /ND" contract.
#
# Hand-off paths, in order of preference (all are non-UI, process-based):
#   1. $env:TRAE_ND_SINK_CMD   — Owner-provided shell command template;
#                                 placeholder {ISSUE_NUMBER} / {ND_CMD} replaced.
#                                 e.g. "& 'C:\path\to\my-agent.exe' issue={ISSUE_NUMBER}"
#   2. Direct stdout+exit      — Scheduled Task wrapper or manual invocation
#                                 captures CANONICAL_ND_CMD from stdout.
#   3. Event drop file         — `CANONICAL_ND_CMD` written to
#                                 $TRAE_AUTO_WAKE_CONTROL/nd_entry.txt.
#                                 Any existing TRAE session polling loopback
#                                 (NOT GitHub polling) can consume it.
#
# We explicitly do NOT simulate UI input. TRAE today offers no stable public
# "execute custom command in existing workspace" CLI, so option 2 / 3 + the
# Owner's explicit `TRAE_ND_SINK_CMD` bridge are the only supported routes.
# This is the BLOCKED boundary reported by PASAY-TASK-010.

[CmdletBinding()]
param(
    [ValidateSet("pull","http","check")]
    [string]$Mode = "pull",
    [int]$Issue = 0,
    [switch]$ServerMode
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$RepoRoot = Split-Path -Parent $RepoRoot
$PyExe = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$Bridge = Join-Path $PSScriptRoot "trae_auto_wake_bridge.py"
$ControlDir = if ($env:TRAE_AUTO_WAKE_CONTROL) { $env:TRAE_AUTO_WAKE_CONTROL } else { Join-Path $RepoRoot ".ai-control\trae-auto-wake" }
$NdEntryFile = Join-Path $ControlDir "nd_entry.txt"

if (-not (Test-Path $PyExe)) {
    # Fallback to system python3 on PATH
    $PyExe = Get-Command python -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Source
    if (-not $PyExe) { $PyExe = Get-Command python3 -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Source }
    if (-not $PyExe) { Write-Host "ERR: NO_PYTHON"; exit 3 }
}

New-Item -ItemType Directory -Force -Path $ControlDir | Out-Null

if ($Mode -eq "http") {
    if ($ServerMode) { $cmdArgs = @($Bridge, "http", "--server-mode") } else { $cmdArgs = @($Bridge, "http") }
} elseif ($Mode -eq "check") {
    $cmdArgs = @($Bridge, "check")
} else {
    $cmdArgs = @($Bridge, "pull")
    if ($Issue -gt 0) { $cmdArgs += @("--issue", $Issue) }
}

Write-Host "BRIDGE_CMD: $PyExe $($cmdArgs -join ' ')"

$proc = Start-Process -FilePath $PyExe -ArgumentList $cmdArgs `
    -NoNewWindow -Wait -PassThru `
    -RedirectStandardOutput (Join-Path $ControlDir "bridge.stdout.log") `
    -RedirectStandardError  (Join-Path $ControlDir "bridge.stderr.log")

$stdout = Get-Content (Join-Path $ControlDir "bridge.stdout.log") -Raw -ErrorAction SilentlyContinue
$stderr = Get-Content (Join-Path $ControlDir "bridge.stderr.log") -Raw -ErrorAction SilentlyContinue
Write-Host $stdout
if ($stderr) { Write-Host "---STDERR---"; Write-Host $stderr }

$canonical = ($stdout -split "`n") | Where-Object { $_ -match '^CANONICAL_ND_CMD:\s*(.+)$' } | Select-Object -First 1
if (-not $canonical) {
    Write-Host "NO_CANONICAL_ND_CMD: bridge did not issue a new /ND (status above)."
    exit $proc.ExitCode
}
$ndCmd = $Matches[1]
$issueNum = $null
if ($ndCmd -match '/ND\s+(\d+)') { $issueNum = $Matches[1] }

# --- drop file sink ---
Set-Content -Path $NdEntryFile -Value "$ndCmd`nissue_number=$issueNum`nts=$([DateTimeOffset]::UtcNow.ToUnixTimeSeconds())" -NoNewline:$false
Write-Host "SINK_DROP_FILE: $NdEntryFile"

# --- custom sink cmd ---
if ($env:TRAE_ND_SINK_CMD) {
    $expanded = $env:TRAE_ND_SINK_CMD
    if ($issueNum) { $expanded = $expanded -replace '\{ISSUE_NUMBER\}', $issueNum }
    $expanded = $expanded -replace '\{ND_CMD\}', [Management.Automation.Language.CodeGeneration]::EscapeSingleQuotedStringContent($ndCmd)
    Write-Host "SINK_CUSTOM_CMD: $expanded"
    Invoke-Expression $expanded | Out-Host
}

Write-Host "HANDOFF_MODE_ENTRY_POINT: PASAY-TASK-010 bridge complete."
Write-Host "TRAE_CAPABILITY_BOUNDARY: No stable public TRAE custom-command CLI detected."
Write-Host "  -> TRAE process may consume nd_entry.txt via loopback watch if enabled."
Write-Host "  -> Owner explicit TRAE_ND_SINK_CMD overrides are respected."
Write-Host "  -> THIS SCRIPT EXITS NOW; never chains to next issue."
exit 0
