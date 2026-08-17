# install-runtime-task.ps1 - register the durable Pasay runtime autostart task.
#
# The ONE normal deploy/restart path for the whole Pasay runtime on this node.
# At logon (and on any manual run) it invokes the canonical runtime starter
# bin/start-runtime.ps1, which idempotently brings up exactly ONE API + ONE bot
# poller + ONE operations worker (no duplicate worker, no extra manual command).
#
# Correct chain (007D):
#     Windows Scheduled Task ("Pasay Runtime Autostart")
#         -> powershell -File bin/start-runtime.ps1
#             -> .venv python bin/pasay_runtime.py bootstrap   (canonical owner)
# The task NEVER launches uvicorn / pasay_bot / worker directly.
#
# MultipleInstances=IgnoreNew guarantees a duplicate task launch (e.g. logon +
# manual run) can never spawn a second runtime unit.
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File bin/install-runtime-task.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File bin/install-runtime-task.ps1 -Verify
#   powershell -NoProfile -ExecutionPolicy Bypass -File bin/install-runtime-task.ps1 -Unregister
param(
    [switch]$Verify,
    [switch]$Unregister
)

$ErrorActionPreference = 'Stop'
$TASK_NAME = 'Pasay Runtime Autostart'
$WORKSPACE = 'D:\AI-Review\pasay-pm'
$STARTER   = Join-Path $WORKSPACE 'bin\start-runtime.ps1'
$RUNTIME   = Join-Path $WORKSPACE '.runtime'
$DEF_JSON  = Join-Path $RUNTIME 'task-definition-pasay-runtime-autostart.json'

if (-not (Test-Path -LiteralPath $STARTER)) {
    throw "runtime starter not found: $STARTER"
}
New-Item -ItemType Directory -Force -Path $RUNTIME | Out-Null

if ($Unregister) {
    Unregister-ScheduledTask -TaskName $TASK_NAME -TaskPath '\' -Confirm:$false -ErrorAction SilentlyContinue
    Write-Output "UNREGISTERED: $TASK_NAME (if it existed)"
    exit 0
}

if ($Verify) {
    # Dump the REAL registered definition (requires Scheduled-Task store access).
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
        Write-Output "The scheduled-task store is not readable from this context (e.g. a DSH sandbox)."
        Write-Output "Run this from an interactive/elevated PowerShell:"
        Write-Output "  powershell -NoProfile -ExecutionPolicy Bypass -File $($MyInvocation.MyCommand.Path) -Verify"
        exit 2
    }
    exit 0
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

Register-ScheduledTask -TaskName $TASK_NAME -TaskPath '\' -Action $Action -Trigger $Trigger -Settings $Settings -Principal $Principal -Force | Out-Null

# Record the intended definition on disk (readable even where the task store is
# not) so the persistence contract is auditable.
$snapshot = [ordered]@{
    task_name    = $TASK_NAME
    execute      = 'powershell.exe'
    arguments    = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}"' -f $STARTER
    working_dir  = $WORKSPACE
    chain        = @(
        'Windows Scheduled Task -> bin/start-runtime.ps1 -> bin/pasay_runtime.py bootstrap (canonical owner)',
        'NEVER: Scheduled Task -> uvicorn / pasay_bot / worker directly'
    )
    trigger      = 'AtLogOn + PT20S delay'
    multiple_instances = 'IgnoreNew'
    run_level    = 'Limited'
    logon_type   = 'Interactive'
    registered_at = (Get-Date -Format o)
}
$snapshot | ConvertTo-Json | Set-Content -LiteralPath $DEF_JSON -Encoding UTF8
Write-Output "TASK_REGISTERED: $TASK_NAME -> $STARTER"
Write-Output "DEFINITION_SNAPSHOT: $DEF_JSON"
Write-Output "Verify with: powershell -NoProfile -ExecutionPolicy Bypass -File $($MyInvocation.MyCommand.Path) -Verify"
