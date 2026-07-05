[CmdletBinding()]
param(
    [string]$TrainingStartDate = "",
    [int]$MaxWorkers = 2,
    [string]$DbPath = "",
    [string]$CacheDir = "",
    [switch]$SkipPrewarm
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pythonExe = Join-Path $repoRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $pythonExe)) {
    throw "Python executable not found at $pythonExe"
}

$env:PYTHONPATH = $repoRoot
$env:USE_TURSO = "0"
$env:USE_LOCAL_DB = "true"

if ([string]::IsNullOrWhiteSpace($DbPath)) {
    $DbPath = Join-Path $repoRoot "data\wnba.sqlite"
}
if ([string]::IsNullOrWhiteSpace($CacheDir)) {
    $CacheDir = Join-Path (Split-Path -Parent $DbPath) "cache"
}

$args = @(
    "scripts\run_model_training.py",
    "--db-path", $DbPath,
    "--cache-dir", $CacheDir,
    "--max-workers", $MaxWorkers
)

if (-not [string]::IsNullOrWhiteSpace($TrainingStartDate)) {
    $args += @("--training-start-date", $TrainingStartDate)
}
if ($SkipPrewarm) {
    $args += "--skip-prewarm"
}

Write-Host "Running local WNBA training from $repoRoot"
& $pythonExe @args
if ($LASTEXITCODE -ne 0) {
    throw "Local WNBA training failed with exit code $LASTEXITCODE"
}

Write-Host "Local WNBA training completed successfully."
