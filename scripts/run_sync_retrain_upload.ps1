[CmdletBinding()]
param(
    [string]$Server = "root@89.117.77.41",
    [string]$RemoteDbPath = "",
    [string]$RemoteRoot = "/root/WNBAStats",
    [string]$TrainingStartDate = "",
    [int]$MaxWorkers = 2,
    [switch]$SkipLocalDbBackup,
    [switch]$SkipLocalTraining,
    [switch]$SkipPrewarm,
    [switch]$SkipCacheUpload,
    [switch]$RunServerTraining,
    [switch]$PollServer
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$scriptsDir = $PSScriptRoot
$syncScript = Join-Path $scriptsDir "sync_server_db_to_local.ps1"
$retrainScript = Join-Path $scriptsDir "retrain_local_models.ps1"
$uploadScript = Join-Path $scriptsDir "upload_model_cache_to_server.ps1"
$triggerScript = Join-Path $scriptsDir "trigger_server_training.ps1"

foreach ($requiredScript in @($syncScript, $retrainScript, $uploadScript, $triggerScript)) {
    if (-not (Test-Path $requiredScript)) {
        throw "Required script not found: $requiredScript"
    }
}

Write-Host "[1/3] Syncing server DB to local..."
$syncParams = @{
    Server = $Server
    RemoteRoot = $RemoteRoot
    RemoteDbPath = $RemoteDbPath
}
if ($SkipLocalDbBackup) {
    $syncParams.SkipBackup = $true
}
& $syncScript @syncParams
if ($LASTEXITCODE -ne 0) {
    throw "Step 1 failed (sync server DB)."
}

if (-not $SkipLocalTraining) {
    Write-Host "[2/3] Running local WNBA training validation..."
    $retrainParams = @{
        MaxWorkers = $MaxWorkers
    }
    if (-not [string]::IsNullOrWhiteSpace($TrainingStartDate)) {
        $retrainParams.TrainingStartDate = $TrainingStartDate
    }
    if ($SkipPrewarm) {
        $retrainParams.SkipPrewarm = $true
    }
    & $retrainScript @retrainParams
    if ($LASTEXITCODE -ne 0) {
        throw "Step 2 failed (local WNBA training)."
    }
}
else {
    Write-Host "[2/3] Skipping local WNBA training validation."
}

if (-not $SkipCacheUpload) {
    Write-Host "[3/3] Processing and uploading WNBA model cache..."
    $uploadParams = @{
        Server = $Server
        RemoteRoot = $RemoteRoot
    }
    & $uploadScript @uploadParams
    if ($LASTEXITCODE -ne 0) {
        throw "Step 3 failed (process/upload WNBA model cache)."
    }
}
else {
    Write-Host "[3/3] Skipping WNBA model cache upload."
}

if ($RunServerTraining) {
    Write-Host "[4/4] Triggering live WNBA training..."
    $triggerParams = @{
        Server = $Server
        RemoteRoot = $RemoteRoot
    }
    if ($PollServer) {
        $triggerParams.Poll = $true
    }
    & $triggerScript @triggerParams
    if ($LASTEXITCODE -ne 0) {
        throw "Step 4 failed (trigger live WNBA training)."
    }
}
else {
    Write-Host "[4/4] Skipping live WNBA training trigger."
}

Write-Host "Completed WNBA sync/training workflow."
