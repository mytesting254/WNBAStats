$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$OutputDir = Join-Path $Root "frontend\public\team-logos"
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null

$Teams = @(
    @{ Code = "atl"; Url = "https://a.espncdn.com/i/teamlogos/wnba/500/atl.png" },
    @{ Code = "chi"; Url = "https://a.espncdn.com/i/teamlogos/wnba/500/chi.png" },
    @{ Code = "conn"; Url = "https://a.espncdn.com/i/teamlogos/wnba/500/conn.png" },
    @{ Code = "dal"; Url = "https://a.espncdn.com/i/teamlogos/wnba/500/dal.png" },
    @{ Code = "gs"; Url = "https://a.espncdn.com/i/teamlogos/wnba/500/gs.png" },
    @{ Code = "ind"; Url = "https://a.espncdn.com/i/teamlogos/wnba/500/ind.png" },
    @{ Code = "lv"; Url = "https://a.espncdn.com/i/teamlogos/wnba/500/lv.png" },
    @{ Code = "la"; Url = "https://a.espncdn.com/i/teamlogos/wnba/500/la.png" },
    @{ Code = "min"; Url = "https://a.espncdn.com/i/teamlogos/wnba/500/min.png" },
    @{ Code = "ny"; Url = "https://a.espncdn.com/i/teamlogos/wnba/500/ny.png" },
    @{ Code = "phx"; Url = "https://a.espncdn.com/i/teamlogos/wnba/500/phx.png" },
    @{ Code = "por"; Url = "https://a.espncdn.com/i/teamlogos/wnba/500/por.png" },
    @{ Code = "sea"; Url = "https://a.espncdn.com/i/teamlogos/wnba/500/sea.png" },
    @{ Code = "tor"; Url = "https://a.espncdn.com/i/teamlogos/wnba/500/tor.png" },
    @{ Code = "wsh"; Url = "https://a.espncdn.com/i/teamlogos/wnba/500/wsh.png" }
)

Add-Type -AssemblyName System.Drawing

foreach ($Team in $Teams) {
    $TempPath = Join-Path $OutputDir "$($Team.Code)-source.png"
    $OutputPath = Join-Path $OutputDir "$($Team.Code).png"

    Write-Host "Downloading $($Team.Code)..."
    Invoke-WebRequest -Uri $Team.Url -OutFile $TempPath

    $Source = [System.Drawing.Image]::FromFile($TempPath)
    try {
        $Canvas = New-Object System.Drawing.Bitmap 96, 96
        $Graphics = [System.Drawing.Graphics]::FromImage($Canvas)
        try {
            $Graphics.Clear([System.Drawing.Color]::Transparent)
            $Graphics.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
            $Graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::HighQuality
            $Scale = [Math]::Min(88 / $Source.Width, 88 / $Source.Height)
            $Width = [Math]::Round($Source.Width * $Scale)
            $Height = [Math]::Round($Source.Height * $Scale)
            $X = [Math]::Round((96 - $Width) / 2)
            $Y = [Math]::Round((96 - $Height) / 2)
            $Graphics.DrawImage($Source, $X, $Y, $Width, $Height)
            $Canvas.Save($OutputPath, [System.Drawing.Imaging.ImageFormat]::Png)
        }
        finally {
            $Graphics.Dispose()
            $Canvas.Dispose()
        }
    }
    finally {
        $Source.Dispose()
        Remove-Item -LiteralPath $TempPath -Force
    }
}

Write-Host "Saved logo thumbnails to $OutputDir"
