[CmdletBinding()]
param(
    [string]$Server = "root@89.117.77.41",
    [string]$RemoteRoot = "/root/WNBAStats",
    [string]$SourceCacheDir = "",
    [string]$ProcessedCacheDir = "",
    [switch]$SkipRemoteBackup
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pythonExe = Join-Path $repoRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $pythonExe)) {
    throw "Python executable not found at $pythonExe"
}
if (-not (Get-Command ssh -ErrorAction SilentlyContinue)) {
    throw "ssh is not available in PATH. Install OpenSSH client first."
}
if (-not (Get-Command scp -ErrorAction SilentlyContinue)) {
    throw "scp is not available in PATH. Install OpenSSH client first."
}

if ([string]::IsNullOrWhiteSpace($SourceCacheDir)) {
    $SourceCacheDir = Join-Path $repoRoot "data\cache"
}
if ([string]::IsNullOrWhiteSpace($ProcessedCacheDir)) {
    $ProcessedCacheDir = Join-Path $repoRoot "data\processed_model_cache"
}

Write-Host "Resolving live WNBA cache path on $Server"
$runtimeJson = & ssh $Server "cd '$RemoteRoot' && python3 scripts/live_backend.py host-runtime-info"
if ($LASTEXITCODE -ne 0) {
    throw "Unable to resolve live WNBA cache path with exit code $LASTEXITCODE"
}
$runtimeInfo = $runtimeJson | ConvertFrom-Json
$remoteDbPath = [string]$runtimeInfo.db_path
$remoteCacheDir = [string]$runtimeInfo.cache_dir
if ([string]::IsNullOrWhiteSpace($remoteDbPath) -or [string]::IsNullOrWhiteSpace($remoteCacheDir)) {
    throw "live_backend.py host-runtime-info did not return db_path and cache_dir."
}

& $pythonExe "scripts\process_model_cache.py" --source-cache-dir $SourceCacheDir --output-cache-dir $ProcessedCacheDir --target-db-path $remoteDbPath --clean
if ($LASTEXITCODE -ne 0) {
    throw "WNBA cache processing failed with exit code $LASTEXITCODE"
}

if (-not $SkipRemoteBackup) {
    $timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $remoteBackupDir = "$remoteCacheDir/model-cache-backups/$timestamp"
    $backupCmd = "mkdir -p '$remoteBackupDir' && cp -a '$remoteCacheDir'/learned_prop_model-*.json '$remoteBackupDir'/ 2>/dev/null || true"
    Write-Host "Creating remote model cache backup at $remoteBackupDir"
    & ssh $Server $backupCmd
    if ($LASTEXITCODE -ne 0) {
        throw "Remote cache backup command failed with exit code $LASTEXITCODE"
    }
}

$filesToUpload = @(Get-ChildItem -Path $ProcessedCacheDir -Filter "learned_prop_model-*.json" -File | Select-Object -ExpandProperty FullName)
if ($filesToUpload.Count -eq 0) {
    throw "No processed WNBA model cache files found in $ProcessedCacheDir"
}

& ssh $Server "mkdir -p '$remoteCacheDir'"
if ($LASTEXITCODE -ne 0) {
    throw "Remote cache directory creation failed with exit code $LASTEXITCODE"
}

foreach ($file in $filesToUpload) {
    Write-Host "Uploading $file"
    & scp $file "$Server`:$remoteCacheDir/"
    if ($LASTEXITCODE -ne 0) {
        throw "scp upload failed for $file with exit code $LASTEXITCODE"
    }
}

Write-Host "Uploaded $($filesToUpload.Count) processed WNBA model cache file(s) to $Server`:$remoteCacheDir"
