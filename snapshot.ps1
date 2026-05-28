$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$SnapshotDir = if ($env:SNAPSHOT_DIR) { $env:SNAPSHOT_DIR } else { Join-Path $Root "data\snapshots" }
$DbPath = if ($env:WNBA_DB_PATH) { $env:WNBA_DB_PATH } else { Join-Path $Root "data\wnba.sqlite" }

function Show-Usage {
    Write-Host "Usage:"
    Write-Host "  .\snapshot.ps1"
    Write-Host "  .\snapshot.ps1 auto"
    Write-Host "  .\snapshot.ps1 start"
    Write-Host "  .\snapshot.ps1 create [--label NAME] [--device NAME] [--db-path PATH] [--output-dir PATH]"
    Write-Host "  .\snapshot.ps1 restore <snapshot.sqlite> [--force] [--db-path PATH] [--snapshot-dir PATH]"
    Write-Host "  .\snapshot.ps1 list [--snapshot-dir PATH]"
    Write-Host "  .\snapshot.ps1 latest [--snapshot-dir PATH]"
    Write-Host "  .\snapshot.ps1 help"
}

if (-not (Test-Path $Python)) {
    Write-Error "Missing Python virtual environment. Run: python3.14.exe -m venv .venv; & '.\.venv\Scripts\pip.exe' install -r backend\requirements.txt"
}

$Command = if ($args.Count -gt 0) { $args[0] } else { "auto" }
$RemainingArgs = if ($args.Count -gt 1) { $args[1..($args.Count - 1)] } else { @() }

switch ($Command) {
    { $_ -in @("auto", "start") } {
        New-Item -ItemType Directory -Force -Path $SnapshotDir | Out-Null
        $LatestFile = Get-ChildItem -Path $SnapshotDir -Filter *.sqlite -File -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending |
            Select-Object -First 1

        if ($LatestFile) {
            Write-Host "Restoring latest snapshot: $($LatestFile.FullName)"
            & $Python (Join-Path $Root "scripts\snapshot_restore.py") $LatestFile.FullName "--db-path" $DbPath "--snapshot-dir" $SnapshotDir
            if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        }
        else {
            Write-Host "No snapshots found in $SnapshotDir. Starting without restore."
        }

        Write-Host "Starting app..."
        & (Join-Path $Root "dev.ps1")
        $DevExit = $LASTEXITCODE

        Write-Host "Creating shutdown snapshot..."
        & $Python (Join-Path $Root "scripts\snapshot_create.py") "--db-path" $DbPath "--output-dir" $SnapshotDir "--label" "auto"
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

        exit $DevExit
    }
    "create" {
        & $Python (Join-Path $Root "scripts\snapshot_create.py") "--db-path" $DbPath "--output-dir" $SnapshotDir @RemainingArgs
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
        $Files = Get-ChildItem -Path $Dir -Filter *.sqlite -File -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending

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
        $LatestFile = Get-ChildItem -Path $Dir -Filter *.sqlite -File -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending |
            Select-Object -First 1

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
