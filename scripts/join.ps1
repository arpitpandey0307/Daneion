# Join this laptop to a controller. Copy the exact line shown in the dashboard's
# "Add laptop" dialog, for example:
#   .\scripts\join.ps1 10.21.218.141 J8PR-CRUU 486a55f313e487fd
param(
    [Parameter(Mandatory = $true)][string]$Controller,
    [Parameter(Mandatory = $true)][string]$Code,
    [Parameter(Mandatory = $true)][string]$Fingerprint,
    [string]$Name = $env:COMPUTERNAME
)
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
if (-not (Test-Path ".venv")) { & "$PSScriptRoot\setup.ps1" }
uv run daneion join $Controller $Code --fp $Fingerprint --name $Name
