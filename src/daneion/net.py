"""LAN address discovery for the join command."""

from __future__ import annotations

import ipaddress
import socket

import psutil

_VIRTUAL = ("vethernet", "virtualbox", "vmware", "hyper-v", "wsl", "docker", "loopback",
            "bluetooth", "tailscale", "zerotier", "npcap", "teredo", "isatap")


def lan_addresses() -> list[str]:
    """Private IPv4 addresses of up, physical-looking interfaces; Wi-Fi/Ethernet first."""
    stats = psutil.net_if_stats()
    found: list[tuple[int, str]] = []
    for name, addrs in psutil.net_if_addrs().items():
        lname = name.lower()
        if not stats.get(name) or not stats[name].isup or any(v in lname for v in _VIRTUAL):
            continue
        for a in addrs:
            if a.family != socket.AF_INET:
                continue
            ip = ipaddress.ip_address(a.address)
            if ip.is_loopback or ip.is_link_local or not ip.is_private:
                continue
            rank = 0 if ("wi-fi" in lname or "wlan" in lname or "wireless" in lname) else 1
            found.append((rank, a.address))
    return [ip for _, ip in sorted(found)]
