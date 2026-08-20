param(
    [switch]$Verify,
    [switch]$Unregister
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 3
$ProgressPreference = 'SilentlyContinue'
$env:GIT_PAGER = 'cat'

$TASK_NAME = 'Pasay OpenDesign Sync Watcher'
$WORKSPACE = Split-Path $PSScriptRoot -Parent | Split-Path -Parent
$WORKSPACE = [System.IO.Path]::GetFullPath($WORKSPACE)
$WATCHER   = Join-Path $WORKSPACE 'scripts\opendesign-sync\watch.ps1'
$LOG_DIR   = Join-Path $WORKSPACE '.ai-control\logs\opendesign-sync'
$DEF_JSON  = Join-Path $LOG_DIR 'task-definition.json'

if (-not (Test-Path -Path $WATCHER)) {
    throw "watcher script not found: $WATCHER"
}
New-Item -ItemType Directory -Force -Path $LOG_DIR | Out-Null

if ($Unregister) {
    Unregister-ScheduledTask -TaskName $TASK_NAME -TaskPath '\' -Confirm:$false -ErrorAction SilentlyContinue
    Write-Output "UNREGISTERED: $TASK_NAME (if it existed)"
    exit 0
}

if ($Verify) {
    try {
        $t = Get-ScheduledTask -TaskName $TASK_NAME -TaskPath '\'
        $xml = Export-ScheduledTask -TaskName $TASK_NAME -TaskPath '\'
        Write-Output "TASK_FOUND: $TASK_NAME"
        Write-Output "  State           : $($t.State)"
        Write-Output "  Action Exec     : $($t.Actions[0].Execute)"
        Write-Output "  Action Args     : $($t.Actions[0].Arguments)"
        Write-Output "  Action WorkDir  : $($t.Actions[0].WorkingDirectory)"
        for ($i = 0; $i -lt $t.Triggers.Count; $i++) {
            $tr = $t.Triggers[$i]
            Write-Output ("  Trigger[{0}]     : CIMClass={1} Enabled={2}" -f $i, $tr.CimClass.CimClassName, $tr.Enabled)
        }
        Write-Output "  Principal User  : $($t.Principal.UserId)  RunLevel=$($t.Principal.RunLevel) LogonType=$($t.Principal.LogonType)"
        Write-Output "  Settings Multi  : $($t.Settings.MultipleInstances)  RestartCount=$($t.Settings.RestartCount)  StartWhenAvailable=$($t.Settings.StartWhenAvailable)"
        Write-Output "--- exported XML ---"
        Write-Output $xml
    } catch {
        Write-Output "VERIFY_FAILED: $($_.Exception.Message)"
        Write-Output "The scheduled-task store is not readable from this context."
        Write-Output "Run this from an interactive/elevated PowerShell:"
        Write-Output "  powershell -NoProfile -ExecutionPolicy Bypass -File `"$($MyInvocation.MyCommand.Path)`" -Verify"
        exit 2
    }
    exit 0
}

$Action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument ('-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}"' -f $WATCHER) `
    -WorkingDirectory $WORKSPACE

$Trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$Trigger.Delay = 'PT30S'

$Settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable

$Principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TASK_NAME -TaskPath '\' -Action $Action -Trigger $Trigger -Settings $Settings -Principal $Principal -Force | Out-Null

$snapshot = [ordered]@{
    task_name          = $TASK_NAME
    execute            = 'powershell.exe'
    arguments          = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}"' -f $WATCHER
    working_dir        = $WORKSPACE
    watcher_script     = $WATCHER
    chain              = @(
        'Windows Scheduled Task (AtLogOn PT30S delay) -> scripts/opendesign-sync/watch.ps1',
        '  watch.ps1 -> FileSystemWatcher + debounce -> scripts/opendesign-sync/sync.ps1',
        '    sync.ps1 -> allowlist copy -> gates-runner.js -> git commit -> push opendesign/live'
    )
    trigger            = 'AtLogOn + PT30S delay'
    user               = "$env:USERDOMAIN\$env:USERNAME"
    multiple_instances = 'IgnoreNew'
    restart_count      = 3
    restart_interval   = 'PT1M'
    execution_limit    = 'PT0S (unlimited)'
    run_level          = 'Limited'
    logon_type         = 'Interactive'
    registered_at      = (Get-Date -Format o)
}
$snapshot | ConvertTo-Json -Depth 4 | Set-Content -Path $DEF_JSON -Encoding UTF8
Write-Output "TASK_REGISTERED: $TASK_NAME -> $WATCHER"
Write-Output "DEFINITION_SNAPSHOT: $DEF_JSON"
Write-Output "Verify with: powershell -NoProfile -ExecutionPolicy Bypass -File `"$($MyInvocation.MyCommand.Path)`" -Verify"
