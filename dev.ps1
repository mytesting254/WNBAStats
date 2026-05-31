$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$BackendPython = Join-Path $Root ".venv\Scripts\python.exe"
$FrontendDir = Join-Path $Root "frontend"
$BackendPort = if ($env:BACKEND_PORT) { [int]$env:BACKEND_PORT } else { 8010 }
$FrontendPort = if ($env:FRONTEND_PORT) { [int]$env:FRONTEND_PORT } else { 5184 }

function Test-PortAvailable {
    param([int]$Port)

    # netstat reliably reports active listeners across bind addresses on Windows.
    $ListenPattern = ":{0}\s+.*LISTENING" -f $Port
    $ExistingListener = netstat -ano -p tcp | Select-String -Pattern $ListenPattern | Select-Object -First 1
    if ($null -ne $ExistingListener) {
        return $false
    }

    $Listener = $null
    try {
        $Listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Any, $Port)
        $Listener.Start()
        return $true
    }
    catch {
        return $false
    }
    finally {
        if ($Listener) {
            $Listener.Stop()
        }
    }
}

function Resolve-FreePort {
    param(
        [int]$PreferredPort,
        [int[]]$ReservedPorts = @()
    )
    $Port = $PreferredPort
    while ($Port -le 65535) {
        if ($ReservedPorts -contains $Port) {
            $Port++
            continue
        }
        if (Test-PortAvailable -Port $Port) {
            return $Port
        }
        $Port++
    }
    throw "No available port found from $PreferredPort to 65535."
}

# Enforce local SQLite in dev startup by default.
$env:USE_TURSO = "0"
if (-not (Test-Path Env:WNBA_DB_PATH)) {
    $env:WNBA_DB_PATH = (Join-Path $Root "data\\wnba.sqlite")
}

if (-not (Test-Path $BackendPython)) {
    Write-Error "Missing Python virtual environment. Run: python3.14.exe -m venv .venv; & '.\.venv\Scripts\pip.exe' install -r backend\requirements.txt"
}

if (-not (Test-Path (Join-Path $FrontendDir "node_modules"))) {
    Write-Error "Missing frontend dependencies. Run: cd frontend; npm.cmd install"
}

Write-Host "Initializing database..."
& $BackendPython (Join-Path $Root "scripts\init_db.py")

$Processes = @()
$ResolvedBackendPort = Resolve-FreePort -PreferredPort $BackendPort
$ResolvedFrontendPort = Resolve-FreePort -PreferredPort $FrontendPort -ReservedPorts @($ResolvedBackendPort)

try {
    if ($ResolvedBackendPort -ne $BackendPort) {
        Write-Host "Backend port $BackendPort is busy. Using $ResolvedBackendPort."
    }
    if ($ResolvedFrontendPort -ne $FrontendPort) {
        Write-Host "Frontend port $FrontendPort is busy. Using $ResolvedFrontendPort."
    }

    Write-Host "Starting FastAPI backend on http://0.0.0.0:$ResolvedBackendPort"
    $BackendProcess = Start-Process -FilePath $BackendPython -ArgumentList @(
        "-m", "uvicorn", "backend.app.main:app",
        "--host", "0.0.0.0",
        "--port", $ResolvedBackendPort
    ) -WorkingDirectory $Root -NoNewWindow -PassThru
    $Processes += $BackendProcess

    Write-Host "Starting React frontend on http://0.0.0.0:$ResolvedFrontendPort"
    $env:VITE_BACKEND_URL = "http://127.0.0.1:$ResolvedBackendPort"
    $FrontendProcess = Start-Process -FilePath "npm.cmd" -ArgumentList @(
        "run", "dev", "--", "--host", "0.0.0.0", "--port", $ResolvedFrontendPort
    ) -WorkingDirectory $FrontendDir -NoNewWindow -PassThru
    $Processes += $FrontendProcess

    Write-Host ""
    Write-Host "Open http://127.0.0.1:$ResolvedFrontendPort"
    Write-Host "API docs: http://127.0.0.1:$ResolvedBackendPort/docs"
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
