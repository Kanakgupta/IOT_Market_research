<#
.SYNOPSIS
  Runs the Wireless Market Research Agent (Wi-Fi / Bluetooth) on demand.

.DESCRIPTION
  Searches public news feeds for all tracked companies, applications and topics,
  builds the report and refreshes the website data in ..\..\site\data.
  A full run takes about 4 minutes. A log is written to ..\..\agent\logs.

.PARAMETER Quick
  Small smoke-test run (~30 seconds, only a few companies/apps).

.PARAMETER Open
  Open the report in the default browser when the run finishes.

.EXAMPLE
  .\Run_Agent.ps1
  .\Run_Agent.ps1 -Open
  .\Run_Agent.ps1 -Quick
#>
param([switch]$Quick, [switch]$Open)

$ErrorActionPreference = "Continue"
$root   = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$agent  = Join-Path $root "agent"
$logDir = Join-Path $agent "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir ("run_" + (Get-Date -Format "yyyy-MM-dd_HHmmss") + ".log")

$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { $py = (Get-Command py -ErrorAction SilentlyContinue).Source }
if (-not $py) { Write-Host "Python was not found on PATH. Install Python 3.10+ and try again." -ForegroundColor Red; exit 2 }

& $py -c "import requests" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Installing the 'requests' package..." -ForegroundColor Yellow
    & $py -m pip install --quiet requests
}

$env:PYTHONIOENCODING = "utf-8"
$argsList = @("$agent\research_agent.py")
if ($Quick) { $argsList += "--quick" }

Write-Host ""
Write-Host "Wireless Market Research Agent" -ForegroundColor Cyan
Write-Host ("Mode : " + ($(if ($Quick) { "quick smoke test" } else { "full run (~4 min)" })))
Write-Host "Log  : $log"
Write-Host ""

$start = Get-Date
Push-Location $agent
try {
    & $py @argsList 2>&1 | ForEach-Object { $line = "$_"; Write-Host $line; $line } | Out-File $log -Encoding utf8
    $code = $LASTEXITCODE
} finally { Pop-Location }

$mins = [math]::Round(((Get-Date) - $start).TotalMinutes, 1)
Write-Host ""
if ($code -eq 0) {
    Write-Host "Done in $mins min. Report updated: $root\site\index.html" -ForegroundColor Green
    Write-Host "Start the local server with Script\Windows\Start_Server.bat (or open site\index.html directly)."
    if ($env:WR_PUBLISH_CMD) {
        Write-Host "Publishing: $env:WR_PUBLISH_CMD"
        Invoke-Expression $env:WR_PUBLISH_CMD 2>&1 | ForEach-Object { "$_" } | Out-File $log -Append -Encoding utf8
    }
    if ($Open) { Start-Process (Join-Path $root "site\index.html") }
} else {
    Write-Host "Agent failed (exit code $code). See log: $log" -ForegroundColor Red
}

# keep 30 days of logs
Get-ChildItem $logDir -Filter "run_*.log" | Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-30) } | Remove-Item -Force
exit $code
