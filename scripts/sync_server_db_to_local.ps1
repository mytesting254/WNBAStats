[CmdletBinding()]
param(
    [string]$Server = "root@89.117.77.41",
    [string]$RemoteRoot = "/root/WNBAStats",
    [string]$RemoteDbPath = "",
    [string]$LocalDbPath = "",
    [switch]$SkipBackup
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if ([string]::IsNullOrWhiteSpace($LocalDbPath)) {
    $LocalDbPath = Join-Path $repoRoot "data\wnba.sqlite"
}

$localDbDir = Split-Path -Parent $LocalDbPath
$tempPath = "$LocalDbPath.download"
$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"

if (-not (Get-Command scp -ErrorAction SilentlyContinue)) {
    throw "scp is not available in PATH. Install OpenSSH client first."
}
if (-not (Get-Command ssh -ErrorAction SilentlyContinue)) {
    throw "ssh is not available in PATH. Install OpenSSH client first."
}

if ([string]::IsNullOrWhiteSpace($RemoteDbPath)) {
    Write-Host "Resolving live WNBA DB path on $Server"
    $runtimeJson = & ssh $Server "cd '$RemoteRoot' && python3 scripts/live_backend.py host-runtime-info"
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to resolve live WNBA DB path with exit code $LASTEXITCODE"
    }
    $runtimeInfo = $runtimeJson | ConvertFrom-Json
    $RemoteDbPath = [string]$runtimeInfo.db_path
    if ([string]::IsNullOrWhiteSpace($RemoteDbPath)) {
        throw "live_backend.py host-runtime-info did not return db_path."
    }
}

if (-not (Test-Path $localDbDir)) {
    New-Item -ItemType Directory -Path $localDbDir -Force | Out-Null
}

if ((Test-Path $LocalDbPath) -and (-not $SkipBackup)) {
    $backupDir = Join-Path $localDbDir "backups"
    New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
    $backupPath = Join-Path $backupDir ("wnba_{0}.sqlite" -f $timestamp)
    Copy-Item -Path $LocalDbPath -Destination $backupPath -Force
    Write-Host "Backed up local DB to $backupPath"
}

if (Test-Path $tempPath) {
    Remove-Item -Path $tempPath -Force
}

Write-Host "Downloading $Server`:$RemoteDbPath"
& scp "$Server`:$RemoteDbPath" "$tempPath"
if ($LASTEXITCODE -ne 0) {
    throw "scp download failed with exit code $LASTEXITCODE"
}

Move-Item -Path $tempPath -Destination $LocalDbPath -Force
Write-Host "Updated local DB at $LocalDbPath"
