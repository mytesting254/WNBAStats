$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$BackendPython = Join-Path $Root ".venv\Scripts\python.exe"
$FrontendDir = Join-Path $Root "frontend"
$FrontendPort = "5174"

if (-not (Test-Path $BackendPython)) {
    Write-Error "Missing Python virtual environment. Run: python3.14.exe -m venv .venv; & '.\.venv\Scripts\pip.exe' install -r backend\requirements.txt"
}

if (-not (Test-Path (Join-Path $FrontendDir "node_modules"))) {
    Write-Error "Missing frontend dependencies. Run: cd frontend; npm.cmd install"
}

Write-Host "Initializing database..."
& $BackendPython (Join-Path $Root "scripts\init_db.py")

$Processes = @()

try {
    Write-Host "Starting FastAPI backend on http://127.0.0.1:8000"
    $BackendProcess = Start-Process -FilePath $BackendPython -ArgumentList @(
        "-m", "uvicorn", "backend.app.main:app",
        "--host", "127.0.0.1",
        "--port", "8000"
    ) -WorkingDirectory $Root -NoNewWindow -PassThru
    $Processes += $BackendProcess

    Write-Host "Starting React frontend on http://127.0.0.1:$FrontendPort"
    $FrontendProcess = Start-Process -FilePath "npm.cmd" -ArgumentList @(
        "run", "dev", "--", "--host", "127.0.0.1", "--port", $FrontendPort
    ) -WorkingDirectory $FrontendDir -NoNewWindow -PassThru
    $Processes += $FrontendProcess

    Write-Host ""
    Write-Host "Open http://127.0.0.1:$FrontendPort"
    Write-Host "API docs: http://127.0.0.1:8000/docs"
    Write-Host "Press Ctrl+C in this terminal to stop both servers."
    Write-Host ""

    while ($true) {
        $Running = $Processes | Where-Object { -not $_.HasExited }
        if (-not $Running) {
            break
        }
        Start-Sleep -Seconds 1
    }
}
finally {
    Write-Host ""
    Write-Host "Stopping dev servers..."
    foreach ($Process in $Processes) {
        if ($Process -and -not $Process.HasExited) {
            Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
        }
    }
    Write-Host "Stopped."
}
