$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 3
$ProgressPreference = 'SilentlyContinue'
$env:GIT_PAGER = 'cat'

$LOG_DIR    = (Join-Path $PSScriptRoot '..\..\.ai-control\logs\opendesign-sync')
$PID_FILE   = (Join-Path $LOG_DIR 'watcher.pid')
$INSTALLER = Join-Path $PSScriptRoot 'install-task.ps1'

& $INSTALLER -Unregister

if (Test-Path -Path $PID_FILE) {
    try {
        $pidVal = (Get-Content -Path $PID_FILE -Raw -Encoding UTF8).Trim()
        if ($pidVal) {
            $proc = Get-Process -Id ([int]$pidVal) -ErrorAction SilentlyContinue
            if ($proc) {
                Write-Output "Stopping existing watcher PID=$pidVal ($($proc.ProcessName))"
                Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
            }
        }
    } catch {
        Write-Output "PID file cleanup note: $($_.Exception.Message)"
    }
    Remove-Item -Path $PID_FILE -Force -ErrorAction SilentlyContinue
    Write-Output "PID file removed: $PID_FILE"
}

Write-Output "UNINSTALLED"
