param(
    [switch]$DryRun,
    [switch]$Quiet
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 3
$ProgressPreference = 'SilentlyContinue'
$env:GIT_PAGER = 'cat'

$OPEN_DESIGN_SOURCE = "C:\Users\Admin\AppData\Roaming\Open Design\namespaces\release-stable-win\data\projects\c5fb3a39-c6d0-4003-9cee-66deb7a626a1"
$GH_REPO_URL       = "https://github.com/jhackuy/pasay-opendesign.git"
$GH_UPSTREAM       = "origin"
$LIVE_BRANCH       = "opendesign/live"
$MAIN_BRANCH       = "main"
$MIRROR_DIR        = (Join-Path $PSScriptRoot '..\..\.ai-control\opendesign-mirror')
$LOG_DIR           = (Join-Path $PSScriptRoot '..\..\.ai-control\logs\opendesign-sync')

$ALLOWLIST = @(
    'index.html','pasay-design-system.html','pasay-mini-app.html','pasay-telegram-bot.html',
    'gates-runner.js','deepseek.svg','minimax.svg','pasay-mini-app-preview.png'
)

$FORBIDDEN_PATTERN = '\.git\\|^\.env|secrets?|token|password|credentials|conversations|logs?\\|database|runtime|namespace|\.zip$|\.bak$|cache|screenshots'

$MIRROR_DIR = [System.IO.Path]::GetFullPath($MIRROR_DIR)
$LOG_DIR = [System.IO.Path]::GetFullPath($LOG_DIR)

New-Item -ItemType Directory -Force -Path $LOG_DIR | Out-Null
$logFile = Join-Path $LOG_DIR "sync-$(Get-Date -Format yyyyMMdd-HHmmss).log"

function Log($s) {
    $line = "[$(Get-Date -Format o)] $s"
    if (-not $Quiet) { Write-Output $line }
    Add-Content -Path $logFile -Value $line -Encoding UTF8
}

function Blocked($code, $detail = '') {
    Log "SYNC_BLOCKED_$code $detail"
    switch ($code) {
        'SOURCE_MISSING'      { exit 2 }
        'SOURCE_MISMATCH'       { exit 2 }
        'MIRROR_INIT'         { exit 2 }
        'NON_FAST_FORWARD'    { exit 2 }
        'FORBIDDEN_FILE'      { exit 2 }
        'SECRET_SUSPECT'      { exit 2 }
        'GATE_FAILED'         { exit 3 }
        'DIRTY_WHITESPACE'    { exit 4 }
        'PUSH_FAILED'         { exit 5 }
        default               { exit 2 }
    }
}

Log "=== SYNC START ==="
Log "Source : $OPEN_DESIGN_SOURCE"
Log "Mirror : $MIRROR_DIR"
Log "Branch : $LIVE_BRANCH"
Log "DryRun : $DryRun"

if (-not (Test-Path -Path $OPEN_DESIGN_SOURCE)) {
    Blocked 'SOURCE_MISSING' "path not found: $OPEN_DESIGN_SOURCE"
}

$resolvedSource = (Resolve-Path -Path $OPEN_DESIGN_SOURCE).Path
if ($resolvedSource -ne $OPEN_DESIGN_SOURCE) {
    Blocked 'SOURCE_MISMATCH' "resolved=$resolvedSource expected=$OPEN_DESIGN_SOURCE"
}

if (-not (Test-Path -Path $MIRROR_DIR)) {
    Log "Mirror dir missing; cloning from $GH_REPO_URL"
    git --no-pager clone $GH_REPO_URL $MIRROR_DIR
    if ($LASTEXITCODE -ne 0) {
        Blocked 'MIRROR_INIT' "git clone failed"
    }
}

Set-Location -Path $MIRROR_DIR
$remoteOk = $false
$remotes = git --no-pager remote -v
foreach ($line in $remotes) {
    if ($line -match "^origin\s+\S+") {
        $remoteOk = $true
        break
    }
}
if (-not $remoteOk) {
    git --no-pager remote set-url origin $GH_REPO_URL
}

Log "Fetching origin..."
git --no-pager fetch --no-tags origin
if ($LASTEXITCODE -ne 0) {
    Blocked 'MIRROR_INIT' "git fetch origin failed"
}

$remoteLiveExists = [bool](git --no-pager branch -r --list "origin/$LIVE_BRANCH")
$localLiveExists  = [bool](git --no-pager branch --list $LIVE_BRANCH)
if ($remoteLiveExists) {
    Log "origin/$LIVE_BRANCH exists; checkout + ff-merge"
    if (-not $localLiveExists) {
        git --no-pager checkout -b $LIVE_BRANCH "origin/$LIVE_BRANCH"
    } else {
        git --no-pager checkout $LIVE_BRANCH
    }
    if ($LASTEXITCODE -ne 0) { Blocked 'NON_FAST_FORWARD' "checkout $LIVE_BRANCH failed" }
    git --no-pager merge --ff-only "origin/$LIVE_BRANCH"
    if ($LASTEXITCODE -ne 0) { Blocked 'NON_FAST_FORWARD' "merge --ff-only origin/$LIVE_BRANCH failed" }
} else {
    Log "origin/$LIVE_BRANCH missing; basing on origin/$MAIN_BRANCH"
    if ($localLiveExists) {
        git --no-pager checkout $LIVE_BRANCH
        if ($LASTEXITCODE -ne 0) { Blocked 'MIRROR_INIT' "checkout existing local $LIVE_BRANCH failed" }
        git --no-pager reset --hard "origin/$MAIN_BRANCH"
        if ($LASTEXITCODE -ne 0) { Blocked 'MIRROR_INIT' "reset local $LIVE_BRANCH to origin/$MAIN_BRANCH failed" }
    } else {
        git --no-pager checkout -b $LIVE_BRANCH "origin/$MAIN_BRANCH"
        if ($LASTEXITCODE -ne 0) { Blocked 'MIRROR_INIT' "create branch from origin/$MAIN_BRANCH failed" }
    }
}

git --no-pager reset --hard HEAD
git --no-pager clean -fdx

$BASE_SHA = git --no-pager rev-parse HEAD
Log "BASE_SHA = $BASE_SHA"

foreach ($rel in $ALLOWLIST) {
    $src = Join-Path $OPEN_DESIGN_SOURCE $rel
    if (Test-Path -Path $src) {
        $content = [System.IO.File]::ReadAllBytes($src)
        $headLen = [Math]::Min(256, $content.Length)
        $headStr = [System.Text.Encoding]::ASCII.GetString($content[0..($headLen-1)])
        if ($headStr -match 'ghp_|github_pat_|sk-|GITHUB_TOKEN') {
            Blocked 'SECRET_SUSPECT' "secret pattern detected in header of $rel"
        }
        $dst = Join-Path $MIRROR_DIR $rel
        $dstParent = Split-Path -Path $dst -Parent
        if ($dstParent) {
            New-Item -ItemType Directory -Force -Path $dstParent | Out-Null
        }
        [System.IO.File]::WriteAllBytes($dst, $content)
        Log "ALLOWLIST_COPIED: $rel"
    }
}

Get-ChildItem -Path $MIRROR_DIR -Recurse -File | ForEach-Object {
    $rel = $_.FullName.Replace($MIRROR_DIR, '')
    if ($rel -match $FORBIDDEN_PATTERN) {
        Blocked 'FORBIDDEN_FILE' "path=$($_.FullName) pattern=$FORBIDDEN_PATTERN"
    }
}

$gateExit = 0
$gateRunner = Join-Path $MIRROR_DIR 'gates-runner.js'
if (Test-Path -Path $gateRunner) {
    Log "Running gates-runner.js..."
    Set-Location -Path $MIRROR_DIR
    & node $gateRunner
    $gateExit = $LASTEXITCODE
    Log "gates-runner.js exit=$gateExit"
} else {
    Log "WARNING: no gates-runner.js, default PASS"
}
if ($gateExit -ne 0) {
    Log "SYNC_BLOCKED_GATE_FAILED exit=$gateExit"
    exit 3
}

if ($DryRun) {
    Log "DRY_RUN_OK: gate passed; skipping commit/push"
    exit 0
}

Set-Location -Path $MIRROR_DIR

$changed = @()
foreach ($rel in $ALLOWLIST) {
    $full = Join-Path $MIRROR_DIR $rel
    if (Test-Path -Path $full) {
        $s = git --no-pager status --porcelain -- $rel
        if ($s) {
            $changed += $rel
            git --no-pager add --force -- $rel
        }
    }
}

git --no-pager diff --cached --check
if ($LASTEXITCODE -ne 0) {
    Blocked 'DIRTY_WHITESPACE' "whitespace errors in staged diff"
}

if ($changed.Count -eq 0) {
    Log "NO_CHANGE: allowlist files identical to HEAD"
    exit 0
}

$ts = Get-Date -Format "yyyy-MM-ddTHH:mmK"
$msg = "sync(opendesign): project c5fb3a39 @ $ts`n`nAllowlist: $($changed -join ', ')`nBase: $BASE_SHA"

git --no-pager -c core.autocrlf=false commit -m $msg
if ($LASTEXITCODE -ne 0) {
    Blocked 'PUSH_FAILED' "git commit failed"
}

$NEW_SHA = git --no-pager rev-parse HEAD
Log "COMMIT_OK: $NEW_SHA (base=$BASE_SHA) changed=$($changed -join ',')"

git --no-pager push origin HEAD:refs/heads/$LIVE_BRANCH
if ($LASTEXITCODE -ne 0) {
    Blocked 'PUSH_FAILED' "git push origin HEAD:refs/heads/$LIVE_BRANCH failed"
}

Log "SYNC_OK"
Log "  Base SHA   : $BASE_SHA"
Log "  New  SHA   : $NEW_SHA"
Log "  Changed   : $($changed -join ', ')"
exit 0
