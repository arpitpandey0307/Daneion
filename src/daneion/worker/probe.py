"""Honest capability report: what this laptop has, measured locally (ADR-0008)."""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from typing import Any

import psutil


def default_slots() -> int:
    return max(1, psutil.cpu_count(logical=False) or (psutil.cpu_count() or 2) // 2)


def _cpu_name() -> str:
    if sys.platform == "win32":
        try:
            import winreg

            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                 r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
            return str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
        except OSError:
            pass
    return platform.processor() or platform.machine()


def _gpus() -> list[dict[str, Any]]:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return []
    try:
        out = subprocess.run(
            [exe, "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    gpus = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 3:
            gpus.append({"name": parts[0], "memory_mb": int(float(parts[1] or 0)),
                         "driver": parts[2]})
    return gpus


def os_name() -> str:
    if sys.platform == "win32":
        return f"Windows {platform.release()}"
    return f"{platform.system()} {platform.release()}"


def probe() -> dict[str, Any]:
    freq = psutil.cpu_freq()
    return {
        "cpu": _cpu_name(),
        "cores_physical": psutil.cpu_count(logical=False),
        "cores_logical": psutil.cpu_count(),
        "cpu_mhz": round(freq.max or freq.current) if freq else None,
        "ram_gb": round(psutil.virtual_memory().total / 2**30, 1),
        "gpus": _gpus(),
        "python": platform.python_version(),
        "arch": platform.machine(),
    }
