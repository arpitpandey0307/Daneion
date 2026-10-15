# Reconnect an already-paired laptop (after a reboot or Wi-Fi change).
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
uv run daneion worker @args
