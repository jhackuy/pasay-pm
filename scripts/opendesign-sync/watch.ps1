param(
    [int]$DebounceMs = 2500,
    [int]$IntervalMs = 500
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 3
$ProgressPreference = 'SilentlyContinue'
$env:GIT_PAGER = 'cat'

$OPEN_DESIGN_SOURCE = "C:\Users\Admin\AppData\Roaming\Open Design\namespaces\release-stable-win\data\projects\c5fb3a39-c6d0-4003-9cee-66deb7a626a1"
$LOG_DIR            = (Join-Path $PSScriptRoot '..\..\.ai-control\logs\opendesign-sync')
$PID_FILE           = (Join-Path $LOG_DIR 'watcher.pid')
$SYNC_SCRIPT        = (Join-Path $PSScriptRoot 'sync.ps1')

$LOG_DIR = [System.IO.Path]::GetFullPath($LOG_DIR)
$PID_FILE = [System.IO.Path]::GetFullPath($PID_FILE)

New-Item -ItemType Directory -Force -Path $LOG_DIR | Out-Null
$logFile = Join-Path $LOG_DIR "watch-$(Get-Date -Format yyyyMMdd-HHmmss).log"

function Log($s) {
    $line = "[$(Get-Date -Format o)] $s"
    Write-Output $line
    Add-Content -Path $logFile -Value $line -Encoding UTF8
}

function Blocked($code, $detail = '') {
    Log "SYNC_BLOCKED_$code $detail"
    if ($code -eq 'WATCHER_ALIVE') { exit 6 }
    if ($code -eq 'SOURCE_MISSING') { exit 2 }
    exit 2
}

Log "=== WATCHER START ==="
Log "Source      : $OPEN_DESIGN_SOURCE"
Log "DebounceMs  : $DebounceMs"
Log "IntervalMs  : $IntervalMs"
Log "PID_FILE    : $PID_FILE"

if (-not (Test-Path -Path $OPEN_DESIGN_SOURCE)) {
    Blocked 'SOURCE_MISSING' "path not found: $OPEN_DESIGN_SOURCE"
}

if (Test-Path -Path $PID_FILE) {
    try {
        $existingPid = [int](Get-Content -Path $PID_FILE -Raw -Encoding UTF8).Trim()
        $proc = Get-Process -Id $existingPid -ErrorAction SilentlyContinue
        if ($proc -and $proc.Id -eq $existingPid) {
            Blocked 'WATCHER_ALIVE' "pid=$existingPid name=$($proc.ProcessName) file=$PID_FILE"
        } else {
            Log "Stale PID file found (pid=$existingPid not alive); removing"
            Remove-Item -Path $PID_FILE -Force -ErrorAction SilentlyContinue
        }
    } catch {
        Log "PID file parse error ($($_.Exception.Message); will overwrite"
        Remove-Item -Path $PID_FILE -Force -ErrorAction SilentlyContinue
    }
}

$PID | Out-File -Path $PID_FILE -Encoding UTF8 -NoNewline
Log "PID $PID written to $PID_FILE"

$script:lastEventAt = [DateTime]::MinValue
$script:dirty = $false
$script:syncCount = 0

function Invoke-Sync {
    $script:syncCount++
    $n = $script:syncCount
    Log "SYNC[$n] invoke: $SYNC_SCRIPT"
    try {
        & $SYNC_SCRIPT
        $code = $LASTEXITCODE
        Log "SYNC[$n] exit=$code"
    } catch {
        Log "SYNC[$n] EXCEPTION: $($_.Exception.Message)"
    }
}

Log "Initial catch-up sync..."
Invoke-Sync

$fsw = New-Object System.IO.FileSystemWatcher
$fsw.Path = $OPEN_DESIGN_SOURCE
$fsw.IncludeSubdirectories = $true
$fsw.NotifyFilter = [System.IO.NotifyFilters]::FileName -bor [System.IO.NotifyFilters]::DirectoryName -bor [System.IO.NotifyFilters]::LastWrite -bor [System.IO.NotifyFilters]::Size -bor [System.IO.NotifyFilters]::Attributes
$fsw.EnableRaisingEvents = $false

$onEvent = {
    param($sender, $e)
    $script:lastEventAt = [DateTime]::Now
    $script:dirty = $true
    $rel = $e.FullPath.Replace($OPEN_DESIGN_SOURCE, '')
    Log "FSW[$($e.ChangeType)] $rel"
}.GetNewClosure()

Register-ObjectEvent -InputObject $fsw -EventName Changed -Action $onEvent | Out-Null
Register-ObjectEvent -InputObject $fsw -EventName Created -Action $onEvent | Out-Null
Register-ObjectEvent -InputObject $fsw -EventName Renamed -Action $onEvent | Out-Null
Register-ObjectEvent -InputObject $fsw -EventName Deleted -Action $onEvent | Out-Null

$fsw.EnableRaisingEvents = $true
Log "FileSystemWatcher armed on $OPEN_DESIGN_SOURCE"

$timer = New-Object System.Timers.Timer
$timer.Interval = $IntervalMs
$timer.AutoReset = $true

$timerCallback = {
    param($sender, $e)
    if ($script:dirty) {
        $elapsed = ([DateTime]::Now - $script:lastEventAt).TotalMilliseconds
        if ($elapsed -ge $DebounceMs) {
            $script:dirty = $false
            Log "Debounce satisfied ($elapsed ms >= $DebounceMs ms); triggering sync"
            Invoke-Sync
        }
    }
}.GetNewClosure()

Register-ObjectEvent -InputObject $timer -EventName Elapsed -Action $timerCallback | Out-Null
$timer.Start()
Log "Timer started: interval=${IntervalMs}ms debounce=${DebounceMs}ms"

try {
    Log "Entering wait loop (Ctrl+C to exit)"
    while ($true) {
        Start-Sleep -Milliseconds 200
    }
} finally {
    Log "Shutting down..."
    $timer.Stop()
    $timer.Dispose()
    $fsw.EnableRaisingEvents = $false
    $fsw.Dispose()
    Remove-Item -Path $PID_FILE -Force -ErrorAction SilentlyContinue
    Log "PID file removed; watcher stopped"
}
