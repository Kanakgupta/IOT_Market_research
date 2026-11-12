param(
    [ValidateRange(1, 10080)]
    [int]$IntervalMinutes = 1440,
    [switch]$Once
)

$ErrorActionPreference = "Continue"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$agentScript = Join-Path $PSScriptRoot "Run_Agent.ps1"
$powerShell = Get-Command powershell.exe -ErrorAction SilentlyContinue
if (-not $powerShell) { $powerShell = Get-Command pwsh.exe -ErrorAction SilentlyContinue }
if (-not $powerShell) { Write-Host "PowerShell was not found." -ForegroundColor Red; exit 2 }

$gitCandidates = @()
$gitCommand = Get-Command git -ErrorAction SilentlyContinue
if ($gitCommand) { $gitCandidates += $gitCommand.Source }
$gitCandidates += @(
    (Join-Path $env:ProgramFiles "Git\cmd\git.exe"),
    (Join-Path $env:LOCALAPPDATA "Programs\Git\cmd\git.exe")
)
$git = $null
foreach ($candidate in ($gitCandidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -Unique)) {
    & $candidate --version *> $null
    if ($LASTEXITCODE -eq 0) { $git = $candidate; break }
}
if (-not $git) { Write-Host "Git was not found. Install Git for Windows and try again." -ForegroundColor Red; exit 2 }

$repoRoot = & $git -C $root rev-parse --show-toplevel 2>$null
if ($LASTEXITCODE -ne 0 -or -not $repoRoot) { Write-Host "This folder is not a Git repository." -ForegroundColor Red; exit 2 }
$upstream = & $git -C $root rev-parse --abbrev-ref --symbolic-full-name "@{upstream}" 2>$null
if ($LASTEXITCODE -ne 0 -or -not $upstream) {
    Write-Host "The current branch has no upstream. Configure a GitHub remote and upstream branch first." -ForegroundColor Red
    exit 2
}
$upstreamParts = $upstream -split "/", 2
if ($upstreamParts.Count -ne 2) { Write-Host "Could not identify the upstream remote and branch." -ForegroundColor Red; exit 2 }
$remote = $upstreamParts[0]
$remoteBranch = $upstreamParts[1]

function Sync-And-Push {
    & $git -C $root fetch $remote
    if ($LASTEXITCODE -ne 0) { throw "Could not fetch $remote. Check network access and try again." }

    & $git -C $root rebase -X theirs $upstream
    if ($LASTEXITCODE -ne 0) { throw "Rebase failed. Resolve the Git conflicts, then rerun the loop." }

    for ($attempt = 1; $attempt -le 2; $attempt++) {
        & $git -C $root push $remote "HEAD:refs/heads/$remoteBranch"
        if ($LASTEXITCODE -eq 0) { break }
        if ($attempt -eq 2) { throw "Git push failed. Check GitHub authentication and branch permissions." }

        Write-Host "Remote changed during push; fetching and rebasing before retry..." -ForegroundColor Yellow
        & $git -C $root fetch $remote
        if ($LASTEXITCODE -ne 0) { throw "Could not fetch $remote after push was rejected." }
        & $git -C $root rebase -X theirs $upstream
        if ($LASTEXITCODE -ne 0) { throw "Rebase after push rejection failed. Resolve the Git conflicts, then rerun the loop." }
    }

    $remaining = & $git -C $root status --porcelain=v1 --untracked-files=all
    if ($remaining) { throw "Push completed, but local changes remain:`n$remaining" }
}

function Test-GeneratedData {
    $dataRoot = Join-Path $root "site\data"
    $requiredFiles = @("report.js", "news.js", "apps.js", "index.js", "runlog.js")
    foreach ($name in $requiredFiles) {
        $path = Join-Path $dataRoot $name
        if (-not (Test-Path $path) -or (Get-Item $path).Length -eq 0) {
            throw "Required generated data file is missing or empty: $path"
        }
    }

    $files = @(Get-ChildItem $dataRoot -Filter "*.js" -File -Recurse)
    if ($files.Count -eq 0) { throw "No generated JavaScript data files were found." }
    foreach ($file in $files) {
        $content = [System.IO.File]::ReadAllText($file.FullName)
        $separator = $content.IndexOf(" = ", [System.StringComparison]::Ordinal)
        if (-not $content.StartsWith("window.WR_", [System.StringComparison]::Ordinal) -or
            $separator -lt 0 -or -not $content.TrimEnd().EndsWith(";", [System.StringComparison]::Ordinal)) {
            throw "Unexpected generated data format: $($file.FullName)"
        }

        $json = $content.Substring($separator + 3).Trim()
        $json = $json.Substring(0, $json.Length - 1)
        try { $null = ConvertFrom-Json -InputObject $json -ErrorAction Stop }
        catch { throw "Invalid JSON payload in $($file.FullName): $($_.Exception.Message)" }
    }
    Write-Host "Validated $($files.Count) generated data files." -ForegroundColor Green
}

while ($true) {
    $started = Get-Date
    try {
        Write-Host "`n[$($started.ToString('yyyy-MM-dd HH:mm:ss'))] Refreshing wireless research data..." -ForegroundColor Cyan
        & $powerShell.Source -NoProfile -ExecutionPolicy Bypass -File $agentScript
        if ($LASTEXITCODE -ne 0) { throw "Research agent failed with exit code $LASTEXITCODE." }

        Test-GeneratedData
        & $git -C $root add --all
        if ($LASTEXITCODE -ne 0) { throw "Could not stage project changes." }

        & $git -C $root diff --cached --quiet
        if ($LASTEXITCODE -eq 1) {
            $commitDate = (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd")
            & $git -C $root -c "user.name=research-agent" -c "user.email=research-agent@users.noreply.github.com" commit -m "Wireless research and project updates $commitDate"
            if ($LASTEXITCODE -ne 0) { throw "Git commit failed." }
        } elseif ($LASTEXITCODE -eq 0) {
            Write-Host "No file changes to commit; syncing local commits to $upstream." -ForegroundColor Green
        } else {
            throw "Could not inspect staged updates."
        }

        Sync-And-Push
        Write-Host "All non-ignored project changes are pushed to $upstream; the worktree is clean." -ForegroundColor Green
    } catch {
        Write-Host "Update cycle failed: $($_.Exception.Message)" -ForegroundColor Red
        if ($Once) { exit 1 }
    }

    if ($Once) { exit 0 }
    Write-Host "Next update in $IntervalMinutes minutes. Press Ctrl+C to stop."
    Start-Sleep -Seconds ($IntervalMinutes * 60)
}