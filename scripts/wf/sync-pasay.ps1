$ErrorActionPreference = 'Stop'

$Repo = 'D:\AI-Review\pasay-pm'
$MacHost = 'macmini'
$MacRepo = '/Users/jhackuy/Projects/pasay-pm'

# ------------------------------------------------------------------
# WF-002 hardening (2026-08-13):
# * Mac canonical repo is the ONLY authority.
# * Workflow rule files below are content-synced Mac -> Windows only,
#   and are NEVER reverted to an older Git HEAD by this script.
# * No `git reset --hard`, no forced checkout: if a change would be
#   lost, the script FAILS CLOSED instead.
# * This script never writes to the Mac repo (read-only sync direction).
# ------------------------------------------------------------------
$ProtectedFiles = @(
    'AGENTS.md',
    'AI_WORKFLOW_RULES.md',
    'scripts/wf/wf_lib.py',
    'scripts/wf/wf_ctl.py',
    'scripts/wf/sync-pasay.ps1',
    'scripts/wf/wf_ops.py',
    'scripts/wf/wf003_tests.py'
)

function Invoke-MacGit([string]$Arguments) {
    $result = & ssh -o BatchMode=yes -o ConnectTimeout=10 $MacHost "git -C '$MacRepo' $Arguments" 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Mac Git command failed: $($result -join [Environment]::NewLine)"
    }
    return @($result)
}

