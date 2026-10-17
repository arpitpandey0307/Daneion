# Let other laptops reach this controller: allow the worker port in Windows Firewall and,
# if the Wi-Fi is marked Public, offer to mark it Private. Run once on the CONTROLLER laptop.
param([int]$Port = 8751)
$ErrorActionPreference = "Stop"

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
         ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) {
    Write-Host "Asking for administrator rights (Windows will show a prompt)..." -ForegroundColor Cyan
    Start-Process powershell -Verb RunAs -Wait -ArgumentList @(
        "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"", "-Port", $Port)
    exit
}

$rule = "Daneion workers (TCP $Port)"
if (Get-NetFirewallRule -DisplayName $rule -ErrorAction SilentlyContinue) {
    Write-Host "Firewall rule '$rule' already exists." -ForegroundColor Green
} else {
    New-NetFirewallRule -DisplayName $rule -Direction Inbound -Protocol TCP -LocalPort $Port `
        -Action Allow -Profile Private, Domain | Out-Null
    Write-Host "Added firewall rule '$rule' (Private networks)." -ForegroundColor Green
}

$public = @(Get-NetConnectionProfile | Where-Object NetworkCategory -eq Public)
foreach ($p in $public) {
    $answer = Read-Host "Network '$($p.Name)' ($($p.InterfaceAlias)) is Public, so other laptops are blocked. Mark it Private? [y/N]"
    if ($answer -match '^[yY]') {
        Set-NetConnectionProfile -InterfaceIndex $p.InterfaceIndex -NetworkCategory Private
        Write-Host "  '$($p.Name)' is now Private." -ForegroundColor Green
    } else {
        Write-Host "  Left as Public: workers cannot connect over this network." -ForegroundColor Yellow
    }
}
Write-Host "`nDone. Workers can now join on port $Port."
Read-Host "Press Enter to close"
