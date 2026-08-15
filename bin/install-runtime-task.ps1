# install-runtime-task.ps1 - register the durable Pasay runtime autostart task.
#
# The one NORMAL deploy/restart path for the whole Pasay runtime on this node.
# At logon (and on any manual run) it invokes the canonical runtime starter
# bin/start-runtime.ps1, which idempotently brings up exactly ONE API + ONE bot
# poller + ONE operations worker (no duplicate worker, no extra manual command).
#
# Mirrors the existing "DeepSeek Harness Autostart" scheduled-task pattern.
param()

$ErrorActionPreference = 'Stop'
$TASK_NAME = 'Pasay Runtime Autostart'
$WORKSPACE = 'D:\AI-Review\pasay-pm'
$STARTER   = Join-Path $WORKSPACE 'bin\start-runtime.ps1'

if (-not (Test-Path -LiteralPath $STARTER)) {
    throw "runtime starter not found: $STARTER"
}

$Action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument ('-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}"' -f $STARTER) `
    -WorkingDirectory $WORKSPACE

$Trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$Trigger.Delay = 'PT20S'

$Settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable

$Principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TASK_NAME -Action $Action -Trigger $Trigger -Settings $Settings -Principal $Principal -Force | Out-Null
Write-Output "TASK_REGISTERED: $TASK_NAME -> $STARTER"
