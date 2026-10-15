# Start the controller on the presenting laptop and open the dashboard.
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
if (-not (Test-Path ".venv")) { & "$PSScriptRoot\setup.ps1" }
Start-Job -ScriptBlock { Start-Sleep 6; Start-Process "http://127.0.0.1:8750/" } | Out-Null
uv run daneion controller @args
