$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$SnapshotDir = if ($env:SNAPSHOT_DIR) { $env:SNAPSHOT_DIR } else { Join-Path $Root "data\snapshots" }
$DbPath = if ($env:WNBA_DB_PATH) { $env:WNBA_DB_PATH } else { Join-Path $Root "data\wnba.sqlite" }
$SnapshotAutosaveInterval = if ($env:SNAPSHOT_AUTOSAVE_INTERVAL) { $env:SNAPSHOT_AUTOSAVE_INTERVAL } else { "300" }
$SnapshotKeepLatest = if ($env:SNAPSHOT_KEEP_LATEST) { $env:SNAPSHOT_KEEP_LATEST } else { "5" }
$SnapshotName = if ($env:SNAPSHOT_NAME) { $env:SNAPSHOT_NAME } else { "wnba-runtime" }

function Show-Usage {
    Write-Host "Usage:"
    Write-Host "  .\snapshot.ps1"
    Write-Host "  .\snapshot.ps1 auto"
    Write-Host "  .\snapshot.ps1 start"
    Write-Host "  .\snapshot.ps1 watch"
    Write-Host "  .\snapshot.ps1 create [--name NAME] [--label NAME] [--device NAME] [--db-path PATH] [--output-dir PATH] [--keep-latest N]"
    Write-Host "  .\snapshot.ps1 restore <snapshot.sqlite> [--force] [--db-path PATH] [--snapshot-dir PATH]"
    Write-Host "  .\snapshot.ps1 list [--snapshot-dir PATH]"
    Write-Host "  .\snapshot.ps1 latest [--snapshot-dir PATH]"
    Write-Host "  .\snapshot.ps1 help"
}

if (-not (Test-Path $Python)) {
    Write-Error "Missing Python virtual environment. Run: python3.14.exe -m venv .venv; & '.\.venv\Scripts\pip.exe' install -r backend\requirements.txt"
}

function Resolve-SnapshotIntervalSeconds {
    param([string]$Raw)
    $Parsed = 0
    if (-not [int]::TryParse($Raw, [ref]$Parsed) -or $Parsed -lt 5) {
        Write-Host "Invalid SNAPSHOT_AUTOSAVE_INTERVAL='$Raw'. Using 300 seconds."
        return 300
    }
    return $Parsed
}

function Get-LatestSnapshotFile {
    param([string]$Dir)
    return Get-ChildItem -Path $Dir -Filter "wnba-*.sqlite" -File -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTimeUtc -Descending |
        Select-Object -First 1
}

function Start-SnapshotWatcher {
    param(
        [string]$PythonPath,
        [string]$RootPath,
        [string]$DatabasePath,
        [string]$OutputDir,
        [int]$IntervalSeconds,
        [string]$SnapshotName,
        [string]$SnapshotKeepLatest
    )

    New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
    return Start-Job -Name "snapshot-watcher" -ScriptBlock {
        param($PythonExe, $RepoRoot, $DbFile, $Dir, $Interval, $RollingSnapshotName, $SnapshotKeep)
        $ErrorActionPreference = "Stop"
        $LastTicks = $null
        while ($true) {
            if (Test-Path $DbFile) {
                $Ticks = (Get-Item -LiteralPath $DbFile).LastWriteTimeUtc.Ticks
                if ($LastTicks -ne $Ticks) {
                    & $PythonExe (Join-Path $RepoRoot "scripts\snapshot_create.py") "--db-path" $DbFile "--output-dir" $Dir "--name" $RollingSnapshotName "--keep-latest" $SnapshotKeep
                    $LastTicks = $Ticks
                }
            }
            Start-Sleep -Seconds $Interval
        }
    } -ArgumentList @($PythonPath, $RootPath, $DatabasePath, $OutputDir, $IntervalSeconds, $SnapshotName, $SnapshotKeepLatest)
}

$Command = if ($args.Count -gt 0) { $args[0] } else { "auto" }
$RemainingArgs = if ($args.Count -gt 1) { $args[1..($args.Count - 1)] } else { @() }

