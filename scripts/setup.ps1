# One-time setup on any laptop: installs uv if missing, Python deps, and (if Node.js is
# installed) builds the dashboard. Safe to run again.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "Installing uv (Python manager)..." -ForegroundColor Cyan
    winget install --id astral-sh.uv -e --accept-source-agreements --accept-package-agreements
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "User") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "Machine")
}

Write-Host "Installing Python packages (first time downloads Python 3.12)..." -ForegroundColor Cyan
uv sync
if ($LASTEXITCODE -ne 0) { throw "uv sync failed" }

if (Get-Command npm -ErrorAction SilentlyContinue) {
    if (-not (Test-Path "apps/console/dist/index.html")) {
        Write-Host "Building the dashboard..." -ForegroundColor Cyan
        Push-Location apps/console
        npm ci --no-audit --no-fund
        npm run build
        Pop-Location
    }
} else {
    Write-Host "Node.js not found: fine for worker laptops (only the controller shows the dashboard)." -ForegroundColor Yellow
}

Write-Host "`nChecking this laptop..." -ForegroundColor Cyan
uv run daneion doctor --role worker
