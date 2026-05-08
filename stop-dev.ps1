$ErrorActionPreference = "Continue"

$JobNames = @("wnba-backend", "wnba-frontend")

foreach ($Name in $JobNames) {
    $Job = Get-Job -Name $Name -ErrorAction SilentlyContinue
    if ($Job) {
        Write-Host "Stopping job $Name..."
        Stop-Job -Name $Name -ErrorAction SilentlyContinue
        Remove-Job -Name $Name -Force -ErrorAction SilentlyContinue
    }
}

$Ports = @(8000, 5173)
foreach ($Port in $Ports) {
    $Connections = Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue
    foreach ($Connection in $Connections) {
        if ($Connection.OwningProcess -and $Connection.OwningProcess -ne 0) {
            Write-Host "Stopping process $($Connection.OwningProcess) on port $Port..."
            Stop-Process -Id $Connection.OwningProcess -Force -ErrorAction SilentlyContinue
        }
    }
}

Write-Host "Dev servers stopped."