function Get-Sha256([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Invoke-Native {
    param([scriptblock]$Block)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $Block
    }
    finally {
        $ErrorActionPreference = $prev
    }
}

function Test-IsProtected([string]$Rel) {
    foreach ($p in $ProtectedFiles) {
        if ($Rel -eq $p -or $Rel.StartsWith($p + '/')) { return $true }
    }
    return $false
}

function Sync-ProtectedFromMac {
    $results = @()
    foreach ($rel in $ProtectedFiles) {
        $macPath = "$MacRepo/$($rel.Replace('\', '/'))"
        $winPath = Join-Path $Repo ($rel.Replace('/', '\'))
        $tmp = Join-Path $env:TEMP ("wf-sync-" + [guid]::NewGuid().ToString('N') + ".tmp")
        $prevEap = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            $null = & scp -q -o BatchMode=yes -o ConnectTimeout=10 "${MacHost}:$macPath" $tmp 2>&1
        }
        finally {
            $ErrorActionPreference = $prevEap
        }
        $ok = ($LASTEXITCODE -eq 0) -and (Test-Path -LiteralPath $tmp -PathType Leaf)
        if ($ok) {
            $macSha = (Get-FileHash -LiteralPath $tmp -Algorithm SHA256).Hash.ToLowerInvariant()
            $dir = Split-Path -Parent $winPath
            if ($dir) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
            Copy-Item -LiteralPath $tmp -Destination $winPath -Force
            Remove-Item -LiteralPath $tmp -Force
            $winSha = Get-Sha256 $winPath
            $match = ($winSha -eq $macSha)
            $results += [pscustomobject]@{ File = $rel; Synced = $true; MacSha = $macSha; Match = $match }
            if (-not $match) {
                Write-Output "WORKFLOW_RULES_SYNC_FAILED: $rel"
                exit 5
            }
        }
        else {
            Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
            if ($rel -eq 'AGENTS.md' -or $rel -eq 'AI_WORKFLOW_RULES.md') {
                Write-Output "CANONICAL_RULE_MISSING: $rel"
                exit 5
            }
            Write-Output "PROTECTED_NOT_ON_MAC_YET (skipped): $rel"
            $results += [pscustomobject]@{ File = $rel; Synced = $false; MacSha = $null; Match = $null }
        }
    }
    return $results
}

if (-not (Test-Path -LiteralPath "$Repo\.git" -PathType Container)) {
    throw "Windows audit repository not found: $Repo"
}

$macBranch = ((Invoke-MacGit 'branch --show-current') -join '').Trim()
$macHead = ((Invoke-MacGit 'rev-parse HEAD') -join '').Trim()
$macStatus = @(Invoke-MacGit 'status --short')

if ([string]::IsNullOrWhiteSpace($macBranch)) {
    throw 'Mac repository is in detached HEAD state; refusing to invent a Windows branch.'
}
if ($macHead -notmatch '^[0-9a-f]{40}$') {
    throw "Unexpected Mac HEAD value: $macHead"
}
if ($macBranch -notmatch '^[A-Za-z0-9._/-]+$') {
    throw "Unexpected Mac branch value: $macBranch"
}

& git -C $Repo fetch --prune origin
if ($LASTEXITCODE -ne 0) { throw 'git fetch failed.' }

& git -C $Repo cat-file -e "$macHead^{commit}"
if ($LASTEXITCODE -ne 0) { throw "Mac commit was not fetched: $macHead" }

# 1) Canonical rule content sync (Mac -> Windows), hash-verified.
$protectedResults = @(Sync-ProtectedFromMac)

# 2) Blocking-change check: tracked changes outside protected/runtime files.
$blocking = @()
$safeAlign = @()
foreach ($line in @(& git -C $Repo status --porcelain)) {
    if ($line.Length -lt 3) { continue }
    $code = $line.Substring(0, 2).Trim()
    $rel = $line.Substring(3).Replace('\', '/').Trim()
    if ($code -eq '??') {
        if (Test-IsProtected $rel) {
            $safeAlign += $rel
            continue
        }
        $relNoSlash = $rel.TrimEnd('/')
        $protectedDir = $false
        foreach ($p in $ProtectedFiles) {
            if ($p.StartsWith($relNoSlash + '/')) { $protectedDir = $true; break }
        }
        if ($protectedDir) {
            $safeAlign += $rel
            continue
        }
        # Untracked non-protected file: only matters if the target commit contains it.
        $tb = (Invoke-Native { & git -C $Repo rev-parse --verify "${macHead}:$relNoSlash" }) 2>$null
        if ($LASTEXITCODE -eq 0) {
            if (Test-Path -LiteralPath (Join-Path $Repo $rel) -PathType Container) {
                $blocking += $line
                continue
            }
            $lb = (Invoke-Native { & git -C $Repo hash-object -- $rel }) 2>$null
            if ($LASTEXITCODE -eq 0 -and $lb -eq $tb) { $safeAlign += $rel; continue }
            $blocking += $line
        }
        continue
    }
    if ($code -eq '') { continue }
    if (Test-IsProtected $rel) {
        $safeAlign += $rel
        continue
    }
    if ($rel.StartsWith('.ai-control/') -or $rel.StartsWith('.runtime/') -or
        $rel.StartsWith('pasay-telegram-bot/state/') -or $rel.StartsWith('ux/results/') -or
        $rel.StartsWith('worktrees/')) { continue }
    # Allow files whose working content already equals the target commit
    # (e.g. files previously synced from canonical); checkout is then safe.
    $targetBlob = (Invoke-Native { & git -C $Repo rev-parse --verify "${macHead}:$rel" }) 2>$null
    if ($LASTEXITCODE -eq 0) {
        $localBlob = (Invoke-Native { & git -C $Repo hash-object -- $rel }) 2>$null
        if ($LASTEXITCODE -eq 0 -and $localBlob -eq $targetBlob) {
            $safeAlign += $rel
            continue
        }
    }
    $blocking += $line
}
if ($blocking.Count -gt 0) {
    Write-Output ''
    Write-Output 'BLOCKED_DIRTY_WORKTREE'
    Write-Output 'Tracked changes outside protected workflow files; refusing to overwrite. Files:'
    $blocking | ForEach-Object { Write-Output $_ }
    exit 3
}

# 3) Safe branch alignment: NO reset --hard, NO forced checkout.
#    Protected files and files whose content already equals the target commit
#    are staged first (index only, not a commit) so checkout can move to a Mac
#    HEAD that already contains them.
Invoke-Native { & git -C $Repo checkout -B $macBranch $macHead } 2>$null
$aligned = ($LASTEXITCODE -eq 0)
if (-not $aligned) {
    if ($safeAlign.Count -gt 0) {
        Invoke-Native { & git -C $Repo add -- $safeAlign } 2>$null
    }
    Invoke-Native { & git -C $Repo checkout -B $macBranch $macHead } 2>$null
    $aligned = ($LASTEXITCODE -eq 0)
    if (-not $aligned) {
        Invoke-Native { & git -C $Repo reset -- $safeAlign } 2>$null
        Write-Output 'ALIGNMENT_DEFERRED: canonical content not yet in Git HEAD (uncommitted on Mac); protected files already synced from canonical. No changes were destroyed.'
        exit 4
    }
}

# 4) Final verification: protected files still match canonical.
$rulesOk = $true
foreach ($r in $protectedResults) {
    if (-not $r.Synced) { continue }
    $winPath = Join-Path $Repo ($r.File.Replace('/', '\'))
    if ((Get-Sha256 $winPath) -ne $r.MacSha) { $rulesOk = $false }
}

$windowsBranch = (& git -C $Repo branch --show-current).Trim()
$windowsHead = (& git -C $Repo rev-parse HEAD).Trim()
$windowsStatus = @(& git -C $Repo status --short)
$match = ($windowsBranch -eq $macBranch) -and ($windowsHead -eq $macHead)
$baseline = $windowsHead -eq $macHead
$macState = if ($macStatus.Count -eq 0) { 'CLEAN' } else { 'DIRTY' }
$windowsState = if ($windowsStatus.Count -eq 0) { 'CLEAN' } else { 'DIRTY' }

Write-Output ''
Write-Output 'PASAY AUDIT SYNC (hardened)'
Write-Output 'DIRECTION: canonical (Mac) -> Windows mirror only; no write-back to Mac.'
Write-Output ''
Write-Output 'MAC'
Write-Output "Branch: $macBranch"
Write-Output "HEAD: $macHead"
Write-Output "Status: $macState"
if ($macStatus.Count -gt 0) {
    Write-Output 'Uncommitted files:'
    $macStatus | ForEach-Object { Write-Output $_ }
}
Write-Output ''
Write-Output 'WINDOWS'
Write-Output "Branch: $windowsBranch"
Write-Output "HEAD: $windowsHead"
Write-Output "Status: $windowsState"
if ($windowsStatus.Count -gt 0) {
    $windowsStatus | ForEach-Object { Write-Output $_ }
}
Write-Output ''
Write-Output 'SYNC'
Write-Output "MATCH: $(if ($match) { 'YES' } else { 'NO' })"
Write-Output "Committed baseline synchronized: $(if ($baseline) { 'YES' } else { 'NO' })"
Write-Output "WORKFLOW_RULES_SYNC: $(if ($rulesOk) { 'OK' } else { 'FAIL' })"

if (-not $rulesOk) { exit 5 }
if (-not $match) { exit 2 }
exit 0
