# Join this laptop to a controller. Either pass the values from the dashboard's
# "Add laptop" dialog, for example:
#   .\scripts\join.ps1 10.21.218.141 J8PR-CRUU 486a55f313e487fd
# or run it with no arguments (Join Cluster.cmd) and paste the command it shows.
param(
    [string]$Controller,
    [string]$Code,
    [string]$Fingerprint,
    [int]$Port = 8751,
    [string]$Name = $env:COMPUTERNAME
)
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
if (-not (Test-Path ".venv")) { & "$PSScriptRoot\setup.ps1" }

if (-not ($Controller -and $Code -and $Fingerprint)) {
    Write-Host "In the dashboard click 'Add laptop' and copy the command, e.g." -ForegroundColor Cyan
    Write-Host "  uv run daneion join 10.21.218.141 J8PR-CRUU --fp 486a55f313e487fd"
    $line = Read-Host "Paste it here"
    # Parse the values rather than running pasted text.
    $m = [regex]::Match($line, 'join\s+(\S+)\s+([A-Za-z0-9-]+)\s+--fp\s+([0-9a-fA-F]+)(?:\s+--port\s+(\d+))?')
    if (-not $m.Success) { throw "That does not look like the join command from the dashboard." }
    $Controller = $m.Groups[1].Value
    $Code = $m.Groups[2].Value
    $Fingerprint = $m.Groups[3].Value
    if ($m.Groups[4].Success) { $Port = [int]$m.Groups[4].Value }
}

uv run daneion join $Controller $Code --fp $Fingerprint --port $Port --name $Name
