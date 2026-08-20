# install-scheduled-task.ps1
#
# Register a one-shot-per-trigger Windows Scheduled Task that invokes
# trae-nd-entry.ps1. Matching PASAY-TASK-010 Design Decisions §1 and §5,
# this is a SCHEDULED TRIGGER not a polling daemon: the script runs once,
# picks at most one issue, then EXITS. Owner controls trigger frequency.
#
# Default triggers:
#   - AtLogOn of current user
#   - Every 20 minutes while idle (Owner can change frequency via -RepeatMinutes)
#
# The task NEVER runs continuously; concurrent runs are prohibited by the
# bridge's own PID file + Windows Task Scheduler's "IgnoreNew" setting.

[CmdletBinding()]
param(
    [string]$TaskName = "Pasay TRAE Auto Wake",
    [int]$RepeatMinutes = 20,
    [ValidateSet("pull","http")][string]$Mode = "pull",
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptDir
$RepoRoot = Split-Path -Parent $RepoRoot
$EntryScript = Join-Path $ScriptDir "trae-nd-entry.ps1"

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "UNINSTALLED: $TaskName"
    exit 0
}

if (-not (Test-Path $EntryScript)) { throw "Missing: $EntryScript" }

$Action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument ("-NoProfile -ExecutionPolicy Bypass -NonInteractive -File `"$EntryScript`" -Mode $Mode") `
    -WorkingDirectory $RepoRoot

$Trigger1 = New-ScheduledTaskTrigger -AtLogOn
$Trigger2 = New-ScheduledTaskTrigger -Once -At (Get-Date).Date.AddMinutes(5) -RepetitionInterval (New-TimeSpan -Minutes $RepeatMinutes)

$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

$Settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -RestartCount 1 `
    -RestartInterval (New-TimeSpan -Minutes 5)

Register-ScheduledTask -TaskName $TaskName -TaskPath '\' `
    -Action $Action -Trigger @($Trigger1, $Trigger2) -Settings $Settings -Principal $Principal `
    -Description "PASAY-TASK-010 — one-shot GitHub → TRAE /ND bridge (never a daemon)." `
    -Force | Out-Null

Write-Host "INSTALLED: $TaskName"
Write-Host "  Mode      : $Mode"
Write-Host "  Repeat    : every $RepeatMinutes min (idle) + AtLogOn"
Write-Host "  Singleton : bridge PID file + IgnoreNew"
Write-Host ""
Write-Host "Owner post-install checklist:"
Write-Host "  1. Set `$env:GITHUB_TOKEN (classic, repo scope) at machine/user level (pull-mode)."
Write-Host "  2. [Optional] Set `$env:TRAE_ND_SINK_CMD to hand /ND <N> into your agent launcher."
Write-Host "  3. [Optional HTTP push] Set GitHub repo secrets TRAE_LOCAL_BRIDGE_URL / TRAE_LOCAL_BRIDGE_TOKEN."
Write-Host "  4. Verify once: powershell -File '$EntryScript' -Mode pull"
