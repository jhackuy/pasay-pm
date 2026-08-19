$ErrorActionPreference = 'Stop'

$Repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$CanonicalRules = Join-Path $Repo 'AI_WORKFLOW_RULES.md'
$LegacyMirror = Join-Path $Repo '.ai-control\RULES.md'

function Get-Sha256([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

$gitDir = Join-Path $Repo '.git'
if (-not (Test-Path -LiteralPath $gitDir)) {
    $resolvedRepo = (& git -C $Repo rev-parse --show-toplevel 2>$null).Trim()
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($resolvedRepo)) {
        throw "Pasay repository not found: $Repo"
    }
    $Repo = $resolvedRepo
}

if (-not (Test-Path -LiteralPath $CanonicalRules -PathType Leaf)) {
    Write-Output 'CANONICAL_RULE_MISSING: AI_WORKFLOW_RULES.md'
    exit 5
}

$legacyDir = Split-Path -Parent $LegacyMirror
if ($legacyDir) {
    New-Item -ItemType Directory -Force -Path $legacyDir | Out-Null
}
Copy-Item -LiteralPath $CanonicalRules -Destination $LegacyMirror -Force

$canonicalSha = Get-Sha256 $CanonicalRules
$legacySha = Get-Sha256 $LegacyMirror
$branch = (& git -C $Repo branch --show-current).Trim()
$head = (& git -C $Repo rev-parse HEAD).Trim()
$status = @(& git -C $Repo status --short)
$rulesOk = ($canonicalSha -eq $legacySha)
$repoState = if ($status.Count -eq 0) { 'CLEAN' } else { 'DIRTY' }

Write-Output ''
Write-Output 'PASAY WORKFLOW SYNC'
Write-Output 'AUTHORITY: Windows canonical repository'
Write-Output "REPO: $Repo"
Write-Output "Branch: $branch"
Write-Output "HEAD: $head"
Write-Output "Status: $repoState"
Write-Output "Canonical rules: <repo-root>/AI_WORKFLOW_RULES.md"
Write-Output "Legacy mirror: .ai-control/RULES.md"
Write-Output "WORKFLOW_RULES_SYNC: $(if ($rulesOk) { 'OK' } else { 'FAIL' })"
if ($status.Count -gt 0) {
    Write-Output 'Uncommitted files:'
    $status | ForEach-Object { Write-Output $_ }
}

if (-not $rulesOk) { exit 5 }
exit 0
