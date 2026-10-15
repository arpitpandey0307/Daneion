# One-time setup on any laptop: installs uv if missing, Python deps, and (if Node.js is
# installed) builds the dashboard. Safe to run again. Works from a git checkout or the
# downloaded release zip (which already contains the built dashboard).
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Update-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "User") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                "$env:USERPROFILE\.local\bin"
}

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "Installing uv (Python manager)..." -ForegroundColor Cyan
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        winget install --id astral-sh.uv -e --accept-source-agreements --accept-package-agreements
        Update-Path
    }
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        # No winget (or it failed): use uv's official installer.
        Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
        Update-Path
    }
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { throw "could not install uv" }
}

Write-Host "Installing Python packages (first time downloads Python 3.12)..." -ForegroundColor Cyan
# Install everything `uv run` will want now, so later runs work offline. The release zip pins
# exact versions from uv.lock.
if (Test-Path "tests") { uv sync } else { uv sync --frozen }
if ($LASTEXITCODE -ne 0) { throw "uv sync failed" }

if (-not (Test-Path "apps/console/dist/index.html")) {
    if (Get-Command npm -ErrorAction SilentlyContinue) {
        Write-Host "Building the dashboard..." -ForegroundColor Cyan
        Push-Location apps/console
        npm ci --no-audit --no-fund
        npm run build
        Pop-Location
    } else {
        Write-Host "Node.js not found: fine for worker laptops (only the controller shows the dashboard)." -ForegroundColor Yellow
    }
}

Write-Host "`nChecking this laptop..." -ForegroundColor Cyan
uv run daneion doctor --role worker
