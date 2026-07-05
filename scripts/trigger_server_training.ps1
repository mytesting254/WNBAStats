[CmdletBinding()]
param(
    [string]$Server = "root@89.117.77.41",
    [string]$RemoteRoot = "/root/WNBAStats",
    [string]$Container = "",
    [switch]$Poll
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if (-not (Get-Command ssh -ErrorAction SilentlyContinue)) {
    throw "ssh is not available in PATH. Install OpenSSH client first."
}

$containerArg = ""
if (-not [string]::IsNullOrWhiteSpace($Container)) {
    $containerArg = "--container $Container"
}

$triggerCmd = "cd '$RemoteRoot' && python3 scripts/live_backend.py $containerArg exec -- sh -lc 'curl -fsS -X POST -H ""X-API-Key: `$API_KEY"" http://127.0.0.1:8010/api/models/train'"
Write-Host "Triggering live WNBA training on $Server"
& ssh $Server $triggerCmd
if ($LASTEXITCODE -ne 0) {
    throw "Remote training trigger failed with exit code $LASTEXITCODE"
}

if ($Poll) {
    $pollCmd = "cd '$RemoteRoot' && python3 scripts/live_backend.py $containerArg exec -- sh -lc 'curl -fsS http://127.0.0.1:8010/api/ops/health'"
    Write-Host "Polling live WNBA training status on $Server"
    & ssh $Server $pollCmd
    if ($LASTEXITCODE -ne 0) {
        throw "Remote training poll failed with exit code $LASTEXITCODE"
    }
}