switch ($Command) {
    { $_ -in @("auto", "start") } {
        New-Item -ItemType Directory -Force -Path $SnapshotDir | Out-Null
        $LatestFile = $null
        if (-not (Test-Path $DbPath)) {
            $LatestFile = Get-LatestSnapshotFile -Dir $SnapshotDir
        }

        if (Test-Path $DbPath) {
            Write-Host "Existing runtime DB found at $DbPath. Skipping auto-restore to avoid rolling back newer local data."
        }
        elseif ($LatestFile) {
            Write-Host "Restoring latest snapshot: $($LatestFile.FullName)"
            & $Python (Join-Path $Root "scripts\snapshot_restore.py") $LatestFile.FullName "--db-path" $DbPath "--snapshot-dir" $SnapshotDir
            if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        }
        else {
            Write-Host "No snapshots found in $SnapshotDir. Starting without restore."
        }

        $IntervalSeconds = Resolve-SnapshotIntervalSeconds -Raw $SnapshotAutosaveInterval
        Write-Host "Starting snapshot watcher (every $($IntervalSeconds)s)..."
        $WatcherJob = Start-SnapshotWatcher -PythonPath $Python -RootPath $Root -DatabasePath $DbPath -OutputDir $SnapshotDir -IntervalSeconds $IntervalSeconds -SnapshotName $SnapshotName

        Write-Host "Starting app..."
        try {
            & (Join-Path $Root "dev.ps1")
            $DevExit = $LASTEXITCODE
        }
        finally {
            if ($WatcherJob) {
                Stop-Job -Job $WatcherJob -ErrorAction SilentlyContinue
                Remove-Job -Job $WatcherJob -Force -ErrorAction SilentlyContinue
            }
        }

        if ($DevExit -eq 0) {
            Write-Host "Creating shutdown snapshot..."
            & $Python (Join-Path $Root "scripts\snapshot_create.py") "--db-path" $DbPath "--output-dir" $SnapshotDir "--name" $SnapshotName "--keep-latest" $SnapshotKeepLatest
            if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        }
        else {
            Write-Host "Skipping shutdown snapshot due to non-clean app exit code: $DevExit"
        }

        exit $DevExit
    }
    "watch" {
        $IntervalSeconds = Resolve-SnapshotIntervalSeconds -Raw $SnapshotAutosaveInterval
        Write-Host "Watching DB changes for snapshots in $SnapshotDir (interval $($IntervalSeconds)s, snapshot '$SnapshotName.sqlite')."
        New-Item -ItemType Directory -Force -Path $SnapshotDir | Out-Null

        $LastTicks = $null
        while ($true) {
            if (Test-Path $DbPath) {
                $Ticks = (Get-Item -LiteralPath $DbPath).LastWriteTimeUtc.Ticks
                if ($LastTicks -ne $Ticks) {
                    & $Python (Join-Path $Root "scripts\snapshot_create.py") "--db-path" $DbPath "--output-dir" $SnapshotDir "--name" $SnapshotName "--keep-latest" $SnapshotKeepLatest
                    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
                    $LastTicks = $Ticks
                }
            }
            Start-Sleep -Seconds $IntervalSeconds
        }
    }
    "create" {
        & $Python (Join-Path $Root "scripts\snapshot_create.py") "--db-path" $DbPath "--output-dir" $SnapshotDir "--name" $SnapshotName "--keep-latest" $SnapshotKeepLatest @RemainingArgs
        exit $LASTEXITCODE
    }
    "restore" {
        if ($RemainingArgs.Count -lt 1) {
            Write-Host "Missing snapshot filename."
            Show-Usage
            exit 1
        }
        $Snapshot = $RemainingArgs[0]
        $RestoreArgs = if ($RemainingArgs.Count -gt 1) { $RemainingArgs[1..($RemainingArgs.Count - 1)] } else { @() }
        & $Python (Join-Path $Root "scripts\snapshot_restore.py") $Snapshot "--db-path" $DbPath "--snapshot-dir" $SnapshotDir @RestoreArgs
        exit $LASTEXITCODE
    }
    "list" {
        $Dir = $SnapshotDir
        if ($RemainingArgs.Count -ge 2 -and $RemainingArgs[0] -eq "--snapshot-dir") {
            $Dir = $RemainingArgs[1]
        }

        New-Item -ItemType Directory -Force -Path $Dir | Out-Null
        $Files = Get-ChildItem -Path $Dir -Filter "wnba-*.sqlite" -File -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTimeUtc -Descending

        if (-not $Files) {
            Write-Host "No snapshots found in $Dir"
            exit 0
        }

        $Files | ForEach-Object { Write-Host $_.FullName }
    }
    "latest" {
        $Dir = $SnapshotDir
        if ($RemainingArgs.Count -ge 2 -and $RemainingArgs[0] -eq "--snapshot-dir") {
            $Dir = $RemainingArgs[1]
        }

        New-Item -ItemType Directory -Force -Path $Dir | Out-Null
        $LatestFile = Get-LatestSnapshotFile -Dir $Dir

        if (-not $LatestFile) {
            Write-Host "No snapshots found in $Dir"
            exit 1
        }

        Write-Host $LatestFile.FullName
    }
    { $_ -in @("help", "-h", "--help") } {
        Show-Usage
    }
    default {
        Write-Host "Unknown command: $Command"
        Show-Usage
        exit 1
    }
}
