# Reconnect an already-paired laptop (after a reboot or Wi-Fi change).
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
if (-not (Test-Path ".venv")) { & "$PSScriptRoot\setup.ps1" }
uv run daneion worker @args
