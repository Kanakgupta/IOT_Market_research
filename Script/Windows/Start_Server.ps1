<#
.SYNOPSIS
  Starts a local web server for the Wireless Intelligence website.

.PARAMETER Port
  Port to listen on (default 8080). If it is busy, the next free port is used.

.PARAMETER NoBrowser
  Do not open the browser automatically.

.EXAMPLE
  .\Start_Server.ps1
  .\Start_Server.ps1 -Port 9000
#>
param([int]$Port = 8080, [switch]$NoBrowser)

$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$site = Join-Path $root "site"
if (-not (Test-Path (Join-Path $site "index.html"))) { Write-Host "Site not found at $site" -ForegroundColor Red; exit 1 }
if (-not (Test-Path (Join-Path $site "data\report.js"))) {
    Write-Host "No report data yet - run Script\Windows\Run_Agent.bat first." -ForegroundColor Yellow
}

$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { $py = (Get-Command py -ErrorAction SilentlyContinue).Source }
if (-not $py) { Write-Host "Python was not found on PATH. Install Python 3.10+ and try again." -ForegroundColor Red; exit 2 }

function Test-PortBusy([int]$p) {
    $null -ne (Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue)
}
while (Test-PortBusy $Port) { Write-Host "Port $Port is in use, trying $($Port + 1)..." -ForegroundColor Yellow; $Port++ }

$url = "http://localhost:$Port/index.html"
Write-Host ""
Write-Host "Wireless Intelligence - local server" -ForegroundColor Cyan
Write-Host "Serving : $site"
Write-Host "Open    : $url" -ForegroundColor Green
Write-Host "Stop    : press Ctrl+C (or close this window)"
Write-Host ""

if (-not $NoBrowser) {
    # open the browser shortly after the server starts
    Start-Job -ScriptBlock { param($u) Start-Sleep -Seconds 2; Start-Process $u } -ArgumentList $url | Out-Null
}
& $py -m http.server $Port --bind 127.0.0.1 --directory $site
